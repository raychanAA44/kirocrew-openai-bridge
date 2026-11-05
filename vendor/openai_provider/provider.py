"""OpenAIProvider — implements LLMProvider over any OpenAI-compatible API.

Maps OpenAI streaming chat-completion events → AcpEvent stream so KiroCrew's
TurnDriver / dashboard can consume it without modification.

Supported features:
  - text streaming          → EVENT_TEXT_CHUNK
  - tool_calls              → EVENT_TOOL_CALL + EVENT_PERMISSION_REQUEST
  - reasoning/thinking      → EVENT_THINKING_CHUNK  (o1/o3/deepseek-r1 style)
  - tool results            → EVENT_TOOL_RESULT
  - context usage           → context_usage_pct via prompt_tokens / context_window
  - cancel                  → asyncio.Event to abort in-flight stream

Config separation:
  - max_tokens      → output token limit per turn (sent as `max_tokens` in API call)
  - context_window  → total model context window (for usage reporting only)
                      auto-resolved from /v1/models at startup, overridable via
                      OPENAI_CONTEXT_WINDOW env var or constructor param.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re as _re
import threading
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from kiro_crew.acp.types import (
    AcpEvent,
    EVENT_TEXT_CHUNK,
    EVENT_THINKING_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_CALL_UPDATE,
    EVENT_TOOL_RESULT,
    EVENT_PERMISSION_REQUEST,
    EVENT_COMPLETE,
    EVENT_CLEAR_STATUS,
    STOP_REASON_END_TURN,
    STOP_REASON_CANCELLED,
    OPTION_ALLOW_ONCE,
    OPTION_ALLOW_ALWAYS,
    TurnUsage,
)
from kiro_crew.providers.base import LLMProvider, CancelOutcome

from ._config import (
    DEFAULT_BASE_URL,
    DEFAULT_COMPACTION_KEEP_RECENT,
    DEFAULT_COMPACTION_THRESHOLD,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    DEFAULT_TEMPERATURE,
    KNOWN_CONTEXT_WINDOWS,
    lookup_context_window,
    read_env_int,
    read_env_str,
)

logger = logging.getLogger(__name__)

#: How many trailing characters stay buffered while streaming, so an inline
#: tool-call block can still be recognised before it reaches the dashboard.
_STREAM_GUARD_CHARS = 160

#: Provider prefix KiroCrew puts in front of model ids (``hub-neo/<model>``).
_MODEL_PREFIX = "hub-neo/"


def _bare_model(model: str | None) -> str:
    """Strip the ``<provider>/`` prefix from a KiroCrew model id.

    The dashboard hands the provider ids like ``hub-neo/llama3:8b``; the OpenAI
    wire format wants the bare model name, so the prefix is removed once, on the
    way in.
    """
    name = (model or "").strip()
    return name[len(_MODEL_PREFIX):] if name.startswith(_MODEL_PREFIX) else name


# ── Shared HTTP client ───────────────────────────────────────────────────────
#
# A process-wide ``httpx.AsyncClient`` was tried and REVERTED: it saved the
# measured 29.9 ms of per-turn client setup, but the gateway instance running it
# died once with no traceback while two large streams were in flight, and a
# client created on one event loop is not safe to reuse if the loop is ever
# replaced. 30 ms is not worth an unexplained exit -- keep a client per request.


# ── Tool execution registry ──────────────────────────────────────────────────
# Holds pending tool calls waiting for KiroCrew's approve/reject signal.
# key = request_id (str), value = asyncio.Future[bool]
_PENDING_APPROVALS: dict[str, asyncio.Future] = {}


def _normalise_tool_calls(calls: "list[dict]") -> "list[dict]":
    """Return tool calls in the exact shape an OpenAI server accepts back.

    The stream hands over deltas that carry stream-only fields (``index``) and may
    omit ``type``. Echoing them verbatim makes strict servers lose the pairing
    between a call and its ``tool`` result, and the model then repeats the same
    call forever -- visible as a subagent spinning through dozens of identical
    turns.
    """
    out: list[dict] = []
    for call in calls or []:
        fn = call.get("function") or {}
        name = fn.get("name") or call.get("name") or ""
        args = fn.get("arguments")
        if args is None:
            args = "{}"
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        out.append(
            {
                "id": call.get("id") or "",
                "type": "function",
                "function": {"name": name, "arguments": args},
            }
        )
    return out

def _live_session_key(own: "str | None") -> str:
    """Identity to publish while a tool runs.

    The provider factory hands over ``session_key=None`` for dashboard chats, and
    KiroCrew attributes background work through mcp_core._resolve_session_key().
    Without a key spawn_run stored parent_session="" and the dashboard could not
    place the subagent. Fall back to the most recently opened slot.
    """
    key = str(own or "").strip()
    if key:
        return key
    try:
        path = os.path.join(os.path.expanduser("~"), ".kiro", "crew", "open_slots.json")
        with open(path, encoding="utf-8-sig") as fh:
            keys = json.load(fh).get("keys") or []
        if keys:
            return "dashboard:" + str(keys[-1])
    except Exception:  # noqa: BLE001
        pass
    return ""

_TRACE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "request_models.log"
)


#: Statuses worth one retry against the other endpoint. 503 is the public TLS
#: facade's non-blocking concurrency refusal, 429 its per-IP rate limit; the rest
#: are the usual transient upstream answers.
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})

# ── On-demand tool loading ───────────────────────────────────────────────────
#
# KiroCrew inlines its whole MCP tool set on every request: measured here 125
# tools / 143,184 characters ≈ 27k tokens, and the prefill over them costs
# +1501 ms per turn EVEN at a 99.8% prompt-cache hit. Only a handful are used in
# any given turn, so this sends a core set plus a `tool_search` loader and adds a
# tool to the wire once the model asks for it (matches stay active for the rest
# of the session).
#
# HUB_TOOL_DEFER=0 turns the whole thing off (the full set is then sent, exactly
# as before). A 400 that mentions tools/function switches it off for the session
# automatically and retries once, so a gateway that dislikes the loader cannot
# wedge a chat.
_TOOL_DEFER = os.environ.get("HUB_TOOL_DEFER", "1") != "0"

#: Sent on every turn while deferral is on: the file/code/search essentials plus
#: the two spawn/ask entry points a chat needs to stay useful. Everything else is
#: reachable through `tool_search`.
_CORE_TOOL_NAMES = frozenset({
    "read", "write", "edit", "list_dir", "find", "grep", "glob", "execute_bash",
    "web_fetch", "web_search",
    "kb_read", "kb_list", "kb_search", "kb_write",
    "memory_recall", "save_notes",
    "spawn_sub_agents", "ask_question",
})

_TOOL_SEARCH_NAME = "tool_search"

_TOOL_SEARCH_DEF = {
    "type": "function",
    "function": {
        "name": _TOOL_SEARCH_NAME,
        "description": (
            "Load more tools. Only the core tools are sent with each request. Pass "
            "`names` with exact tool names when you know them -- that always works. "
            "Otherwise pass `query`: single words, matched ONE BY ONE against tool "
            "names and descriptions (not as a phrase), so {\"query\": \"cron\"} and "
            "{\"query\": \"ledger monitor session\"} both work. Loaded tools stay "
            "callable for the rest of the session."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "A few separate words describing what you need to do. "
                        "Each word is matched on its own."
                    ),
                },
                "names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Exact tool names, when you already know them.",
                },
            },
        },
    },
}


def _tool_name(entry: dict) -> str:
    fn = entry.get("function") if isinstance(entry.get("function"), dict) else entry
    return str(fn.get("name") or "")


def _tool_description(entry: dict, limit: int = 200) -> str:
    fn = entry.get("function") if isinstance(entry.get("function"), dict) else entry
    text = " ".join(str(fn.get("description") or "").split())
    return text[:limit]


def _tool_params(entry: dict) -> str:
    fn = entry.get("function") if isinstance(entry.get("function"), dict) else entry
    params = fn.get("parameters") or {}
    props = params.get("properties") or {}
    required = set(params.get("required") or [])
    names = [
        f"{name}*" if name in required else name
        for name in props
    ]
    return ", ".join(names)


#: Words a tool query carries no signal in ("create the record for me").
_QUERY_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "for", "from",
    "i", "in", "is", "it", "me", "my", "of", "on", "or", "please", "so", "that",
    "the", "then", "this", "to", "tool", "tools", "use", "want", "with",
})

_WORD_RE = _re.compile(r"[a-z0-9]+")


def _words(text: str) -> list[str]:
    """Split *text* into lowercase words: snake, camel, punctuation all peel apart.

    A model asks for tools with a BAG of words ("work ledger record create bind
    session create send monitor_start"), so the unit of matching is the word, not
    the phrase. Words of one character are dropped here, which also retires
    "a"/"i" without a stopword entry each.
    """
    spaced = _re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(text or ""))
    return [
        word for word in _WORD_RE.findall(spaced.lower())
        if len(word) > 1 and word not in _QUERY_STOPWORDS
    ]


def _score_tools(terms: list[str], pool: list[dict]) -> list[tuple[int, str]]:
    """Rank tools by how many query words their name / description carry.

    A hit on the tool's own name is worth four times a hit in its description:
    "monitor" in ``monitor_start`` says much more than "monitor" in some
    description's prose. A shared prefix (``schedul`` vs ``schedule``) counts
    half, which is what lets a slightly misspelled word still land.
    """
    if not terms:
        return []
    scored: list[tuple[int, str]] = []
    for entry in pool:
        name = _tool_name(entry)
        if not name or name in _CORE_TOOL_NAMES:
            continue
        name_terms = set(_words(name))
        desc_terms = set(_words(_tool_description(entry, 4000)))
        score = 0
        for term in terms:
            if term in name_terms:
                score += 4
            elif any(t.startswith(term) or term.startswith(t) for t in name_terms):
                score += 2
            if term in desc_terms:
                score += 1
        if score:
            scored.append((score, name))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return scored


class _StreamedResponse:
    """``async with`` wrapper for a streamed ``httpx.Response``.

    ``client.stream()`` returns a context manager; ``client.send(stream=True)``
    returns the response alone, and this httpx build gives it no ``__aenter__``.
    Wrapping keeps the caller's ``async with ... as resp:`` shape (and therefore
    its indentation) while still closing the connection on every exit path.
    """

    def __init__(self, resp: Any) -> None:
        self._resp = resp

    async def __aenter__(self) -> Any:
        return self._resp

    async def __aexit__(self, *_exc: object) -> bool:
        try:
            await self._resp.aclose()
        except Exception:  # noqa: BLE001
            pass
        return False


def _trace(kind: str, **fields: object) -> None:
    """Append one line per turn: which model was asked for, which one answered.

    Kept in its own file because it answers exactly one question -- when the
    dashboard says model X, does X reach the wire and does the gateway answer as
    X. No prompt text, no secrets, and a write failure is never allowed to matter.
    """
    try:
        with open(_TRACE_PATH, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps({"t": time.strftime("%H:%M:%S"), "kind": kind, **fields},
                           ensure_ascii=False) + "\n"
            )
    except Exception:  # noqa: BLE001
        pass


def _crew_config() -> dict:
    """Read KiroCrew's own agent config (cheap, always current)."""
    try:
        with open(os.path.join(os.path.expanduser("~"), ".kiro", "crew", "config.json"),
                  encoding="utf-8-sig") as fh:
            return json.load(fh).get("agent") or {}
    except Exception:  # noqa: BLE001
        return {}


