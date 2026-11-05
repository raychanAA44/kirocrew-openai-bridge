#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_spawn_handler.py — independent review of the spawn_sub_agents bridge fix.

Read-only w.r.t. production code: ``kiro_crew.*`` is stubbed, ``openai_provider``
is loaded as a synthetic package so its ``__init__.py`` (which imports install.py
and therefore the whole dashboard) never runs. No network, no subprocess, no
real spawn: every gateway call and every MCP stdio call is a recording stub.

Run:
    python tests/test_spawn_handler.py        (or: py -3 tests/test_spawn_handler.py)

Exit code 0 = every case PASSed.
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import os
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENDOR = HERE.parent / "vendor"
PKG_DIR = VENDOR / "openai_provider"

PASSED: list[str] = []
FAILED: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    (PASSED if ok else FAILED).append(label)
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"   [{detail}]" if detail else ""))
    return ok


# ── bootstrap: stub kiro_crew, load the package without __init__.py ─────────
def _stub_kiro_crew() -> None:
    for name in ("kiro_crew", "kiro_crew.acp", "kiro_crew.providers"):
        sys.modules.setdefault(name, types.ModuleType(name))

    t = types.ModuleType("kiro_crew.acp.types")

    class AcpEvent:
        def __init__(self, *a, **k):
            self.args, self.kwargs = a, k

    class TurnUsage:
        def __init__(self, *a, **k):
            self.args, self.kwargs = a, k

    for const in (
        "EVENT_TEXT_CHUNK", "EVENT_THINKING_CHUNK", "EVENT_TOOL_CALL",
        "EVENT_TOOL_CALL_UPDATE", "EVENT_TOOL_RESULT", "EVENT_PERMISSION_REQUEST",
        "EVENT_COMPLETE", "EVENT_CLEAR_STATUS",
        "STOP_REASON_END_TURN", "STOP_REASON_CANCELLED",
        "OPTION_ALLOW_ONCE", "OPTION_ALLOW_ALWAYS",
    ):
        setattr(t, const, const.lower())
    t.AcpEvent = AcpEvent
    t.TurnUsage = TurnUsage

    b = types.ModuleType("kiro_crew.providers.base")

    class LLMProvider:
        pass

    class CancelOutcome:
        pass

    b.LLMProvider = LLMProvider
    b.CancelOutcome = CancelOutcome
    sys.modules["kiro_crew.acp.types"] = t
    sys.modules["kiro_crew.providers.base"] = b
    sys.modules["kiro_crew"].acp = sys.modules["kiro_crew.acp"]
    sys.modules["kiro_crew"].providers = sys.modules["kiro_crew.providers"]
    sys.modules["kiro_crew.acp"].types = t


def _load_modules():
    _stub_kiro_crew()
    pkg = types.ModuleType("openai_provider")
    pkg.__path__ = [str(PKG_DIR)]
    sys.modules["openai_provider"] = pkg
    P = importlib.import_module("openai_provider.provider")
    R = importlib.import_module("openai_provider.mcp_handler_registry")
    E = importlib.import_module("openai_provider.mcp_executor")
    return P, R, E


# ── recording fakes ────────────────────────────────────────────────────────
class Rec:
    """Sync/async recorder for module-level gateway helpers."""

    def __init__(self, replies):
        self.replies = list(replies) if isinstance(replies, list) else None
        self.fixed = None if self.replies is not None else replies
        self.calls: list[tuple[str, dict]] = []

    async def post(self, path, body):
        self.calls.append((path, body))
        if self.replies is not None:
            return self.replies[len(self.calls) - 1]
        return self.fixed

    async def get(self, path):
        self.calls.append((path, {}))
        if self.replies is not None:
            idx = min(len(self.calls) - 1, len(self.replies) - 1)
            return self.replies[idx]
        return self.fixed


