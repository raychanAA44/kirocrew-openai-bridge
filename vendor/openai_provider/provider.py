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

def _crew_config() -> dict:
    """Read KiroCrew's own agent config (cheap, always current)."""
    try:
        with open(os.path.join(os.path.expanduser("~"), ".kiro", "crew", "config.json"),
                  encoding="utf-8-sig") as fh:
            return json.load(fh).get("agent") or {}
    except Exception:  # noqa: BLE001
        return {}


#: Reasoning-effort levels each served model accepts, from the vendor docs.
#: A model ABSENT from this map gets no ``reasoning_effort`` field at all --
#: sending one to a route that does not take it is a 400, and the subagent
#: routes here are free third-party models whose capability we cannot verify.
#: Note ``gpt-6.1-sol`` and ``gpt-6-astra`` do NOT list ``none`` while
#: ``gpt-6.1-terra``, ``gpt-6-sol`` and ``gpt-6-luna`` do.
_EFFORT_SUPPORT: dict[str, tuple[str, ...]] = {
    "gpt-6.1-sol": ("low", "medium", "high", "xhigh", "max"),
    "gpt-6.1-terra": ("none", "low", "medium", "high", "xhigh", "max"),
    "gpt-6-astra": ("low", "medium", "high", "xhigh", "max"),
    "gpt-6-sol": ("none", "low", "medium", "high", "xhigh", "max"),
    "gpt-6-luna": ("none", "low", "medium", "high", "xhigh", "max"),
    # Third-party routes this backend drives subagents and background work on.
    # Their vendors publish no effort matrix, so the three levels KiroCrew's own
    # picker treats as the common denominator are what we send; every other
    # level is dropped rather than risked on an undocumented endpoint.
    "beam-free": ("low", "medium", "high"),
    "deepseek-v4.1-flash": ("low", "medium", "high"),
}


def _effort_for_model(model: str, explicit: "str | None") -> str:
    """The ``reasoning_effort`` to send for *model*, or "" to send none.

    *explicit* wins when the caller supplied one; otherwise KiroCrew's
    ``agent.reasoning_effort`` is used. Either way the value is dropped unless
    THIS model documents the level, so a chat-level setting never leaks onto a
    subagent route that would reject it.
    """
    bare = str(model or "").rsplit("/", 1)[-1].strip().lower()
    if not bare:
        return ""
    allowed = _EFFORT_SUPPORT.get(bare)
    if allowed is None:
        for family, levels in _EFFORT_SUPPORT.items():
            if bare.startswith(family):
                allowed = levels
                break
    if allowed is None:
        return ""
    want = str(explicit or "").strip().lower()
    if not want:
        want = str(_crew_config().get("reasoning_effort") or "").strip().lower()
    if not want or want not in allowed:
        return ""
    return want


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
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        system_prompt: str = "",
        max_tokens: int = DEFAULT_MAX_TOKENS,
        context_window: int | None = None,
        temperature: float | None = None,
        tool_executor: "ToolExecutor | None" = None,
        session_key: str | None = None,
        reasoning_effort: str | None = None,
        compaction_threshold: int = DEFAULT_COMPACTION_THRESHOLD,
        compaction_keep_recent: int = DEFAULT_COMPACTION_KEEP_RECENT,
    ) -> None:
        self._base_url = base_url or read_env_str("OPENAI_BASE_URL", DEFAULT_BASE_URL)
        self._api_key = api_key or read_env_str("OPENAI_API_KEY")
        self._model = _bare_model(model)
        self._system_prompt = system_prompt
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._tool_executor: ToolExecutor = tool_executor or DefaultToolExecutor()
        self._session_key = session_key
        # Reasoning effort is a PER-MODEL capability, not a global switch: some
        # served models reject the field outright and one rejects ``none`` while
        # accepting every other level. ``_effort_for_model`` decides what this
        # model actually accepts, so a level configured for the chat model is never
        # sent to a subagent model that would 400 on it.
        self._reasoning_effort = _effort_for_model(self._model, reasoning_effort)

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

    def context_window_tokens(self) -> int:
        return self._context_window or self._max_tokens

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
                f"{self._base_url}/chat/completions",
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
                    if len(pending) > _STREAM_GUARD_CHARS:
                        tail = pending[-_STREAM_GUARD_CHARS:]
                        safe = pending[:-_STREAM_GUARD_CHARS]
                        text_buffer = [AcpEvent(kind=EVENT_TEXT_CHUNK, text=tail)]
                        if safe:
                            yield AcpEvent(kind=EVENT_TEXT_CHUNK, text=safe)
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
        if self._max_tokens:
            payload["max_tokens"] = self._max_tokens
        if self._temperature is not None:
            payload["temperature"] = self._temperature
        # Per-model: "" means this route takes no effort field (see _effort_for_model).
        if self._reasoning_effort:
            payload["reasoning_effort"] = self._reasoning_effort
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        # Accumulate tool calls across chunks
        accumulated_tool_calls: dict[int, dict] = {}
        finish_reason = "stop"
        assistant_text = ""

        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST",
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=headers,
            ) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
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

                    # Usage (last chunk on many providers)
                    if usage := chunk.get("usage"):
                        pt = int(usage.get("prompt_tokens") or 0)
                        ct = int(usage.get("completion_tokens") or 0)
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