_CATALOG_PATH = os.path.join(
    os.path.expanduser("~"), ".config", "opencode", "opencode.json"
)
_CATALOG_CACHE: tuple[float, dict] = (0.0, {})

#: Models the gateway itself advertises, cached briefly. Separate from the file
#: catalog above: that one says what a model can do, this one says which models
#: exist right now, and only the gateway knows the second.
_AVAILABLE_CACHE: tuple[float, list] = (0.0, [])
_AVAILABLE_TTL = 60.0

#: Guards against a refresh stampede: several slots ask for the catalog within
#: the same second, and one background refresh answers all of them.
_AVAILABLE_REFRESH_LOCK = threading.Lock()

_SNAPSHOT_NAME = "models_snapshot.json"


def _snapshot_path() -> str:
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), _SNAPSHOT_NAME,
    )


def _snapshot_rows() -> list[dict[str, str]]:
    """Bare ids from the catalog snapshot ``install.py`` keeps on disk.

    Serving this first is what keeps a new session fast (the live fetch costs
    ~650 ms) AND what keeps the picker populated when the upstream is
    unreachable. It is the last list the gateway actually served, not a guess.
    """
    try:
        with open(_snapshot_path(), encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return []
    ids = payload.get("ids") if isinstance(payload, dict) else payload
    if not isinstance(ids, list):
        return []
    return [
        {"modelId": str(i).strip(), "name": str(i).strip()}
        for i in ids
        if isinstance(i, str) and str(i).strip()
    ]


def _fetch_available_rows(endpoints: list[str], api_key: str) -> list[dict[str, str]]:
    """One synchronous walk of *endpoints* for ``/models``; ``[]`` on failure.

    Primary first, then the optional fallback: when the public HTTPS entry is
    down but the local tunnel is alive (or the reverse), the picker still lists
    the real catalog instead of going blank.
    """
    import urllib.request

    for endpoint in endpoints:
        try:
            req = urllib.request.Request(
                f"{endpoint}/models",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=3) as resp:  # noqa: S310 - local gateway
                payload = json.loads(resp.read(4 << 20).decode("utf-8", "replace")) or {}
        except Exception:  # noqa: BLE001
            continue
        rows: list[dict[str, str]] = []
        for row in payload.get("data") or []:
            if not isinstance(row, dict):
                continue
            mid = str(row.get("id") or "").strip()
            # ``decision`` rows are router internals, not chat models; listing
            # them only offers the user a pick that cannot answer.
            if not mid or row.get("decision"):
                continue
            rows.append({"modelId": mid, "name": str(row.get("name") or mid)})
        if rows:
            return rows
    return []


def _refresh_available_models_async(provider: Any) -> None:
    """Refresh the catalog cache off-thread; at most one refresh in flight."""
    if not _AVAILABLE_REFRESH_LOCK.acquire(blocking=False):
        return

    def _run() -> None:
        global _AVAILABLE_CACHE
        try:
            rows = _fetch_available_rows(provider._endpoints(), provider._api_key)
            if rows:
                _AVAILABLE_CACHE = (time.time(), rows)
        except Exception:  # noqa: BLE001
            logger.debug("catalog refresh failed", exc_info=True)
        finally:
            _AVAILABLE_REFRESH_LOCK.release()

    try:
        threading.Thread(target=_run, name="hub-neo-models-refresh", daemon=True).start()
    except RuntimeError:  # noqa: BLE001 - interpreter shutting down
        _AVAILABLE_REFRESH_LOCK.release()


def _is_loopback(url: str) -> bool:
    """True for a URL that points at this machine (the local SSH tunnel)."""
    return "127.0.0.1" in url or "localhost" in url or "[::1]" in url


def _port_open(url: str, timeout: float = 0.25) -> bool:
    """True when *url*'s host:port accepts a TCP connection right now.

    A refused connect on loopback returns immediately, so this is a sub-
    millisecond test on Windows and never the 0.25 s budget it is given.
    """
    import socket
    import urllib.parse

    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if not host:
            return False
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


#: ``(stamped, key, chosen)`` for :meth:`OpenAIProvider._chat_base_url`.
_CHAT_BASE_CACHE: tuple[float, str, str] = (0.0, "", "")
_CHAT_PROBE_TTL = 5.0


def _catalog() -> dict:
    """Return ``{model_id: entry}`` from the hub-neo catalog, cached by mtime.

    That catalog is the same file the upstream gateway is configured from, so it
    -- not a table written here -- is what says which models exist, how large
    their window is, how much output they take, and which reasoning variants they
    expose. Guessing any of it is what produced the failures this replaces: an id
    the gateway did not know, answered by its default model, and an effort level
    it does not accept.
    """
    global _CATALOG_CACHE
    try:
        mtime = os.path.getmtime(_CATALOG_PATH)
    except OSError:
        return {}
    if _CATALOG_CACHE[0] == mtime:
        return _CATALOG_CACHE[1]
    try:
        with open(_CATALOG_PATH, encoding="utf-8-sig") as fh:
            models = (
                ((json.load(fh).get("provider") or {}).get("hub-neo") or {}).get("models") or {}
            )
        if not isinstance(models, dict):
            models = {}
    except Exception:  # noqa: BLE001
        models = {}
    _CATALOG_CACHE = (mtime, models)
    return models


def _catalog_entry(model: str) -> dict:
    """The catalog entry for *model*, keyed the way the upstream gateway sees it."""
    key = _bare_model(model)
    if not key:
        return {}
    models = _catalog()
    entry = models.get(key)
    if isinstance(entry, dict):
        return entry
    low = key.lower()
    for name, candidate in models.items():
        if isinstance(candidate, dict) and str(name).lower() == low:
            return candidate
    return {}


def _effort_levels_for(model: str) -> tuple[str, ...]:
    """The reasoning variants the catalog advertises for *model*, or () if none.

    No entry, or ``reasoning: false``, means unknown/unsupported, and unknown
    sends no field at all: the gateway answers an unsupported level with an error
    rather than silently downgrading it.
    """
    entry = _catalog_entry(model)
    if entry.get("reasoning") is False:
        return ()
    variants = entry.get("variants")
    if not isinstance(variants, dict):
        return ()
    return tuple(str(k).strip().lower() for k in variants if str(k).strip())


def _catalog_context(model: str) -> int:
    """The context window the catalog declares for *model*, or 0 if unknown."""
    limit = _catalog_entry(model).get("limit")
    if not isinstance(limit, dict):
        return 0
    try:
        return int(limit.get("context") or 0)
    except (TypeError, ValueError):
        return 0


def _catalog_output_cap(model: str) -> int:
    """The output-token ceiling the catalog declares for *model*, or 0 if unknown."""
    limit = _catalog_entry(model).get("limit")
    if not isinstance(limit, dict):
        return 0
    try:
        return int(limit.get("output") or 0)
    except (TypeError, ValueError):
        return 0


def _effort_for_model(model: str, explicit: "str | None") -> str:
    """The ``reasoning_effort`` to send for *model*, or "" to send none.

    *explicit* wins when the caller supplied one; otherwise KiroCrew's
    ``agent.reasoning_effort`` is used. Either way the value is dropped unless
    THIS model documents the level, so a chat-level setting never leaks onto a
    subagent route that would reject it.
    """
    allowed = _effort_levels_for(model)
    if not allowed:
        return ""
    want = str(explicit or "").strip().lower()
    if not want:
        want = str(_crew_config().get("reasoning_effort") or "").strip().lower()
    return want if want in allowed else ""


def _crew_model_context(model: str) -> int:
    """Context window KiroCrew recorded for this model, or 0 when it has none.

    KSWA's ``/v1/models`` carries no context field, so this table
    (``agent.model_context_tokens``) is the only per-model source there is.

    KiroCrew keys its table by the id the OPERATOR typed, which carries whatever
    channel prefix that route goes through (``hub-neo/oc/beam-free``,
    ``regptplus/gpt-6.1-sol``), while the served id we hold may be prefixed
    differently or not at all. An exact lookup therefore missed every model not
    spelled identically and fell back to a single global window, which is what made
    the usage meter report 128K for a model served at 1M. The trailing segment of
    the id is the model name in every spelling in use, so match on that as well,
    preferring the id with the most prefix segments in common.
    """
    table = _crew_config().get("model_context_tokens") or {}
    if not isinstance(table, dict):
        return 0
    for key in (model, "hub-neo/" + model):
        value = table.get(key)
        if isinstance(value, int) and value > 0:
            return value
    bare = model.rsplit("/", 1)[-1].strip()
    if not bare:
        return 0
    best: tuple[int, int] = (-1, 0)
    for key, value in table.items():
        if not isinstance(value, int) or value <= 0:
            continue
        if key.rsplit("/", 1)[-1].strip() != bare:
            continue
        # Same model name: prefer the entry whose prefix overlaps most with ours,
        # so an ``oc/`` route picks the ``hub-neo/oc/`` row over the plain one.
        shared = sum(
            1
            for a, b in zip(model.split("/")[:-1], key.split("/")[:-1])
            if a == b
        )
        if shared > best[0]:
            best = (shared, value)
    return best[1]


def _approval_timeout() -> float:
    """KiroCrew's own tool-approval timeout, so both sides agree."""
    try:
        seconds = float(_crew_config().get("tool_approval_timeout_secs"))
    except (TypeError, ValueError):
        seconds = 600.0
    return seconds if seconds > 0 else 600.0


def _approval_is_auto() -> bool:
    """True when KiroCrew is configured to auto-approve tool calls."""
    return str(_crew_config().get("approval_mode") or "").lower() in ("auto", "yolo", "bypass")


_STUB_TOKEN_ENV = "KIROCREW_STUB_SESSION_TOKEN"

_IMAGES_WARNED = False


def _warn_images_unsupported() -> None:
    """Say it once: this provider does not forward image attachments."""
    global _IMAGES_WARNED
    if _IMAGES_WARNED:
        return
    _IMAGES_WARNED = True
    logger.warning(
        "openai_provider: image attachments are not forwarded (allow_image is "
        "accepted, but the request body carries text only)"
    )


class OpenAIProvider(LLMProvider):
    """LLMProvider backed by an OpenAI-compatible chat-completions endpoint."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        fallback_base_url: str | None = None,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        system_prompt: str = "",
        max_tokens: int = DEFAULT_MAX_TOKENS,
        context_window: int | None = None,
        temperature: float | None = None,
        tool_executor: "ToolExecutor | None" = None,
        session_key: str | None = None,
        acp_backend: str = "",
        reasoning_effort: str | None = None,
        compaction_threshold: int = DEFAULT_COMPACTION_THRESHOLD,
        compaction_keep_recent: int = DEFAULT_COMPACTION_KEEP_RECENT,
    ) -> None:
        self._base_url = (base_url or read_env_str("OPENAI_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        # Optional second endpoint, tried once when the primary answers a retryable
        # status (429 rate limit / 503 "busy" from the public TLS facade) or drops
        # the connection. Deployments that reach the gateway over the public HTTPS
        # entry keep the local SSH tunnel here, so a burst that exceeds the facade's
        # concurrency cap falls back instead of failing the turn.
        self._fallback_base_url = (
            fallback_base_url or read_env_str("OPENAI_FALLBACK_BASE_URL")
        ).rstrip("/")
        self._api_key = api_key or read_env_str("OPENAI_API_KEY")
        self._model = _bare_model(model)
        self._system_prompt = system_prompt
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._tool_executor: ToolExecutor = tool_executor or DefaultToolExecutor()
        self._session_key = session_key
        # The ACP backend this bridge stands in for. KiroCrew asks
        # ``provider.capabilities.backend`` when it renders the effort picker, and
        # the value only has to name the harness whose capability set applies --
        # ``capabilities_for`` is pure and total for any string.
        self._acp_backend = acp_backend or "kiro"
        # Reasoning effort is a PER-MODEL capability, not a global switch: some
        # served models reject the field outright and one rejects ``none`` while
        # accepting every other level. ``_effort_for_model`` decides what this
        # model actually accepts, so a level configured for the chat model is never
        # sent to a subagent model that would 400 on it.
        self._reasoning_effort = _effort_for_model(self._model, reasoning_effort)
        # On-demand tool loading (see _CORE_TOOL_NAMES): the core set plus any tool
        # this session has already asked for is what goes on the wire.
        self._tool_defer = _TOOL_DEFER
        self._activated_tools: set[str] = set()
        self._tool_retry_done = False
        #: The full set the caller handed us this turn -- what _load_tools searches.
        self._all_tools: list[dict] = []

        # Context window for reporting (separate from max_tokens output limit)
        # If not provided, will be resolved from /v1/models in start()
        self._context_window_override = context_window  # explicit override (highest priority)
        self._context_window: int = context_window or 0  # final resolved value

        # Compaction settings
        self._compaction_threshold = compaction_threshold  # percent
        self._compaction_keep_recent = compaction_keep_recent  # messages to keep
        self._compacting: bool = False  # re-entrancy guard

        # Conversation history (maintained across turns)
        self._messages: list[dict[str, Any]] = []
        if system_prompt:
            self._messages.append({"role": "system", "content": system_prompt})

        # Context tracking
        self._context_pct: float = 0.0
        self._context_used: int = 0

        # Cancel support
        self._cancel_event: asyncio.Event = asyncio.Event()
        #: True while a turn is streaming -- steering is only accepted then.
        self._in_turn: bool = False
        #: completion tokens of the last turn (reported, not part of context used)
        self._completion_tokens: int = 0
        self._alive: bool = False

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._alive = True
        # Always resolve: KiroCrew's per-model table outranks the single window this
        # provider was configured with. Skipping this whenever an override existed is
        # what pinned EVERY model to one number -- the served model could be a 1M
        # route while the meter read the provider-wide default.
        await self._resolve_context_window()
        logger.info(
            "OpenAIProvider started (model=%s, base_url=%s, context_window=%d, max_tokens=%d)",
            self._model, self._base_url, self._context_window, self._max_tokens,
        )

    async def _resolve_context_window(self) -> None:
        """Resolve the model's context window for usage reporting.

        Resolution priority:
          1. KiroCrew's ``agent.model_context_tokens`` table for THIS model id
          2. Constructor ``context_window`` param (already set in ``__init__``)
          2. ``OPENAI_CONTEXT_WINDOW`` env var
          3. ``/v1/models`` endpoint (``context_window`` / ``context_length``)
          4. Built-in ``KNOWN_CONTEXT_WINDOWS`` map (exact, then prefix)
          5. Fallback to ``max_tokens``
        """
        import httpx

        # Priority 0: the upstream catalog's own per-model window. That file is
        # what the gateway is configured from, so it outranks every table kept on
        # this side -- a window that disagrees with it is simply wrong.
        catalog_ctx = _catalog_context(self._model)
        if catalog_ctx:
            self._context_window = catalog_ctx
            return

        # Priority 0.5: the table KiroCrew maintains per model
        # (agent.model_context_tokens). KSWA's /v1/models carries no context
        # field, so without this the constructor value always won.
        crew_ctx = _crew_model_context(self._model)
        if crew_ctx:
            self._context_window = crew_ctx
            return

        # Priority 1: explicit override (already set in __init__)
        if self._context_window > 0:
            return

        # Priority 2: env var
        env_ctx = read_env_int("OPENAI_CONTEXT_WINDOW")
        if env_ctx > 0:
            self._context_window = env_ctx
            logger.info(
                "Using context window from OPENAI_CONTEXT_WINDOW=%d",
                self._context_window,
            )
            return

        # Priority 3: query /v1/models
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{self._base_url}/models",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
                if resp.status_code == 200:
                    for m in resp.json().get("data", []):
                        if m.get("id") == self._model:
                            ctx = (
                                m.get("capabilities", {}).get("contextWindow")
                                or m.get("context_window")
                                or m.get("context_length")
                            )
                            if ctx and int(ctx) > 0:
                                self._context_window = int(ctx)
                                logger.info(
                                    "Resolved context window from /v1/models: "
                                    "%d (model=%s)",
                                    self._context_window, self._model,
                                )
                                return
                    logger.info(
                        "Model %s found in /v1/models but no context_window field",
                        self._model,
                    )
        except Exception as exc:
            logger.info("Could not query /v1/models: %s", exc)

        # Priority 4: built-in known models map
        known = lookup_context_window(self._model)
        if known is not None:
            self._context_window = known
            logger.info(
                "Using known context window for %s: %d",
                self._model, self._context_window,
            )
            return

        # Priority 5: fallback
        self._context_window = self._max_tokens
        logger.warning(
            "Could not determine context window for model=%s — "
            "falling back to max_tokens=%d. "
            "Set OPENAI_CONTEXT_WINDOW env var to override.",
            self._model, self._max_tokens,
        )

    async def shutdown(self) -> None:
        self._alive = False
        self._cancel_event.set()
        logger.info("OpenAIProvider shut down")

    def is_alive(self) -> bool:
        return self._alive

    # ── Context tracking ─────────────────────────────────────────────────────

    def context_usage_pct(self) -> float:
        return self._context_pct

    def context_used_tokens(self) -> int:
        return self._context_used

    #: This provider answers ``supports_effort()`` from the LIVE model, so KiroCrew
    #: may act on a False from it as a real "not supported" rather than a
    #: placeholder. Without this flag the dashboard treats the answer as "no
    #: information" and never offers the effort control at all.
    reports_effort_support = True

    def supports_effort(self) -> bool:
        """True when the model this provider serves accepts a reasoning level."""
        return bool(_effort_levels_for(self._model))

    def get_valid_effort_levels(self) -> list[str]:
        """The levels this model accepts, in the order the picker should show them."""
        return list(_effort_levels_for(self._model))

    def accepts_effort_level(self, level: str) -> bool:
        """Whether *level* would be sent for this model (used by the effort route).

        KiroCrew asks this before pushing a level live, so answering it here is
        what lets the dashboard change the level on a running session without
        tearing it down (a teardown rebuilt the session on the configured
        DEFAULT model, i.e. an effort tweak silently moved the chat onto a
        different, possibly paid, model).
        """
        want = str(level or "").strip().lower()
        return not want or want in _effort_levels_for(self._model)

    async def change_effort(self, level: str) -> bool:
        """Apply *level* to subsequent requests, live.

        Returns False for a level this model does not document, so KiroCrew takes
        its reset path instead of recording a level that would be dropped on the
        wire anyway. An empty *level* clears the override (model default).
        """
        want = str(level or "").strip().lower()
        allowed = _effort_levels_for(self._model)
        if want and want not in allowed:
            return False
        self._reasoning_effort = want
        return True

    @property
    def capabilities(self):
        """The ACP capability set of the backend this bridge stands in for.

        ``capabilities_for`` is pure and total, so an id this build cannot name
        fails closed instead of raising inside a request handler.
        """
        from kiro_crew.agent_sdk.capabilities import capabilities_for

        return capabilities_for(self._acp_backend)

    @property
    def client(self) -> "OpenAIProvider":
        """The object KiroCrew's ACP-shaped code calls into.

        Session setup reads ``provider.client._model`` and may call
        ``provider.client.set_model(...)``. This provider is its own client, so
        that seam is satisfied without inventing a second object to keep in sync.
        """
        return self

    async def clear_effort(self) -> bool:
        """Drop the explicit level so the model's own default applies."""
        self._reasoning_effort = ""
        return True

    def context_window_tokens(self) -> int:
        return self._context_window or self._max_tokens

    def _endpoints(self) -> list[str]:
        """The base URLs this provider may talk to, best-first.

        The CHAT endpoint is chosen per turn by :meth:`_chat_base_url` (local
        tunnel when it answers, public HTTPS otherwise); this list only orders
        the catalog walk and reuses that same choice first.
        """
        out: list[str] = []
        for candidate in (
            self._chat_base_url(),
            self._base_url,
            (getattr(self, "_fallback_base_url", "") or "").rstrip("/"),
        ):
            if candidate and candidate not in out:
                out.append(candidate)
        return out

    async def _open_stream(self, client: Any, payload: dict, headers: dict) -> Any:
        """Open the chat stream, trying the other endpoint once if it refuses.

        The public TLS facade caps concurrency at 16 and answers ``503 busy``
        immediately (no queueing), and it rate-limits at 300 req/60 s per IP. On
        a burst — subagents fanning out — the first attempt can therefore lose a
        race it would win a second later, while the local tunnel is free. The
        retry happens BEFORE a single byte is yielded, so it cannot duplicate
        output; a mid-stream failure is left alone (that one needs a real resume,
        not a silent replay).

        Every response is returned as a streamed one; the caller closes it.
        """
        import httpx

        endpoints = self._endpoints()
        last: Any = None
        for attempt, base in enumerate(endpoints[:2]):
            last_error: Exception | None = None
            try:
                request = client.build_request(
                    "POST",
                    f"{base}/chat/completions",
                    json=payload,
                    headers=headers,
                )
                resp = await client.send(request, stream=True)
            except httpx.TransportError as exc:
                resp, last_error = None, exc
            if resp is not None and resp.status_code == 200:
                return _StreamedResponse(resp)
            status = resp.status_code if resp is not None else "transport"
            if attempt + 1 < len(endpoints) and (
                last_error is not None or status in _RETRYABLE_STATUS
            ):
                if resp is not None:
                    try:
                        await resp.aclose()
                    except Exception:  # noqa: BLE001
                        pass
                _trace("retry", status=status, from_endpoint=base, reason="endpoint retry")
                last = resp
                continue
            if resp is None:
                raise last_error  # type: ignore[misc]
            return _StreamedResponse(resp)
        return _StreamedResponse(last) if last is not None else last

    def _routable_names(self) -> set[str]:
        """Tool names the executor can actually run, or an empty set if unknown.

        Empty means "no opinion" (the executor has not discovered anything yet),
        and the caller then advertises everything -- a stale empty answer must
        never strip the tool list down to nothing.
        """
        executor = getattr(self, "_tool_executor", None)
        getter = getattr(executor, "routable_names", None)
        if getter is None:
            return set()
        try:
            return set(getter() or ())
        except Exception:  # noqa: BLE001
            return set()

    def _wire_tools(self, tools: list[dict]) -> list[dict]:
        """The tool list to send: core + already-loaded, else the whole set.

        Deferral only pays when there is something to defer, so a set that is
        already small (or a provider with deferral switched off) passes through
        untouched -- including when the model answered a 400 earlier.
        """
        if not self._tool_defer or not tools:
            return tools
        # Advertise only what can be executed. KiroCrew offers its whole set
        # every turn, and a name with no handler and no owning server (kb_read
        # before obsidian-kb answered, save_notes before kirocrew-mate did) can
        # only ever come back as an error -- so the model spends a tool call to
        # learn what this filter already knows. Filtering never empties the set.
        routable = self._routable_names()
        if routable:
            kept = [t for t in tools if _tool_name(t) in routable]
            if kept and len(kept) < len(tools):
                _trace(
                    "tools_routable",
                    kept=len(kept),
                    of=len(tools),
                    dropped=",".join(
                        sorted(_tool_name(t) for t in tools
                               if _tool_name(t) not in routable)
                    )[:400],
                )
                tools = kept
        core = [t for t in tools if _tool_name(t) in _CORE_TOOL_NAMES]
        loaded = [
            t for t in tools
            if _tool_name(t) in self._activated_tools and _tool_name(t) not in _CORE_TOOL_NAMES
        ]
        if not core:
            # Names this build does not know: never guess a subset.
            return tools
        if len(core) + len(loaded) >= len(tools):
            return tools
        sent = core + loaded + [_TOOL_SEARCH_DEF]
        _trace(
            "tools",
            sent=len(sent),
            of=len(tools),
            chars=sum(len(json.dumps(t, ensure_ascii=False)) for t in sent),
        )
        return sent

    def _load_tools(self, names: list[str], query: str) -> str:
        """Activate tools named in *names* or matched by *query*; say what loaded.

        Two ways in, and the exact names always win: a model that already knows
        the tool must not be second-guessed by word matching. The query path is
        deliberately word-by-word -- an earlier contiguous-substring test missed
        every real query a model sent, which is how "No tool matched" became a
        dead end instead of a tool call.
        """
        pool = self._all_tools or []
        by_name = {_tool_name(t): t for t in pool}
        folded = {name.lower(): name for name in by_name}
        chosen: list[str] = []
        for raw in names:
            raw = str(raw or "").strip()
            # A model writes names as it saw them in prose ("Monitor_Start") or
            # fully qualified ("mcp__kirocrew-core__monitor_start"); either one
            # must land, because failing an explicit request is the worst answer.
            if "__" in raw:
                raw = raw.rsplit("__", 1)[-1] or raw
            name = raw if raw in by_name else folded.get(raw.lower(), "")
            if name and name not in chosen:
                chosen.append(name)
        if query:
            # Repeated words would score the same tool twice ("create" twice in
            # one real query moved an unrelated cron tool into the top eight).
            terms = list(dict.fromkeys(_words(query)))
            matched = _score_tools(terms, pool)
            for _, name in matched[:8]:
                if name not in chosen:
                    chosen.append(name)
        else:
            matched = []
        if len(chosen) > 24:
            chosen = chosen[:24]
        fresh = [name for name in chosen if name not in self._activated_tools]
        self._activated_tools.update(chosen)
        _trace(
            "tools_loaded",
            query=str(query or "")[:160],
            candidates=len(matched),
            names=",".join(chosen)[:200],
            fresh=len(fresh),
        )
        if not chosen:
            available = sorted(
                name for name in by_name
                if name and name not in _CORE_TOOL_NAMES
            )
            head = available[:40]
            return "\n".join([
                "No tool matched those words, so nothing was loaded.",
                f"{len(available)} more tools are available. Call tool_search again "
                f"with names: [\"<exact name>\"], or with different words -- words "
                f"are matched against the English tool names and descriptions.",
                ", ".join(head) + (" ..." if len(available) > len(head) else ""),
            ])
        lines = ["Loaded (callable from now on):"]
        for name in chosen:
            entry = by_name[name]
            params = _tool_params(entry)
            lines.append(
                f"- {name}({params})" if params else f"- {name}"
            )
            desc = _tool_description(entry)
            if desc:
                lines.append(f"  {desc}")
        return "\n".join(lines)

    def _chat_base_url(self) -> str:
        """Which endpoint this turn's chat request goes to.

        Two endpoints, opposite trade-offs. The local SSH tunnel reaches the
        gateway one hop closer, and an alternating A/B (same payload, same free
        model, n=5 each) measured it ahead by 365 ms of TTFB on a short prompt
        and 581 ms on a ~20k-token one -- the facade terminates TLS, rewrites
        Host and re-forwards. The public endpoint, though, is the DURABLE one --
        it needs no OPS/control.mjs process to be alive, and a cold DNS lookup
        there can cost seconds.

        So the tunnel wins whenever it actually answers, and the public endpoint
        is the fallback the moment it does not. The probe is a loopback TCP
        connect (~0.2 ms) cached for 5 s, i.e. re-decided roughly once per turn,
        which is what makes "OPS closed mid-session" degrade to the slower-but-
        working path instead of failing the turn.
        """
        local = (getattr(self, "_fallback_base_url", "") or "").rstrip("/")
        if not local or local == self._base_url or not _is_loopback(local):
            return self._base_url
        global _CHAT_BASE_CACHE
        key = f"{self._base_url}|{local}"
        stamped, cached_key, chosen = _CHAT_BASE_CACHE
        now = time.time()
        if cached_key == key and now - stamped < _CHAT_PROBE_TTL and chosen:
            return chosen
        chosen = local if _port_open(local) else self._base_url
        _CHAT_BASE_CACHE = (now, key, chosen)
        return chosen

    def available_models(self) -> list[dict[str, str]]:
        """The models the gateway actually serves, for KiroCrew's picker.

        Without this the picker falls back to a catalog from elsewhere, which is
        how a pick could name a model the gateway does not have -- and such a pick
        is answered by the gateway's DEFAULT model, i.e. the UI says one thing and
        another model answers.

        Ids are the gateway's own BARE ids, the same spelling ``/api/models``
        serves (install._fetch_upstream_models) and the same one
        ``model_registry.to_acp_id`` sends on the wire. They used to carry the
        ``hub-neo/`` provider prefix, which broke KiroCrew's entitlement check:
        ``acp.runtime_models.model_is_unusable`` is a literal membership test, so
        every BARE pin (``ollama/deepseek-v4.1-flash:1017``, ``oc/beam-free``)
        came back unusable and the UI announced "… isn't offered right now" while
        the gateway served it happily.

        Synchronous by design: the callers are sync accessors (KiroCrew's slot
        entitlements, its wire-id fold), the listener is local, and a stale
        answer beats blocking a request handler on a retry. A stale-but-known
        answer therefore wins over a fresh one: this method returns the last
        good list at once and refreshes it on a daemon thread, so a session
        never waits ~650 ms for a catalog it already has on disk.
        """
        global _AVAILABLE_CACHE
        now = time.time()
        stamped, cached = _AVAILABLE_CACHE
        if stamped and now - stamped < _AVAILABLE_TTL and cached:
            return cached

        seeded = cached or _snapshot_rows()
        if seeded:
            _refresh_available_models_async(self)
            return seeded

        rows = _fetch_available_rows(self._endpoints(), self._api_key)
        if rows:
            _AVAILABLE_CACHE = (now, rows)
            return rows
        # Nothing local and nothing reachable: the file catalog is still better
        # than an empty picker (and this path is first-run only).
        return [{"modelId": k, "name": k} for k in sorted(_catalog())]

    @property
    def served_model(self) -> str:
        return self._model

    # ── Auto-compaction ─────────────────────────────────────────────────────

    async def _maybe_compact(self) -> None:
        """Summarize old messages when context usage exceeds the threshold.

        When the context window is nearly full, this method:
          1. Preserves the system prompt and the last ``compaction_keep_recent``
             messages (recent conversation).
          2. Sends the middle messages to the same (or cheaper) model to produce
             a concise summary.
          3. Replaces the middle messages with a single summary message.

        The compaction is transparent to the user — a ``THINKING_CHUNK`` event
        is emitted so the dashboard shows "Auto-compacting…" in the thinking
        block, but no text content leaks into the main conversation.
        """
        # Guard: don't compact if already compacting or threshold is 0 (disabled)
        if self._compacting or self._compaction_threshold <= 0:
            return

        ctx_win = self._context_window or self._max_tokens
        if ctx_win <= 0:
            return

        # Estimate current usage: count tokens in all messages (rough: 4 chars ≈ 1 token)
        total_chars = sum(len(str(m.get("content", ""))) + len(str(m.get("tool_calls", ""))) for m in self._messages)
        # 中文约 1.7 字符/token，按 4 字符/token 会低报约 2.7 倍，压缩触发太晚。
        # 有真实 usage 时直接用最近一次的 prompt_tokens（_context_used 只含输入）。
        estimated_tokens = int(self._context_used) or int(total_chars / 1.7)
        pct = min(estimated_tokens / ctx_win * 100, 100.0)

        if pct < self._compaction_threshold:
            return

        # Need compaction — figure out what to keep vs summarize
        # Always keep: system prompt (index 0 if present)
        has_system = self._messages and self._messages[0].get("role") == "system"
        offset = 1 if has_system else 0

        # Must have enough messages to compact
        if len(self._messages) < offset + self._compaction_keep_recent + 2:
            return

        keep_start = len(self._messages) - self._compaction_keep_recent
        old_messages = self._messages[offset:keep_start]
        if not old_messages:
            return

        self._compacting = True
        logger.info(
            "Auto-compaction triggered: %.1f%% context used (%d/%d tokens), "
            "compacting %d old messages, keeping %d recent",
            pct, estimated_tokens, ctx_win, len(old_messages), self._compaction_keep_recent,
        )

        try:
            summary = await self._summarize_for_compaction(old_messages)
            if not summary:
                logger.warning("Compaction summarization returned empty — skipping")
                return

            # Build compacted history: [system?] + [summary] + [recent messages]
            summary_msg = {
                "role": "system",
                "content": (
                    "[Auto-compacted conversation summary]\n"
                    "The following is a summary of earlier conversation messages "
                    "that were compressed to free up context space:\n\n"
                    f"{summary}"
                ),
            }
            new_messages: list[dict[str, Any]] = []
            if has_system:
                new_messages.append(self._messages[0])
            new_messages.append(summary_msg)
            new_messages.extend(self._messages[keep_start:])

            removed_count = len(self._messages) - len(new_messages)
            self._messages = new_messages
            logger.info(
                "Auto-compaction complete: %d messages removed, %d remaining "
                "(+1 summary)",
                removed_count, len(self._messages) - 1,
            )
        except Exception as exc:
            logger.warning("Auto-compaction failed: %s — continuing without compaction", exc)
        finally:
            self._compacting = False

    async def _summarize_for_compaction(self, messages: list[dict]) -> str:
        """Call the API (non-streaming) to summarize old messages."""
        import httpx

        # Build a summarization prompt
        # Flatten the messages into a readable transcript
        transcript_parts: list[str] = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            tool_calls = msg.get("tool_calls")

            if role == "tool":
                # Tool result — include truncated
                transcript_parts.append(f"[Tool result]: {str(content)[:500]}")
            elif tool_calls:
                # Assistant tool-call turn
                names = [tc.get("function", {}).get("name", "?") for tc in tool_calls]
                transcript_parts.append(f"[Assistant called tools: {', '.join(names)}]")
            elif content:
                # Normal text message
                transcript_parts.append(f"[{role}]: {str(content)[:1000]}")
            else:
                transcript_parts.append(f"[{role}]: (empty)")

        transcript = "\n".join(transcript_parts)

        summarization_messages = [
            {
                "role": "system",
                "content": (
                    "You are a conversation summarizer. Produce a concise but "
                    "complete summary of the conversation below. Preserve:\n"
                    "- Key decisions and conclusions\n"
                    "- Code changes made (file paths, what was changed)\n"
                    "- Tool calls and their outcomes\n"
                    "- User preferences expressed\n"
                    "- Open questions or pending tasks\n"
                    "Keep the summary under 2000 characters. Output ONLY the summary, "
                    "no preamble."
                ),
            },
            {
                "role": "user",
                "content": f"Summarize this conversation:\n\n{transcript}",
            },
        ]

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": summarization_messages,
            "max_tokens": 800,
            "temperature": 0.1,
            "stream": False,
        }

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{self._chat_base_url()}/chat/completions",
                json=payload,
                headers=headers,
            )
            if resp.status_code != 200:
                body = await resp.aread()
                raise RuntimeError(
                    f"Compaction API error {resp.status_code}: {body.decode()[:300]}"
                )

            data = resp.json()
            choices = data.get("choices", [])
            if not choices:
                return ""
            return choices[0].get("message", {}).get("content", "")

    # ── Manual /compact command ─────────────────────────────────────────────

    async def compact(self, context: str = "") -> None:
        """Handle the /compact slash command.

        Summarizes the entire conversation history (except the system prompt)
        using the same summarization API, then replaces all messages with the
        summary.  This is the manual version of auto-compaction — triggered
        explicitly by the user typing ``/compact`` in the dashboard.
        """
        if self._compacting:
            logger.warning("compact() called while auto-compaction is in progress — skipping")
            return

        has_system = self._messages and self._messages[0].get("role") == "system"
        offset = 1 if has_system else 0

        # Nothing to compact
        if len(self._messages) <= offset + 1:
            logger.info("compact(): not enough messages to compact (%d)", len(self._messages))
            return

        # All messages except system prompt
        old_messages = self._messages[offset:]
        if not old_messages:
            return

        self._compacting = True
        logger.info("compact() triggered: summarizing %d messages", len(old_messages))

        try:
            summary = await self._summarize_for_compaction(old_messages)
            if not summary:
                logger.warning("compact(): summarization returned empty — skipping")
                return

            # Add user's context hint if provided
            if context:
                summary = f"{summary}\n\n[User-specified context to preserve: {context}]"

            summary_msg = {
                "role": "system",
                "content": (
                    "[Compacted conversation summary]\n"
                    "The full conversation history was compressed into this summary:\n\n"
                    f"{summary}"
                ),
            }

            new_messages: list[dict[str, Any]] = []
            if has_system:
                new_messages.append(self._messages[0])
            new_messages.append(summary_msg)

            removed_count = len(self._messages) - len(new_messages)
            self._messages = new_messages
            logger.info(
                "compact() complete: %d messages removed, %d remaining",
                removed_count, len(self._messages),
            )
        except Exception as exc:
            logger.warning("compact() failed: %s", exc)
        finally:
            self._compacting = False

    async def wait_for_compaction(self, timeout: float = 30.0) -> dict:
        """Wait for compaction to complete.

        Since OpenAI compaction is synchronous (happens within the compact() call),
        this returns immediately with the result status.
        """
        return {"type": "completed"}

    # ── Approval (tool permission) ────────────────────────────────────────────

    async def approve_tool(self, request_id: str | int, *, always: bool = False) -> None:
        key = str(request_id)
        fut = _PENDING_APPROVALS.get(key)
        if fut and not fut.done():
            fut.set_result(True)

    async def reject_tool(self, request_id: str | int) -> None:
        key = str(request_id)
        fut = _PENDING_APPROVALS.get(key)
        if fut and not fut.done():
            fut.set_result(False)

    # ── Cancel ───────────────────────────────────────────────────────────────

    async def cancel(self, *, wait_ack_timeout: float = 0.0) -> CancelOutcome:
        self._cancel_event.set()
        return "acked"

    # ── Main stream ──────────────────────────────────────────────────────────

    async def set_model(self, model_id: str) -> None:
        """Switch the model for the rest of the session.

        KiroCrew's dashboard model picker reaches the provider through this
        method (``slot_model._try_live_model_switch`` / ``acp_model_effort``).
        Without it a pick in the UI is silently dropped and every request keeps
        going to the model that was current when the session was created.
        """
        bare = _bare_model(model_id)
        if bare:
            self._model = bare

    def _ensure_stub_token(self, session_key: str) -> str:
        """Mint (once per session key) the stub token that attests this session."""
        if not session_key:
            return ""
        cached = getattr(self, "_stub_token", None)
        if cached and getattr(self, "_stub_token_key", "") == session_key:
            return cached
        try:
            from kiro_crew.mcp_gateway.claim import mint_stub_session_token

            token = mint_stub_session_token()
        except Exception:  # noqa: BLE001
            return ""
        self._stub_token = token
        self._stub_token_key = session_key
        return token
    async def steer(self, message: str) -> bool:
        """Inject a message into the turn that is currently running.

        KiroCrew's steering (the dashboard's steer action and the ``spawn_steer``
        tool) calls this, and reports ``steer rejected by provider`` when the method
        is missing or returns False. The HTTP request already in flight cannot be
        amended, so the text is appended to the conversation and is picked up by the
        next tool-loop iteration of the running turn. Returns False when no turn is
        running -- there is nothing to steer into and the caller should queue it.
        """
        text = (message or "").strip()
        if not text or not self._in_turn:
            return False
        self._messages.append({"role": "user", "content": text})
        return True

    def has_active_turn(self) -> bool:
        """Whether a turn is in flight (KiroCrew asks before live operations)."""
        return self._in_turn

    async def stream(self, message: str, **kwargs: object) -> AsyncIterator[AcpEvent]:  # type: ignore[override]
        """Send a user message and yield AcpEvents until end_turn or cancel.

        ``**kwargs`` absorbs the extra call-site arguments current KiroCrew
        builds pass (``allow_image``, ...). Without it every turn dies with
        ``TypeError: stream() got an unexpected keyword argument``.
        """
        if kwargs.get("allow_image"):
            _warn_images_unsupported()
        self._cancel_event.clear()
        self._in_turn = True
        self._messages.append({"role": "user", "content": message})
        try:
            # Auto-compact if context usage is high
            await self._maybe_compact()

            # Tool definitions from executor. Discovery is async and, when called
            # from inside a running loop, ``tool_definitions()`` only *schedules* it
            # and returns an empty list -- which handed the model a tool-less prompt
            # on the first turn of every session. Await the discovery first.
            fetch = getattr(self._tool_executor, "_fetch_tools_async", None)
            if fetch is not None:
                try:
                    await fetch()
                except Exception:  # noqa: BLE001
                    pass
            tools = self._tool_executor.tool_definitions()

            async for event in self._run_turn(tools):
                if self._cancel_event.is_set():
                    yield AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_CANCELLED)
                    return
                yield event
        finally:
            # Steering is only accepted while this is True; a turn that ends (or is
            # cancelled, or whose generator is closed) must stop accepting steers.
            self._in_turn = False

    async def _run_turn(self, tools: list[dict]) -> AsyncIterator[AcpEvent]:
        """Inner turn loop — handles tool call cycles.

        Text chunks are BUFFERED and only flushed to the UI once we know the
        finish_reason is 'stop' (not 'tool_calls').  This prevents the raw
        assistant preamble / tool-call XML from leaking into the dashboard
        when the model outputs text before deciding to call a tool.

        Thinking/reasoning chunks are forwarded immediately — they display in
        the collapsible thinking block and do not pollute the main message.
        """
        while True:
            tool_calls_made: list[dict] = []
            finish_reason = "stop"
            # Buffer for text chunks — flushed only when finish_reason != tool_calls
            text_buffer: list[AcpEvent] = []
            # Non-text, non-internal events (tool_call/result events from _stream_completion)
            other_events: list[AcpEvent] = []

            async for event in self._stream_completion(tools):
                if event.kind == "_tool_calls_collected":
                    tool_calls_made = json.loads(event.text)
                    continue
                if event.kind == "_finish_reason":
                    finish_reason = event.text
                    continue
                # Thinking chunks: forward immediately (goes to collapsible block, not main text)
                if event.kind == EVENT_THINKING_CHUNK:
                    yield event
                    continue
                # Text chunks: emit as they arrive, holding back a small tail so an
                # inline tool-call block still has a chance to be recognised before it
                # reaches the dashboard. Buffering the entire turn here made a genuinely
                # streaming upstream look non-streaming: the gateway reported first
                # token 1.08 s / total 10.46 s, yet the UI only got text at the end.
                if event.kind == EVENT_TEXT_CHUNK:
                    text_buffer.append(event)
                    pending = "".join(e.text for e in text_buffer)
                    # Only hold text that could still turn into an inline tool call.
                    # Those start with "<tool_call>" or a ```json fence, so any chunk
                    # carrying "<" or a backtick stays buffered. Plain prose is
                    # released at once: holding everything until 160 characters made
                    # every short answer arrive in a single piece at the very end --
                    # a two-character reply was measured at 520 ms of perceived
                    # latency it did not have.
                    suspicious = ("<" in pending) or ("`" in pending)
                    if suspicious:
                        if len(pending) > _STREAM_GUARD_CHARS:
                            tail = pending[-_STREAM_GUARD_CHARS:]
                            safe = pending[:-_STREAM_GUARD_CHARS]
                            text_buffer = [AcpEvent(kind=EVENT_TEXT_CHUNK, text=tail)]
                            if safe:
                                yield AcpEvent(kind=EVENT_TEXT_CHUNK, text=safe)
                        continue
                    if pending:
                        yield AcpEvent(kind=EVENT_TEXT_CHUNK, text=pending)
                        text_buffer = []
                    continue
                # Everything else (rare in _stream_completion): buffer too
                other_events.append(event)

            if self._cancel_event.is_set():
                yield AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_CANCELLED)
                return

            if finish_reason == "tool_calls" and tool_calls_made:
                # Discard text_buffer — it was preamble before the tool call, not real output.
                # Also check for text-format tool calls embedded in the buffer (some models).
                text_tool_calls = _extract_text_tool_calls(text_buffer)
                if text_tool_calls:
                    tool_calls_made = text_tool_calls

                # Flush any non-text events that should still be visible
                for ev in other_events:
                    yield ev

                # Execute each tool call, yield events, add results to history
                tool_results = []
                for tc in tool_calls_made:
                    async for ev in self._handle_tool_call(tc):
                        yield ev
                        if ev.kind == EVENT_TOOL_RESULT:
                            tool_results.append({
                                "tool_call_id": tc["id"],
                                "role": "tool",
                                "content": ev.tool_output if isinstance(ev.tool_output, str) else json.dumps(ev.tool_output, ensure_ascii=False),
                            })
                # Add assistant tool-call turn + results to history
                self._messages.append({
                    "role": "assistant", "content": "", "tool_calls": _normalise_tool_calls(tool_calls_made),
                })
                self._messages.extend(tool_results)
                # Loop: send results back to model
                continue

            # --- finish_reason == "stop" (or other non-tool terminal) ---
            # Check if the text buffer contains text-format tool calls (fallback for models
            # that don't support structured tool_calls but output XML/JSON inline).
            text_tool_calls = _extract_text_tool_calls(text_buffer)
            if text_tool_calls:
                # Model used text-format tool calling — handle it the same way
                tool_calls_made = text_tool_calls
                for ev in other_events:
                    yield ev

                tool_results = []
                for tc in tool_calls_made:
                    async for ev in self._handle_tool_call(tc):
                        yield ev
                        if ev.kind == EVENT_TOOL_RESULT:
                            tool_results.append({
                                "tool_call_id": tc["id"],
                                "role": "tool",
                                "content": ev.tool_output if isinstance(ev.tool_output, str) else json.dumps(ev.tool_output, ensure_ascii=False),
                            })
                self._messages.append({"role": "assistant", "content": "", "tool_calls": _normalise_tool_calls(tool_calls_made)})
                self._messages.extend(tool_results)
                continue

            # Normal text response — flush buffer to UI now
            for ev in text_buffer:
                yield ev
            for ev in other_events:
                yield ev

            yield AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
            return

    async def _stream_completion(self, tools: list[dict]) -> AsyncIterator[AcpEvent]:
        """Call the API with streaming and yield events."""
        import httpx  # deferred — only installed in openai_provider env

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": self._messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        # The catalog's per-model output ceiling is the real limit; asking for
        # more than a route allows is rejected, not clamped, by the gateway.
        _out_cap = _catalog_output_cap(self._model)
        _max_out = self._max_tokens
        if _out_cap and _max_out and _max_out > _out_cap:
            _max_out = _out_cap
        if _max_out:
            payload["max_tokens"] = _max_out
        if self._temperature is not None:
            payload["temperature"] = self._temperature
        # Per-model: "" means this route takes no effort field (see _effort_for_model).
        if self._reasoning_effort:
            payload["reasoning_effort"] = self._reasoning_effort
        if tools:
            # Deferral rewrites this list (see _wire_tools); keep the full set so
            # `tool_search` can reach everything the caller offered.
            self._all_tools = tools
            payload["tools"] = self._wire_tools(tools)
            payload["tool_choice"] = "auto"

        # An empty key used to surface as httpx's "Illegal header value b'Bearer '"
        # from inside header assembly, which says nothing about the missing key or
        # where it should have come from. Fail here, in words, first.
        if not str(self._api_key or "").strip():
            raise RuntimeError(
                "no API key configured for the OpenAI-compatible provider: set "
                "api_key in provider.json (install.ps1 writes one), or export "
                "OPENAI_API_KEY, or let KiroCrew's provider entry supply it"
            )

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        # Accumulate tool calls across chunks
        accumulated_tool_calls: dict[int, dict] = {}
        _served_logged = False
        _trace(
            "request",
            requested=payload["model"],
            effort=self._reasoning_effort,
            session=self._session_key,
        )
        finish_reason = "stop"
        assistant_text = ""

        async with httpx.AsyncClient(timeout=120.0) as client:
            async with await self._open_stream(client, payload, headers) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    if (
                        resp.status_code == 400
                        and self._tool_defer
                        and not self._tool_retry_done
                        and b"tool" in body.lower()
                    ):
                        # A gateway that rejects the deferred list must not wedge the
                        # chat: drop back to the full set and re-run this request once.
                        self._tool_retry_done = True
                        self._tool_defer = False
                        _trace(
                            "tool_defer_off",
                            status=resp.status_code,
                            reason=body[:160].decode("utf-8", "replace"),
                        )
                        async for _ev in self._stream_completion(tools):
                            yield _ev
                        return
                    raise RuntimeError(
                        f"OpenAI API error {resp.status_code}: {body.decode()[:500]}"
                    )

                async for line in resp.aiter_lines():
                    if self._cancel_event.is_set():
                        # Close the upstream response so we stop pulling tokens that
                        # the UI will never show. (The provider may still bill what
                        # it already generated -- that is server-side.)
                        try:
                            await resp.aclose()
                        except Exception:  # noqa: BLE001
                            pass
                        return

                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        break

                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    # Which model actually answered. The dashboard can show one
                    # model while the wire carries another; recording both sides
                    # once per response is what tells those two apart.
                    if not _served_logged:
                        _served_logged = True
                        _trace(
                            "served",
                            requested=payload["model"],
                            served=chunk.get("model"),
                            session=self._session_key,
                            tools=len(payload.get("tools") or []),
                            pt=(chunk.get("usage") or {}).get("prompt_tokens"),
                        )

                    # Usage (last chunk on many providers)
                    if usage := chunk.get("usage"):
                        pt = int(usage.get("prompt_tokens") or 0)
                        ct = int(usage.get("completion_tokens") or 0)
                        # Which tool list this turn actually paid for: deferral's
                        # whole claim is that `pt` drops, so record both numbers.
                        _trace(
                            "usage",
                            requested=payload["model"],
                            pt=pt,
                            ct=ct,
                            tools=len(payload.get("tools") or []),
                            session=self._session_key,
                        )
                        # "Context used" is what the NEXT request has to carry: the
                        # prompt. Adding completion_tokens reported the model's own
                        # output as if it occupied the window, so the meter ran ahead
                        # of reality by one answer every turn.
                        self._context_used = pt
                        self._completion_tokens = ct
                        # Use resolved context_window for percentage calc (not max_tokens!)
                        ctx_win = self._context_window or self._max_tokens
                        if ctx_win:
                            self._context_pct = min(pt / ctx_win * 100, 100.0)

                    choices = chunk.get("choices", [])
                    if not choices:
                        continue

                    delta = choices[0].get("delta", {})
                    fr = choices[0].get("finish_reason")
                    if fr:
                        finish_reason = fr

                    # Thinking / reasoning tokens (o1, deepseek-r1, mimo, etc.)
                    # Field name varies by provider: reasoning_content (most), thinking (Anthropic-style)
                    thinking = delta.get("reasoning_content") or delta.get("thinking") or delta.get("reasoning")
                    if thinking:
                        yield AcpEvent(kind=EVENT_THINKING_CHUNK, text=thinking)

                    # Text content
                    content = delta.get("content") or ""
                    if content:
                        assistant_text += content
                        yield AcpEvent(kind=EVENT_TEXT_CHUNK, text=content)

                    # Tool call deltas (field may be null on some providers)
                    for tc_delta in (delta.get("tool_calls") or []):
                        idx = tc_delta.get("index", 0)
                        if idx not in accumulated_tool_calls:
                            accumulated_tool_calls[idx] = {
                                "id": "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        tc = accumulated_tool_calls[idx]
                        if tc_id := tc_delta.get("id"):
                            tc["id"] += tc_id
                        if fn := tc_delta.get("function", {}):
                            tc["function"]["name"] += fn.get("name") or ""
                            tc["function"]["arguments"] += fn.get("arguments") or ""

        # Add assistant text to history if no tool calls
        if assistant_text and not accumulated_tool_calls:
            self._messages.append({"role": "assistant", "content": assistant_text})

        # Signal collected tool calls via internal event
        if accumulated_tool_calls:
            tool_list = list(accumulated_tool_calls.values())
            yield AcpEvent(kind="_tool_calls_collected", text=json.dumps(tool_list))

        yield AcpEvent(kind="_finish_reason", text=finish_reason)

    async def _handle_tool_call(self, tc: dict) -> AsyncIterator[AcpEvent]:
        """Emit permission_request → (approved?) → execute → tool_result."""
        fn_name = tc["function"]["name"]
        try:
            fn_args = json.loads(tc["function"]["arguments"] or "{}")
        except json.JSONDecodeError:
            fn_args = {}

        tool_call_id = tc.get("id") or str(uuid.uuid4())
        request_id = str(uuid.uuid4())

        # ── On-demand tool loading: the loader itself, and the guard for a tool
        # the model named but was never sent (a hallucinated or remembered name).
        # Both answer as ordinary tool results so the model can correct course in
        # the same turn instead of the request failing on an unknown function.
        if fn_name == _TOOL_SEARCH_NAME:
            yield AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id=tool_call_id,
                title="tool_search",
                tool_name=fn_name,
                tool_purpose="load tools",
                tool_input=json.dumps(fn_args, ensure_ascii=False),
                mcp_server_name="",
            )
            text = self._load_tools(
                fn_args.get("names") or [], str(fn_args.get("query") or ""),
            )
            yield AcpEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id=tool_call_id,
                tool_output=text,
                tool_final=True,
                mcp_server_name="",
            )
            return
        if (
            self._tool_defer
            and self._all_tools
            and fn_name not in _CORE_TOOL_NAMES
            and fn_name not in self._activated_tools
            and any(_tool_name(t) == fn_name for t in self._all_tools)
        ):
            yield AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id=tool_call_id,
                title=f"{fn_name} (not loaded)",
                tool_name=fn_name,
                tool_purpose="not loaded",
                tool_input=json.dumps(fn_args, ensure_ascii=False),
                mcp_server_name="",
            )
            yield AcpEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id=tool_call_id,
                tool_output=(
                    f"{fn_name} is not loaded in this request. Call tool_search with "
                    f"names: [\"{fn_name}\"] first, then call it again."
                ),
                tool_final=True,
                mcp_server_name="",
            )
            return

        # Announce tool call to dashboard
        purpose = fn_args.pop("__tool_use_purpose", "") or fn_name
        # Resolve MCP server name so chat_runner's forgery gate can
        # authenticate directive tools (ask_question, monitor_start, etc.)
        _mcp_server = self._tool_executor.server_name_for(fn_name)
        yield AcpEvent(
            kind=EVENT_TOOL_CALL,
            tool_call_id=tool_call_id,
            title=purpose,
            tool_name=fn_name,
            tool_purpose=purpose,
            tool_input=json.dumps(fn_args),
            mcp_server_name=_mcp_server,
        )

        # Request permission from KiroCrew approval ladder
        fut: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
        _PENDING_APPROVALS[request_id] = fut

        yield AcpEvent(
            kind=EVENT_PERMISSION_REQUEST,
            request_id=request_id,
            title=purpose,
            tool_call_id=tool_call_id,
            tool_name=fn_name,
            tool_input=json.dumps(fn_args),
            options=[
                {"id": OPTION_ALLOW_ONCE, "label": "Allow once"},
                {"id": OPTION_ALLOW_ALWAYS, "label": "Always allow"},
            ],
        )

        try:
            if _approval_is_auto():
                approved = True
            else:
                approved = await asyncio.wait_for(fut, timeout=_approval_timeout())
        except asyncio.TimeoutError:
            approved = False
        finally:
            _PENDING_APPROVALS.pop(request_id, None)

        if not approved:
            yield AcpEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id=tool_call_id,
                tool_output="[Tool call rejected by user]",
                tool_final=True,
                mcp_server_name=_mcp_server,
            )
            return

        # Execute the tool. KiroCrew's tools resolve "which session am I in" through
        # mcp_core._resolve_session_key(): caller context, signed token, then
        # KIROCREW_SESSION_KEY, then a pid mapping. There is no caller context for a
        # tool dispatched by this provider, so the identity has to reach the MCP
        # child in its environment -- the stub servers KiroCrew injects carry it that
        # way too.
        #
        # It rides a contextvar, NOT os.environ. One gateway process serves every
        # session at once (dashboard chats, subagents, crons), so writing the key
        # into os.environ let two concurrent tool calls interleave: spawn_run
        # resolved its parent_session as session A, the gateway then compared the
        # declared key against a token that session B had just stamped, and the
        # request was refused ``member_identity_unavailable``. A contextvar is
        # per-task, and mcp_executor stamps the key+token pair into the child's own
        # environment at spawn, so the pair a request carries always belongs to the
        # session whose tool is running.
        _session_key = _live_session_key(self._session_key)
        # A bare KIROCREW_SESSION_KEY only *declares* the identity; the gateway
        # verifies nothing and answers identity_unattested. The attested path needs
        # a minted stub token published against this session -- that is what the
        # injected MCP servers normally carry.
        _token = self._ensure_stub_token(_session_key) if _session_key else ""
        if _token:
            try:
                from kiro_crew.session_token_sig import publish_session_token

                publish_session_token(_token, _session_key)
            except Exception:  # noqa: BLE001
                pass
        _identity_token = None
        try:
            from .mcp_executor import set_tool_identity

            _identity_token = set_tool_identity(_session_key, _token)
        except Exception:  # noqa: BLE001
            logger.debug("openai_provider: tool identity unavailable", exc_info=True)
        try:
            try:
                result = await self._tool_executor.execute(fn_name, fn_args)
            finally:
                if _identity_token is not None:
                    try:
                        from .mcp_executor import reset_tool_identity

                        reset_tool_identity(_identity_token)
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as exc:
            result = f"[Tool error: {exc}]"
            logger.exception("Tool %s raised", fn_name)

        result_str = result if isinstance(result, str) else json.dumps(result)

        yield AcpEvent(
            kind=EVENT_TOOL_RESULT,
            tool_call_id=tool_call_id,
            tool_output=result_str,
            tool_final=True,
            mcp_server_name=_mcp_server,
        )


