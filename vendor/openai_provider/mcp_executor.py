"""mcp_executor.py — ToolExecutor that routes tool calls through KiroCrew's MCP layer.

KiroCrew manages its own MCP servers (kirocrew-core, kirocrew-cron, file tools,
bash, etc.). This executor exposes those tools to the OpenAI model as standard
function-calling definitions, then routes execution back through the MCP gateway.

Architecture:
    OpenAI Model
      → McpToolExecutor.execute(name, args)
        → _call_tool(name, args)
          ├── Local registry handlers (read, write, edit, grep, glob, find,
          │   list_dir, web_fetch, web_search, execute_bash, ask_question,
          │   spawn_run, spawn_list) — reimplemented in Python because
          │   kiro-cli's Go built-ins are not available here
          └── Dynamic MCP tools — discovered from managed servers (kirocrew-core,
              kirocrew-cron, kirocrew-computer) via in-process _list_tools(),
              executed by spawning the server and calling via MCP stdio protocol

Discovery:
    1. Managed servers: import module → _list_tools() → full tool schemas
    2. Non-managed servers: mcp_discovery.list_servers() → spawn → tools/list
    3. Merge with local handlers (local takes priority for name conflicts)

Execution:
    1. Local registry handler (O(1) dict lookup, zero-latency)
    2. MCP stdio protocol (spawn server → tools/call → parse result)
    3. Error with available-tools hint
"""

from __future__ import annotations

import asyncio
import contextvars
import importlib
import json
import logging
import os
import shutil
from typing import Any

from .provider import ToolExecutor

logger = logging.getLogger(__name__)


# ── Per-call session identity ────────────────────────────────────────────────
#
# One gateway process serves MANY sessions at once (dashboard chats, subagents,
# crons), and the MCP servers are spawned as child processes that must carry the
# identity of the session whose tool ran. KiroCrew's own ACP path puts that
# identity in each session's OWN mcpServers env element; we have no element, so
# the identity has to reach the child some other way.
#
# Writing it into os.environ is wrong and was the cause of
# ``member_identity_unavailable`` / ``identity_unattested``: two sessions running
# tools concurrently overwrite each other's KIROCREW_SESSION_KEY between the
# moment spawn_run resolves its parent_session and the moment the gateway
# verifies the declared key against the signed token, so a request could carry
# session A's key beside session B's token and be refused. A contextvar is
# per-task, so concurrent sessions cannot see each other's identity, and the
# child gets the pair stamped explicitly at spawn time.
_TOOL_IDENTITY: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "openai_bridge_tool_identity", default={}
)


def set_tool_identity(session_key: str, token: str) -> contextvars.Token:
    """Bind *session_key* + *token* to the current task for the duration of a tool call."""
    return _TOOL_IDENTITY.set({"session_key": session_key or "", "token": token or ""})


def reset_tool_identity(token: contextvars.Token) -> None:
    """Unbind the identity :func:`set_tool_identity` installed."""
    try:
        _TOOL_IDENTITY.reset(token)
    except (ValueError, RuntimeError):
        _TOOL_IDENTITY.set({})


def _child_env() -> dict[str, str]:
    """The environment an MCP child gets: this process's, plus OUR session identity.

    Both variables are written together from the same bound identity, so they can
    never disagree -- which is exactly what the gateway's attestation compares.
    Without a bound identity the parent environment is inherited unchanged, so a
    non-provider caller keeps whatever it had.
    """
    env = dict(os.environ)
    identity = _TOOL_IDENTITY.get() or {}
    key = identity.get("session_key") or ""
    token = identity.get("token") or ""
    if key:
        env["KIROCREW_SESSION_KEY"] = key
    else:
        # Inheriting a stale key is worse than having none: the child would
        # declare a session that is not the one whose tool is running, and the
        # call gets attributed to whoever left that variable behind.
        env.pop("KIROCREW_SESSION_KEY", None)
    if token:
        env["KIROCREW_STUB_SESSION_TOKEN"] = token
    else:
        env.pop("KIROCREW_STUB_SESSION_TOKEN", None)
    return env


def _trace_mcp_fail(server: str, tool: str, err: str, stderr: str = "") -> None:
    """Record a failed MCP execution where the turn log can show it.

    Without this the two failures that matter most -- a server that does not
    advertise the tool, and a server that died before answering -- looked
    identical (both ``None``), which is how a cron refusal was mistaken for a
    spawn result. Imported lazily: provider.py imports this module.
    """
    try:
        from .provider import _trace
        _trace("mcp_fail", server=server, tool=tool, err=err[:300],
               stderr=stderr[-300:])
    except Exception:  # noqa: BLE001
        pass