def _patch_gateway(R, post_replies=None, get_replies=None):
    """Replace _gateway_post/_gateway_get and the session-key resolver."""
    pcalls: list[tuple[str, dict]] = []
    gcalls: list[str] = []

    async def fake_post(path, body):
        pcalls.append((path, body))
        if isinstance(post_replies, list):
            return post_replies[min(len(pcalls) - 1, len(post_replies) - 1)]
        return post_replies

    async def fake_get(path):
        gcalls.append(path)
        if isinstance(get_replies, list):
            return get_replies[min(len(gcalls) - 1, len(get_replies) - 1)]
        return get_replies

    R._gateway_post, R._gateway_get = fake_post, fake_get
    R._resolve_session_key = lambda: "sess-test"
    R._internal_secret = lambda: "secret-test"
    R._resolve_gateway_api = lambda: "http://127.0.0.1:0"
    return pcalls, gcalls


def _handler(R, wait=0.05, poll=0.01):
    h = R.SpawnSubAgentsHandler()
    h._WAIT_SECS, h._POLL_SECS = wait, poll
    return h


# ── cases ──────────────────────────────────────────────────────────────────
def main() -> int:
    for p in (PKG_DIR / "mcp_handler_registry.py", PKG_DIR / "mcp_executor.py",
              PKG_DIR / "provider.py"):
        print(f"# {p.name:24} {p.stat().st_size:>7} B  mtime {__import__('datetime').datetime.fromtimestamp(p.stat().st_mtime)}")
    print()

    P, R, E = _load_modules()
    P._TRACE_PATH = os.path.join(tempfile.mkdtemp(prefix="spawn_review_"), "trace.log")

    # a. registration + schema
    R.register_all_builtins()
    h = R.McpHandlerRegistry.get("spawn_sub_agents")
    check("a  register_all_builtins() → McpHandlerRegistry.get('spawn_sub_agents') non-None",
          h is not None, f"registry size={len(R.McpHandlerRegistry._handlers)}")
    d = h.definition() if h is not None else {}
    fn = (d or {}).get("function", {})
    params = fn.get("parameters", {}) or {}
    props = params.get("properties", {}) or {}
    item_req = ((props.get("agents") or {}).get("items") or {}).get("required")
    check("a2 definition(): name + required=['agents'] + items.required=['prompt']",
          fn.get("name") == "spawn_sub_agents"
          and params.get("required") == ["agents"]
          and item_req == ["prompt"],
          f"required={params.get('required')} items.required={item_req}")
    check("a3 schema exposes cwd/max_turns/include_memory/include_lessons/include_project",
          {"cwd", "max_turns", "include_memory", "include_lessons",
           "include_project"} <= set(props),
          f"props={sorted(props)}")

    # b. crew-shaped payload → two /api/spawn POSTs with the right body
    pcalls, gcalls = _patch_gateway(R, post_replies=[{"id": "a1"}, {"id": "a2"}],
                                    get_replies={"done": True, "status": "done", "result": "ok"})
    out_b = asyncio.run(_handler(R).execute({
        "agents": [{"agent_or_mode": "kirocrew-lite", "prompt": "x"}, {"prompt": "y"}],
        "cwd": "D:/",
        "include_lessons": False,
    }))
    paths_ok = [p for p, _ in pcalls] == ["/api/spawn", "/api/spawn"]
    b0 = pcalls[0][1] if pcalls else {}
    b1 = pcalls[1][1] if len(pcalls) > 1 else {}
    check("b  two POSTs to /api/spawn", len(pcalls) == 2 and paths_ok,
          f"paths={[p for p, _ in pcalls]}")
    check("b2 body has task/agent/parent_session (2nd entry agent empty, prompt kept)",
          b0.get("task") == "x" and b0.get("agent") == "kirocrew-lite"
          and b0.get("parent_session") == "sess-test"
          and b1.get("task") == "y" and b1.get("agent") == "",
          f"b0={json.dumps({k: b0.get(k) for k in ('task', 'agent', 'parent_session', 'cwd', 'include_lessons')}, ensure_ascii=False)}")
    check("b3 include_lessons=False and cwd passthrough",
          b0.get("include_lessons") is False and b1.get("include_lessons") is False
          and b0.get("cwd") == "D:/" and b1.get("cwd") == "D:/",
          f"include_lessons={b0.get('include_lessons')} cwd={b0.get('cwd')}")
    check("b4 include_memory default True is NOT sent (only negatives are)",
          "include_memory" not in b0 and "include_project" not in b0,
          f"keys={sorted(b0)}")

    # c. poll: running → done, then stop
    pcalls, gcalls = _patch_gateway(
        R, post_replies=[{"id": "a1"}],
        get_replies=[{"status": "running"}, {"done": True, "status": "done", "result": "hello"}])
    out_c = asyncio.run(_handler(R, wait=5.0, poll=0.01).execute(
        {"agents": [{"prompt": "do thing"}]}))
    check("c  polls until done then stops (2 GETs: running → done)",
          len(gcalls) == 2 and all(p == "/api/spawn/a1" for p in gcalls),
          f"get_calls={len(gcalls)} paths={gcalls}")
    check("c2 result text contains the child's result", "hello" in out_c,
          f"head={out_c.splitlines()[1][:80]!r}" if out_c else "empty")

    # d. gateway error surfaces, no exception
    pcalls, gcalls = _patch_gateway(R, post_replies={"error": "boom"}, get_replies={})
    try:
        out_d = asyncio.run(_handler(R).execute({"agents": [{"prompt": "x"}]}))
        raised = ""
    except Exception as exc:  # noqa: BLE001
        out_d, raised = "", f"{type(exc).__name__}: {exc}"
    check("d  {'error':'boom'} → text contains boom, no raise",
          not raised and "boom" in out_d, raised or f"head={out_d[:100]!r}")

    # e. missing / empty / non-list agents
    pcalls, gcalls = _patch_gateway(R, post_replies={"id": "x"}, get_replies={"done": True})
    e_outs = []
    for bad in ({}, {"agents": []}, {"agents": "nope"}):
        e_outs.append(asyncio.run(_handler(R).execute(bad)))
    check("e  missing/empty/non-list agents → Error text, zero requests",
          len(pcalls) == 0 and all(o.startswith("Error:") for o in e_outs),
          f"post_calls={len(pcalls)} outs={[o[:40] for o in e_outs]}")

    # f. no blind sweep: unknown MCP tool raises and calls no server
    calls: list[tuple[str, str]] = []

    async def counting_stdio(server, tool, args):
        calls.append((server, tool))
        return "should-not-happen"

    E._execute_via_mcp_stdio = counting_stdio
    E._tool_server_map = {}                                    # nobody owns "ghost"
    E._server_info = {"kirocrew-cron": {"command": "x", "args": []},
                      "other-srv": {"command": "y", "args": []}}
    try:
        asyncio.run(E._call_tool("ghost", {}, {"ghost"}))
        raised_f = ""
    except Exception as exc:  # noqa: BLE001
        raised_f = f"{type(exc).__name__}: {exc}"
    check("f  unroutable MCP tool raises RuntimeError, zero stdio attempts",
          raised_f.startswith("RuntimeError") and calls == [],
          raised_f[:120] or "no exception")
    check("f2 message names the reason (no sweep, no lottery)",
          "no server advertises it" in raised_f, raised_f[:160])

    # f3. owner known but silent → ONE attempt at the owner, still no sweep
    calls.clear()
    E._tool_server_map = {"toolA": "kirocrew-cron"}

    async def silent_stdio(server, tool, args):
        calls.append((server, tool))
        return None

    E._execute_via_mcp_stdio = silent_stdio
    try:
        asyncio.run(E._call_tool("toolA", {}, {"toolA"}))
        raised_f3 = ""
    except Exception as exc:  # noqa: BLE001
        raised_f3 = f"{type(exc).__name__}: {exc}"
    check("f3 silent owner → exactly 1 attempt (its owner), others untouched",
          raised_f3.startswith("RuntimeError") and calls == [("kirocrew-cron", "toolA")],
          f"calls={calls} err={raised_f3[:100]}")

    # g. _discover_external_tools returns (tools, by_server)
    class Srv:
        def __init__(self, name, disabled=False, command="cmd", args=None):
            self.name, self.disabled, self.command, self.args = name, disabled, command, args or []

    disc = types.ModuleType("kiro_crew.mcp_discovery")
    disc.list_servers = lambda: [
        Srv("kirocrew-core"),            # managed → skipped
        Srv("off", disabled=True),       # disabled → skipped
        Srv("nocmd", command=""),        # no command → skipped
        Srv("srvA"), Srv("srvB"),
    ]
    sys.modules["kiro_crew.mcp_discovery"] = disc
    sys.modules["kiro_crew"].mcp_discovery = disc

    async def fake_probe(server):
        return [{"name": f"t_{server.name}", "description": "", "inputSchema": {}}]

    E._probe_server_tools = fake_probe
    got_g = asyncio.run(E._discover_external_tools())
    check("g  _discover_external_tools returns a 2-tuple (tools, by_server)",
          isinstance(got_g, tuple) and len(got_g) == 2, f"type={type(got_g).__name__}")
    tools_g, map_g = got_g
    check("g2 only enabled non-managed servers contribute; by_server maps name→tools",
          [t["name"] for t in tools_g] == ["t_srvA", "t_srvB"]
          and map_g == {"srvA": ["t_srvA"], "srvB": ["t_srvB"]},
          f"tools={[t['name'] for t in tools_g]} map={map_g}")

    # h. merge managed + external maps into the execution routing map
    #    NOTE: _build_tool_server_map only maps servers that list_servers()
    #    returns, so the stub has to list every server the map should know --
    #    that coupling is reported in the review.
    E._tool_server_map.clear()
    E._server_info.clear()
    disc.list_servers = lambda: [Srv("kirocrew-core"), Srv("srvX")]

    def fake_managed():
        return ([{"name": "core_tool", "description": "", "inputSchema": {}}],
                {"kirocrew-core": ["core_tool"]})

    async def fake_external():
        return ([{"name": "ext_tool", "description": "", "inputSchema": {}}],
                {"srvX": ["ext_tool"]})

    E._discover_managed_tools = fake_managed
    E._discover_external_tools = fake_external
    ex = E.McpToolExecutor()
    asyncio.run(ex._fetch_tools_async())
    check("h  _fetch_tools_async merges managed+external into _tool_server_map",
          E._tool_server_map.get("core_tool") == "kirocrew-core"
          and E._tool_server_map.get("ext_tool") == "srvX",
          f"map={E._tool_server_map}")
    check("h2 _mcp_tool_names covers both discovery paths",
          ex._mcp_tool_names == {"core_tool", "ext_tool"},
          f"names={sorted(ex._mcp_tool_names)}")
    check("h3 local handlers win name collisions and stay routable",
          ex.server_name_for("spawn_sub_agents") == ""
          and ex.server_name_for("ext_tool") == "srvX",
          f"spawn→{ex.server_name_for('spawn_sub_agents')!r} ext→{ex.server_name_for('ext_tool')!r}")

    # i. _trace_mcp_fail reaches provider._trace (lazy import)
    trace_path = P._TRACE_PATH
    E._trace_mcp_fail("srvA", "t_srvA", "boom", "stderr tail")
    wrote = Path(trace_path).exists() and "mcp_fail" in Path(trace_path).read_text(encoding="utf-8")
    check("i  _trace_mcp_fail writes an mcp_fail line via provider._trace", wrote,
          f"trace={trace_path}")

    print()
    print(f"# summary: {len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("# FAILED: " + "; ".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