# ── Text-format tool call parser ─────────────────────────────────────────────


def _extract_text_tool_calls(text_buffer: list) -> list[dict]:
    """Parse text-format tool calls from buffered TEXT_CHUNK events.

    Some models (especially when given a long system prompt) emit tool calls as
    structured text rather than structured tool_calls JSON.  We support several
    common formats:

    1. KiroCrew XML style (from system prompt examples):
       <tool_call>
         <function=bash>
           <parameter=command>ls -la</parameter>
         </function>
       </tool_call>

    2. Hermes/Nous format:
       <tool_call>{"name": "bash", "arguments": {"command": "ls"}}</tool_call>

    3. Plain JSON block:
       ```json
       {"name": "bash", "arguments": {"command": "ls"}}
       ```

    Returns a list of OpenAI-format tool call dicts, or [] if none found.
    """
    if not text_buffer:
        return []

    # Reconstruct full text from buffer
    full_text = "".join(ev.text for ev in text_buffer if hasattr(ev, "text") and ev.text)
    if not full_text.strip():
        return []

    tool_calls = []

    # ── Format 1: KiroCrew XML style ─────────────────────────────────────────
    # <tool_call>\n  <function=NAME>\n    <parameter=KEY>VALUE</parameter>\n  </function>\n</tool_call>
    xml_blocks = _re.findall(r"<tool_call>(.*?)</tool_call>", full_text, _re.DOTALL)
    for block in xml_blocks:
        # Try JSON first (Hermes format inside <tool_call>)
        block_stripped = block.strip()
        if block_stripped.startswith("{"):
            try:
                obj = json.loads(block_stripped)
                name = obj.get("name") or obj.get("function", {}).get("name", "")
                args = obj.get("arguments") or obj.get("parameters") or obj.get("input") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {"input": args}
                if name:
                    tool_calls.append({
                        "id": f"tc_{uuid.uuid4().hex[:8]}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    })
                    continue
            except json.JSONDecodeError:
                pass

        # KiroCrew XML: <function=NAME> <parameter=KEY>VALUE</parameter> ...
        fn_match = _re.search(r"<function=([^>]+)>", block)
        if fn_match:
            fn_name = fn_match.group(1).strip()
            params = {}
            for pm in _re.finditer(r"<parameter=([^>]+)>(.*?)</parameter>", block, _re.DOTALL):
                params[pm.group(1).strip()] = pm.group(2).strip()
            tool_calls.append({
                "id": f"tc_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {"name": fn_name, "arguments": json.dumps(params)},
            })

    if tool_calls:
        return tool_calls

    # ── Format 2: JSON code block ─────────────────────────────────────────────
    json_blocks = _re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", full_text, _re.DOTALL)
    for block in json_blocks:
        try:
            obj = json.loads(block)
            name = obj.get("name") or obj.get("tool") or obj.get("function", {}).get("name", "")
            args = obj.get("arguments") or obj.get("parameters") or obj.get("input") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {"input": args}
            if name:
                tool_calls.append({
                    "id": f"tc_{uuid.uuid4().hex[:8]}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                })
        except json.JSONDecodeError:
            continue

    return tool_calls


# ── Tool Executor interface ───────────────────────────────────────────────────

class ToolExecutor:
    """Override this to plug in real tool execution (MCP, bash, file I/O)."""

    def tool_definitions(self) -> list[dict]:
        """Return OpenAI-format tool definitions."""
        return []

    def server_name_for(self, tool_name: str) -> str:
        """Return the MCP server name that hosts this tool, or '' for local tools."""
        return ""

    async def execute(self, name: str, args: dict) -> Any:
        raise NotImplementedError(f"No executor for tool: {name}")


class DefaultToolExecutor(ToolExecutor):
    """No-op executor — no tools available."""

    def tool_definitions(self) -> list[dict]:
        return []