# ── Managed server tool discovery (in-process, no spawn) ─────────────────────

# These are the managed MCP servers whose _list_tools() we can call directly
# without spawning a subprocess. Same list as mcp_discovery._MANAGED_SERVER_TOOL_MODULES.
_MANAGED_SERVER_MODULES = {
    "kirocrew-core": "kiro_crew.mcp_core",
    "kirocrew-cron": "kiro_crew.mcp_cron",
    "kirocrew-computer": "kiro_crew.mcp_computer",
    # The rest of crew's own map (mcp_discovery_managed._MANAGED_SERVER_TOOL_MODULES).
    # Leaving them out made their tools invisible to discovery, so session_create
    # (kirocrew-dashboard) and work_ledger_* (kirocrew-work) had no owner at all.
    "kirocrew-dashboard": "kiro_crew.mcp_dashboard",
    "kirocrew-work": "kiro_crew.mcp_work",
    "kirocrew-crew-log": "kiro_crew.mcp_crew_log",
    "kirocrew-debug": "kiro_crew.mcp_debug",
    "kirocrew-panel": "kiro_crew.mcp_panel",
    "kirocrew-guide": "kiro_crew.mcp_guide",
    "kirocrew-mate": "kiro_crew.mcp_mate",
}


def _discover_managed_tools() -> tuple[list[dict], dict[str, list[str]]]:
    """Discover tools from managed MCP servers by calling _list_tools() in-process.

    This avoids spawning subprocesses and works regardless of sandbox availability.

    Returns:
        Tuple of (tools, tools_by_server) where:
        - tools: list of MCP-format tool dicts {name, description, inputSchema}
        - tools_by_server: dict mapping server_name → list of tool names
    """
    tools: list[dict] = []
    tools_by_server: dict[str, list[str]] = {}
    for server_name, module_name in _MANAGED_SERVER_MODULES.items():
        try:
            module = importlib.import_module(module_name)
            server_tools = module._list_tools()
            if isinstance(server_tools, list):
                server_tool_names = []
                for t in server_tools:
                    if isinstance(t, dict) and t.get("name"):
                        tools.append(t)
                        server_tool_names.append(t["name"])
                tools_by_server[server_name] = server_tool_names
                logger.debug(
                    "Discovered %d tools from %s", len(server_tools), server_name
                )
        except Exception as exc:
            logger.debug("Failed to discover tools from %s: %s", server_name, exc)
    return tools, tools_by_server


# ── Non-managed server discovery (via mcp_discovery) ─────────────────────────

async def _discover_external_tools() -> tuple[list[dict], dict[str, list[str]]]:
    """Discover tools from non-managed MCP servers via mcp_discovery.

    Uses mcp_discovery.list_servers() to find configured servers, then spawns
    each one and sends tools/list to get full schemas.

    Returns ``(tools, tools_by_server)``. The mapping is the point: execution
    must ask the server that actually ADVERTISES a tool, never "whichever server
    answers first" -- a server that does not own the tool replies with its own
    error text, and that text used to be handed to the model as the result.
    """
    try:
        from kiro_crew.mcp_discovery import list_servers
    except ImportError:
        return [], {}

    servers = list_servers()
    tools: list[dict] = []
    by_server: dict[str, list[str]] = {}

    for server in servers:
        # Skip managed servers (handled by _discover_managed_tools)
        if server.name in _MANAGED_SERVER_MODULES:
            continue
        # Skip disabled servers
        if server.disabled:
            continue
        # Skip servers without a command
        if not server.command:
            continue

        try:
            server_tools = await _probe_server_tools(server)
            tools.extend(server_tools)
            if server_tools:
                by_server[server.name] = [t["name"] for t in server_tools]
        except Exception as exc:
            logger.debug("Failed to discover tools from %s: %s", server.name, exc)
            # A probe that fails silently costs its tools on the wire: the
            # filter treats "no owner" as "cannot run", so kb_* disappearing
            # from a session has to leave a trace rather than a debug line.
            _trace_mcp_fail(server.name, "-", "discovery failed", str(exc))

    return tools, by_server


async def _probe_server_tools(server) -> list[dict]:
    """Spawn an MCP server and get its full tool definitions via tools/list.

    Returns list of MCP-format tool dicts: {name, description, inputSchema}.
    """
    # Resolve command
    resolved = shutil.which(server.command)
    if not resolved:
        return []

    proc = None
    try:
        # Spawn the server
        proc = await asyncio.create_subprocess_exec(
            resolved, *(server.args or []),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_child_env(),
        )

        # Send initialize request
        init_req = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "openai-provider", "version": "1.0"},
            },
        }) + "\n"
        proc.stdin.write(init_req.encode())
        await proc.stdin.drain()

        # Read initialize response
        resp_line = await asyncio.wait_for(proc.stdout.readline(), timeout=10.0)
        if not resp_line:
            return []

        # Send initialized notification
        notif = json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        }) + "\n"
        proc.stdin.write(notif.encode())
        await proc.stdin.drain()

        # Send tools/list request
        list_req = json.dumps({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        }) + "\n"
        proc.stdin.write(list_req.encode())
        await proc.stdin.drain()

        # Read tools/list response
        resp_line = await asyncio.wait_for(proc.stdout.readline(), timeout=10.0)
        if not resp_line:
            return []

        resp = json.loads(resp_line)
        result = resp.get("result", {})
        tools_data = result.get("tools", [])

        return [t for t in tools_data if isinstance(t, dict) and t.get("name")]

    except Exception as exc:
        logger.debug("MCP probe for %s failed: %s", server.name, exc)
        return []
    finally:
        if proc:
            try:
                proc.terminate()
            except Exception:
                pass


# ── MCP tool execution via stdio protocol ────────────────────────────────────

# Cache of server command info for tool → server mapping
_tool_server_map: dict[str, str] = {}  # tool_name → server_name
_server_info: dict[str, dict] = {}  # server_name → {command, args, env}


def _build_tool_server_map(tools_by_server: dict[str, list[str]] | None = None) -> None:
    """Build the tool → server map plus the per-server launch info.

    Ownership comes from the DISCOVERY result, not from what ``list_servers()``
    happens to enumerate: a server that answered ``tools/list`` owns its tools
    even when the config reader does not name it, and silently dropping that
    ownership is exactly how a call becomes unroutable. ``list_servers()`` is
    still what supplies each server's command, so it alone can be spawned.
    """
    try:
        from kiro_crew.mcp_discovery import list_servers
        servers = list_servers()
    except ImportError:
        servers = []
    for server in servers:
        if server.disabled or not server.command:
            continue
        _server_info[server.name] = {
            "command": server.command,
            "args": server.args or [],
        }
    for server_name, tool_names in (tools_by_server or {}).items():
        for tool_name in tool_names:
            if isinstance(tool_name, str):
                _tool_server_map[tool_name] = server_name
    for server in servers:
        if server.disabled or not server.command:
            continue
        for tool_name in (getattr(server, "tools", None) or []):
            if isinstance(tool_name, str):
                _tool_server_map.setdefault(tool_name, server.name)


async def _execute_via_mcp_stdio(
    server_name: str,
    tool_name: str,
    args: dict,
) -> str | None:
    """Execute a tool by spawning its MCP server and calling tools/call.

    Returns the tool result string, or None if execution failed.
    """
    info = _server_info.get(server_name)
    if not info:
        return None

    resolved = shutil.which(info["command"])
    if not resolved:
        return None

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            resolved, *info["args"],
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_child_env(),
        )

        # Initialize
        init_req = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "openai-provider", "version": "1.0"},
            },
        }) + "\n"
        proc.stdin.write(init_req.encode())
        await proc.stdin.drain()
        await asyncio.wait_for(proc.stdout.readline(), timeout=10.0)

        # Initialized notification
        notif = json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        }) + "\n"
        proc.stdin.write(notif.encode())
        await proc.stdin.drain()

        # tools/call
        call_req = json.dumps({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": args,
            },
        }) + "\n"
        proc.stdin.write(call_req.encode())
        await proc.stdin.drain()

        # Read response
        resp_line = await asyncio.wait_for(proc.stdout.readline(), timeout=30.0)
        if not resp_line:
            _trace_mcp_fail(server_name, tool_name, "empty stdout")
            return None

        resp = json.loads(resp_line)
        if resp.get("error"):
            err = resp["error"]
            return f"[MCP error: {err.get('message', str(err))}]"

        result = resp.get("result", {})
        content = result.get("content", [])
        parts = [c.get("text", "") for c in content if c.get("type") == "text"]
        return "\n".join(parts) if parts else json.dumps(result)

    except Exception as exc:
        stderr_tail = ""
        try:
            if proc is not None and proc.stderr is not None:
                raw = await asyncio.wait_for(proc.stderr.read(), timeout=1.0)
                stderr_tail = raw.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        logger.debug("MCP stdio execution of %s/%s failed: %s", server_name, tool_name, exc)
        _trace_mcp_fail(server_name, tool_name, f"{type(exc).__name__}: {exc}", stderr_tail)
        return None
    finally:
        if proc:
            try:
                proc.terminate()
            except Exception:
                pass


# ── MCP tool format conversion ───────────────────────────────────────────────

def _mcp_tool_to_openai(tool: dict) -> dict:
    """Convert an MCP tool definition to OpenAI function-calling format."""
    input_schema = tool.get("inputSchema") or tool.get("input_schema") or {}
    # Strip additionalProperties if present (some MCP servers add it, OpenAI rejects)
    clean_schema = {k: v for k, v in input_schema.items() if k != "additionalProperties"}
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": clean_schema or {"type": "object", "properties": {}},
        },
    }


# ── McpToolExecutor ──────────────────────────────────────────────────────────

class McpToolExecutor(ToolExecutor):
    """Executor that exposes KiroCrew MCP tools to the OpenAI model.

    Discovery:
      - Managed servers (kirocrew-core, kirocrew-cron, kirocrew-computer):
        in-process _list_tools() — no subprocess, no sandbox needed
      - Non-managed servers: mcp_discovery.list_servers() → spawn → tools/list
      - Local handlers: Python reimplementations of kiro-cli built-in tools

    Execution:
      1. Local registry handler (O(1) dict lookup)
      2. MCP stdio protocol (spawn server → tools/call)
      3. Error with available-tools hint
    """

    def __init__(self) -> None:
        self._tools_cache: list[dict] | None = None
        self._definitions_cache: list[dict] | None = None
        self._mcp_tool_names: set[str] = set()  # tools from MCP servers
        self._lock = asyncio.Lock()

    # ── Tool discovery ────────────────────────────────────────────────────────

    def tool_definitions(self) -> list[dict]:
        """Return OpenAI tool definitions (sync — uses cached value)."""
        if self._definitions_cache is not None:
            return self._definitions_cache
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self._fetch_tools_async())
                return []
            else:
                loop.run_until_complete(self._fetch_tools_async())
                return self._definitions_cache or []
        except Exception:
            return []

    async def _fetch_tools_async(self) -> None:
        """Populate tools cache by discovering from MCP servers + local handlers."""
        async with self._lock:
            if self._definitions_cache is not None:
                return

            # Ensure local handlers are registered
            from .mcp_handler_registry import McpHandlerRegistry, register_all_builtins
            if not McpHandlerRegistry._handlers:
                register_all_builtins()

            # Discover tools from managed servers (in-process, no spawn)
            managed_tools, managed_tools_by_server = _discover_managed_tools()

            # Discover tools from non-managed servers (spawn + MCP protocol)
            external_tools: list[dict] = []
            external_by_server: dict[str, list[str]] = {}
            try:
                external_tools, external_by_server = await _discover_external_tools()
            except Exception as exc:
                logger.warning("External MCP discovery failed: %s", exc)

            # Build tool → server mapping for execution. Both discovery paths
            # contribute, so every advertised tool has exactly one owner.
            _build_tool_server_map(
                tools_by_server={**managed_tools_by_server, **external_by_server}
            )

            # Track MCP tool names (for execution routing)
            all_mcp_tools = managed_tools + external_tools
            self._mcp_tool_names = {t["name"] for t in all_mcp_tools}

            # Merge: local handlers take priority, then MCP tools
            local_names = set(McpHandlerRegistry._handlers.keys())
            all_tools: list[dict] = []

            # Add local handler definitions (in MCP format)
            for name in sorted(local_names):
                defn = McpHandlerRegistry.definition_for(name)
                if defn:
                    fn = defn.get("function", {})
                    all_tools.append({
                        "name": fn.get("name", ""),
                        "description": fn.get("description", ""),
                        "inputSchema": fn.get("parameters", {"type": "object", "properties": {}}),
                    })

            # Add MCP tools not already in local registry
            for tool in all_mcp_tools:
                if tool["name"] not in local_names:
                    all_tools.append(tool)

            self._tools_cache = all_tools
            self._definitions_cache = [_mcp_tool_to_openai(t) for t in all_tools]
            logger.info(
                "McpToolExecutor: loaded %d tools (%d local + %d managed MCP + %d external MCP)",
                len(self._definitions_cache),
                len(local_names),
                len(managed_tools),
                len(external_tools),
            )

    # ── Tool execution ────────────────────────────────────────────────────────

    def server_name_for(self, tool_name: str) -> str:
        """Return the MCP server name that hosts this tool, or '' for local tools."""
        from .mcp_handler_registry import McpHandlerRegistry
        if tool_name in McpHandlerRegistry._handlers:
            return ""  # local handler, no MCP server
        return _tool_server_map.get(tool_name, "")

    def handler_definition(self, tool_name: str) -> dict | None:
        """The wire definition of a tool this build implements locally."""
        from .mcp_handler_registry import McpHandlerRegistry
        try:
            return McpHandlerRegistry.definition_for(tool_name)
        except Exception:  # noqa: BLE001
            return None

    def routable_names(self) -> set[str]:
        """Every tool name this build can actually execute.

        A tool is routable when a local handler owns it or a discovered server
        advertises it. Anything else can only be answered with an error, and
        KiroCrew advertises its whole set every turn (``_CORE_TOOL_NAMES``
        included), so the wire filter uses this rather than trusting the names.

        Empty means NO OPINION: before discovery has run, the server map is not
        filled in yet, and filtering on it would cut every crew tool down to the
        thirteen local handlers on the first turn of a session.
        """
        from .mcp_handler_registry import McpHandlerRegistry
        if self._definitions_cache is None:
            return set()
        names = set(McpHandlerRegistry._handlers)
        names.update(_tool_server_map)
        return names

    async def execute(self, name: str, args: dict) -> Any:
        """Execute a tool by name with given args, return string result."""
        if self._definitions_cache is None:
            await self._fetch_tools_async()

        try:
            result = await _call_tool(name, args, self._mcp_tool_names)
            return result
        except Exception as exc:
            logger.exception("McpToolExecutor: tool %s failed", name)
            return f"[Tool execution error: {exc}]"


# ── Tool execution dispatch ──────────────────────────────────────────────────

async def _call_tool(
    name: str,
    args: dict,
    mcp_tool_names: set[str],
) -> str:
    """Dispatch a tool call.

    Routing order:
      1. Local registry handler (direct, zero-latency)
      2. MCP stdio protocol (spawn server → tools/call)
      3. Error with available-tools hint
    """
    from .mcp_handler_registry import McpHandlerRegistry

    # 1. Try local registry handler
    handler = McpHandlerRegistry.get(name)
    if handler is not None:
        try:
            return await handler.execute(args)
        except Exception as exc:
            logger.exception("Handler %r failed", name)
            return f"[Tool execution error: {exc}]"

    # 2. Try MCP stdio -- ONLY the server that advertises this tool.
    if name in mcp_tool_names:
        server_name = _tool_server_map.get(name)
        if server_name:
            result = await _execute_via_mcp_stdio(server_name, name, args)
            if result is not None:
                return result

        # No sweep of the other servers. Tried that once: a spawn_sub_agents
        # call reached kirocrew-cron, whose shim forwards any name to
        # POST /api/crons/tools, and the model got "Provide a known cron tool
        # and an arguments object." as the result of a spawn it believed had
        # run. A tool the bridge cannot route is an error, not a lottery.
        owner = f"its owner {server_name!r} answered nothing" if server_name \
            else "no server advertises it"
        _trace_mcp_fail(server_name or "-", name, "not routable")
        raise RuntimeError(
            f"MCP tool {name!r} could not be executed: {owner}. "
            f"Servers: {', '.join(sorted(_server_info)) or 'none'}"
        )

    # 3. No handler found
    available = ", ".join(sorted(McpHandlerRegistry._handlers.keys()))
    other_mcp = mcp_tool_names - set(McpHandlerRegistry._handlers.keys())
    if other_mcp:
        available += ", " + ", ".join(sorted(other_mcp))
    raise RuntimeError(
        f"No handler found for tool: {name}. Available: {available}"
    )
