"""install.py — Monkey-patch KiroCrew to use OpenAIProvider.

Call ``install()`` **before** ``kirocrew gateway`` initialises its provider
factory.  The patch replaces the ``ProviderRegistry.create_factory`` seam so
every new session spawns an ``OpenAIProvider`` instead of ``AcpProvider``.

Also patches ``LLMPool._create_worker`` so knowledge extraction uses
``OpenAIWorker`` (pure HTTP) instead of ``AcpClient`` (kiro-cli subprocess).

Environment variables (read at ``install()`` time):

    OPENAI_BASE_URL         API endpoint (default: ``https://api.openai.com/v1``)
    OPENAI_API_KEY          API key (required)
    OPENAI_MODEL            model name (default: ``gpt-4o``)
    OPENAI_MAX_TOKENS       max output tokens per turn (default: ``8192``)
    OPENAI_CONTEXT_WINDOW   model context window for usage reporting
                            (default: auto-resolve from API)
    OPENAI_SYSTEM_PROMPT    override system prompt (optional)
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Callable

from ._config import read_provider_config

logger = logging.getLogger(__name__)

_installed = False

# KiroCrew sentinel values that mean "no model pinned, pick the default".
# When the session manager passes one of these as model_override, the factory
# must fall through to the configured model — otherwise the remote API proxy
# receives "auto" (or similar) and returns 404 "model_not_found".
_MODEL_SENTINELS = frozenset({"auto", ""})

# How long the catalog fetch may block the ``/api/models`` handler. One HTTP
# round trip through the operator's proxy costs DNS+CONNECT+TLS on a cold
# start (measured 505 ms, one cold-DNS case at 18 s), so 2 s was too tight for
# the proxy path; 5 s still fails fast against a dead endpoint while giving a
# cold proxy the time it needs. The result is cached for 60 s either way.
_UPSTREAM_MODELS_TIMEOUT = 5.0

#: Bare model ids from the last SUCCESSFUL catalog fetch, replayed when the
#: upstream is unreachable — OPS/``control.mjs`` closed, ssh tunnel dead, or the
#: app restarted before the tunnel came back. Without this the handler degraded
#: to the stock kiro-cli catalog (KiroCrew's BUILT-IN list), which is exactly the
#: "cloud models only appear while OPS is running" symptom.
_SNAPSHOT_NAME = "models_snapshot.json"

#: Memo for the ``/api/models`` handler. Every dashboard refresh calls it, and
#: one uncached call is a full HTTP round trip through the proxy stack (~800 ms
#: measured warm, 4.9 s on the first call after a restart). The list changes when
#: an operator changes it, so a minute of staleness is free.
_MODELS_MEMO: tuple[float, list] = (0.0, [])
_MODELS_MEMO_TTL = 60.0

#: A snapshot older than this is not trusted to pre-seed the memo. The catalog
#: is operator-managed, so an hours-old list can name models that were retired
#: since — and the dashboard would offer those dead ids for the first minute
#: after a restart while the live list already knew better.
_SNAPSHOT_MAX_AGE_SECS = 3600.0


def install(
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    max_tokens: int | None = None,
    system_prompt: str | None = None,
    tool_executor: Any | None = None,
) -> None:
    """Patch KiroCrew's ``ProviderRegistry`` to return ``OpenAIProvider``.

    Safe to call multiple times — subsequent calls are no-ops.
    Explicit keyword arguments override environment variables.
    """
    global _installed
    if _installed:
        return

    from .provider import OpenAIProvider

    # Merge explicit args with env-var config (explicit wins).
    cfg = read_provider_config()
    cfg["base_url"] = base_url or cfg["base_url"]
    cfg["api_key"] = api_key or cfg["api_key"]
    cfg["model"] = model or cfg["model"]
    cfg["max_tokens"] = max_tokens or cfg["max_tokens"]
    cfg["system_prompt"] = system_prompt or cfg["system_prompt"]
    # context_window: explicit param > env var (already in cfg)
    if cfg["context_window"] is None:
        cfg["context_window"] = None  # will auto-resolve in provider

    if not cfg["api_key"]:
        logger.warning(
            "openai_provider: OPENAI_API_KEY not set — provider will fail on first request"
        )

    def _openai_factory(
        session_key: str | None = None,
        agent: str | None = None,
        channel_id: str | None = None,
        model_override: str | None = None,
        cwd: str | None = None,
        extra_env: dict | None = None,
        reasoning_effort_override: str | None = None,
        **_kwargs: object,
    ) -> OpenAIProvider:
        # Resolve model: explicit override wins unless it's a sentinel.
        if model_override and model_override not in _MODEL_SENTINELS:
            used_model = model_override
        else:
            used_model = cfg["model"]
            if model_override:
                logger.info(
                    "openai_provider: model_override=%r is a sentinel — "
                    "falling back to configured model=%s",
                    model_override, cfg["model"],
                )
        executor = tool_executor or _build_mcp_executor()
        return OpenAIProvider(
            base_url=cfg["base_url"],
            fallback_base_url=cfg.get("fallback_base_url", ""),
            api_key=cfg["api_key"],
            model=used_model,
            system_prompt=cfg["system_prompt"],
            max_tokens=cfg["max_tokens"],
            context_window=cfg["context_window"],
            tool_executor=executor,
            session_key=session_key,
            # KiroCrew resolves the session's reasoning level and handed it to
            # AcpProvider as ``unbound_effort``. This factory accepted
            # ``reasoning_effort_override`` and then dropped it on the floor, so
            # every session ran at the model default while the UI showed the
            # picked level. Falls back to the configured level (OPENAI_* env,
            # filled from provider.json / crew config) when the caller sends none.
            reasoning_effort=reasoning_effort_override or cfg.get("reasoning_effort") or None,
            # Names the capability set the effort picker renders from.
            acp_backend=str(_kwargs.get("acp_backend") or ""),
            compaction_threshold=cfg.get("compaction_threshold", 80),
            compaction_keep_recent=cfg.get("compaction_keep_recent", 6),
        )

    # ── Patch 1: build_provider_factory (module-level function) ──────────────
    try:
        from kiro_crew.config import loader as _loader_mod

        def _patched_build(_cfg: Any) -> Callable:
            logger.info(
                "openai_provider: intercepted build_provider_factory — "
                "returning OpenAI factory"
            )
            return _openai_factory

        _loader_mod.build_provider_factory = _patched_build  # type: ignore[attr-defined]
        logger.info("openai_provider: build_provider_factory patched ✅")

    except Exception as exc:
        logger.warning("openai_provider: build_provider_factory patch failed: %s", exc)
        return

    # ── Patch 2: KiroCrewConfig.create_provider_factory (instance method) ───
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        KiroCrewConfig.create_provider_factory = lambda self: _openai_factory  # type: ignore[method-assign]
        logger.info("openai_provider: KiroCrewConfig.create_provider_factory patched ✅")
    except Exception as exc:
        logger.warning("openai_provider: KiroCrewConfig patch skipped: %s", exc)

    # ── Patch 3: LLMPool._create_worker (knowledge extraction) ──────────────
    try:
        from .openai_worker import _register_openai_worker
        _register_openai_worker()
    except Exception as exc:
        logger.warning(
            "openai_provider: knowledge worker registration skipped: %s", exc
        )

    # ── Patch 4: Embedding backend (replace bundled llama.cpp) ─────────────
    try:
        from .embedding_backend import register_remote_embedding
        register_remote_embedding()
    except Exception as exc:
        logger.warning(
            "openai_provider: embedding backend registration skipped: %s", exc
        )

    # ── Patch 5: Background sessions (auto-title, link summary, etc.) ─────
    # ``run_bg_oneliner`` (used by chat_title, chat_nav) calls
    # ``sessions.get_bg_session()`` which checks ``_bg_provider_is_kiro()``.
    # That method reads ``agent.provider`` from config.json — if it's "acp",
    # bg sessions route to AcpSessionHandle → kiro-cli → Anthropic, bypassing
    # our patched factory entirely.  Overriding it to return False makes
    # ``get_bg_session`` fall through to ``_ensure_background()`` which
    # creates a session via the (already-patched) provider factory →
    # OpenAIProvider → 9router.  The ``_ProviderBgSession`` wrapper provides
    # the same ``prompt()``/``reject_tool()``/``destroy()`` interface that
    # ``run_bg_oneliner`` expects.
    try:
        from kiro_crew.session import SessionManager

        def _bg_provider_is_not_kiro(self: Any) -> bool:
            return False

        SessionManager._bg_provider_is_kiro = _bg_provider_is_not_kiro  # type: ignore[method-assign]
        logger.info(
            "openai_provider: SessionManager._bg_provider_is_kiro patched → "
            "bg sessions will use OpenAI factory ✅"
        )
    except Exception as exc:
        logger.warning(
            "openai_provider: bg session patch skipped: %s", exc
        )

    # ── Patch 6: /api/models — upstream-first catalog ───────────────────
    # Replaces the stock /api/models handler (which shells out to
    # ``kiro-cli --list-models``) with one that queries the upstream
    # OpenAI-compatible ``/v1/models`` endpoint first.  The kiro-cli
    # handler is kept as a graceful fallback if upstream is unreachable.
    try:
        _patch_api_models_fallback(
            cfg["base_url"], cfg["api_key"], cfg.get("fallback_base_url", ""),
            default_window=int(cfg.get("context_window") or 0),
        )
    except Exception as exc:
        logger.warning("openai_provider: /api/models fallback patch skipped: %s", exc)

    _installed = True
    logger.info(
        "openai_provider installed — model=%s  base_url=%s",
        cfg["model"], cfg["base_url"],
    )


def _bridge_dir() -> str:
    """The bridge's install root (``%LOCALAPPDATA%\\kirocrew-openai-bridge``)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _snapshot_path() -> str:
    return os.path.join(_bridge_dir(), _SNAPSHOT_NAME)


def _read_snapshot() -> list[str]:
    """Bare ids from the last good catalog fetch, or ``[]``."""
    try:
        with open(_snapshot_path(), encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return []
    ids = payload.get("ids") if isinstance(payload, dict) else payload
    if not isinstance(ids, list):
        return []
    return [str(i).strip() for i in ids if isinstance(i, str) and str(i).strip()]


def _snapshot_age_secs() -> float:
    """Age of the cached catalog, ``inf`` when unknown or unreadable."""
    try:
        with open(_snapshot_path(), encoding="utf-8") as fh:
            payload = json.load(fh)
        # A missing/0/non-numeric stamp is unknown, not "written at epoch":
        # ``or 0.0`` turned that into an age of ~1.7e9 s, which read as a
        # decades-old snapshot instead of one nobody dated.
        fetched_at = float(payload.get("fetched_at") or 0.0)
        if fetched_at <= 0.0:
            return float("inf")
        return max(0.0, time.time() - fetched_at)
    except (OSError, ValueError, TypeError, AttributeError):
        return float("inf")


def _write_snapshot(ids: list[str]) -> None:
    """Best-effort persist of a successful answer; never raises into a handler."""
    path = _snapshot_path()
    try:
        with open(path + ".tmp", "w", encoding="utf-8") as fh:
            json.dump({"fetched_at": time.time(), "ids": ids}, fh)
        os.replace(path + ".tmp", path)
    except OSError:
        logger.debug("openai_provider: could not persist model snapshot", exc_info=True)


def _local_catalog_ids() -> list[str]:
    """Last-resort ids from the local opencode catalog.

    ``~/.config/opencode/opencode.json`` carries ``provider.hub-neo.models`` —
    the same nine ids, on disk, with no network in the path. Used only when
    there is no snapshot yet (first run after install).
    """
    path = os.path.join(os.path.expanduser("~"), ".config", "opencode", "opencode.json")
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        models = ((payload.get("provider") or {}).get("hub-neo") or {}).get("models") or {}
        return [str(k) for k in models if str(k)]
    except (OSError, ValueError, AttributeError):
        return []


def _format_models(
    ids: list[str], windows: dict[str, int] | None = None, default_window: int = 0,
) -> list[dict[str, object]]:
    """KiroCrew's ``/api/models`` row shape for BARE upstream model ids.

    ``windows`` carries whatever window the gateway itself advertised per id:
    ``model_registry.model_window`` knows nothing about these provider ids and
    answered 0 for all nine, which made the dashboard's context meter read 0 %
    on every model. The registry still wins when it knows the id (it is the
    project's single source of truth); the advertised number is the fallback,
    and ``default_window`` (the provider's configured context window) is the
    last resort so a newly added upstream id never renders as a 0-token model.
    """
    from kiro_crew import model_registry

    windows = windows or {}
    rows: list[dict[str, object]] = []
    for mid in ids:
        rows.append(
            {
                "model_name": mid,
                "description": "",
                "context_window_tokens": model_registry.model_window(mid)
                or int(windows.get(mid) or 0)
                or int(default_window or 0),
                "rate_multiplier": 1.0,
            }
        )
    return rows


def _row_window(row: dict) -> int:
    """Context window advertised by one ``/v1/models`` row, 0 when absent."""
    caps = row.get("capabilities") if isinstance(row.get("capabilities"), dict) else {}
    for value in (
        row.get("context_window"),
        row.get("context_length"),
        caps.get("contextWindow"),
        caps.get("context_window"),
    ):
        try:
            if value and int(value) > 0:
                return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def _degraded_models(default_window: int = 0) -> list[dict[str, object]] | None:
    """What to serve when the upstream catalog cannot be read.

    Snapshot first (the real cloud list as last seen, while it is no older than
    ``_SNAPSHOT_MAX_AGE_SECS``), local opencode catalog second, ``None`` last —
    which lets the caller fall back to the stock kiro-cli handler. The order
    matters: a fresh-and-real list beats an empty one, and both beat KiroCrew's
    built-in list, which names models this deployment cannot run at all. The
    snapshot's age goes into the log so a dead-id catalog is never mistaken for
    a live one.
    """
    ids = _read_snapshot()
    age_secs = _snapshot_age_secs()
    if ids and age_secs <= _SNAPSHOT_MAX_AGE_SECS:
        logger.warning(
            "openai_provider: upstream /models unreachable — serving %d cached ids"
            " from %s (age %.1f h)", len(ids), _snapshot_path(),
            age_secs / 3600.0,
        )
        return _format_models(ids, default_window=default_window)
    if ids:
        # An over-age snapshot names models that may no longer exist upstream;
        # the local catalog at least only lists ids this deployment can ask for.
        logger.warning(
            "openai_provider: snapshot too old (%.1f h), falling back to the local"
            " catalog", age_secs / 3600.0,
        )
    ids = _local_catalog_ids()
    if ids:
        logger.warning(
            "openai_provider: upstream /models unreachable and no snapshot — serving"
            " %d ids from the local opencode catalog", len(ids),
        )
        return _format_models(ids, default_window=default_window)
    logger.warning("openai_provider: no upstream, no snapshot, no local catalog")
    return None


async def _fetch_upstream_models(
    base_url: str, api_key: str, fallback_base_url: str = "",
    default_window: int = 0,
) -> list[dict[str, object]] | None:
    """Fetch and format models from the upstream OpenAI-compatible ``/v1/models``.

    Returns a list of model dicts formatted for the KiroCrew frontend
    (``{model_name, description, context_window_tokens, rate_multiplier}``).

    When the upstream cannot be read, falls back to the last good snapshot and
    then to the local opencode catalog (see :func:`_degraded_models`) instead of
    handing the user KiroCrew's built-in list. ``None`` is returned only when
    nothing local knows the catalog either.

    Uses ``model_registry.model_window()`` for context window resolution,
    falling back to ``0`` for unknown models.
    """
    import httpx

    endpoints = [base_url.rstrip("/")]
    if fallback_base_url and fallback_base_url.rstrip("/") not in endpoints:
        endpoints.append(fallback_base_url.rstrip("/"))

    for endpoint in endpoints:
        try:
            async with httpx.AsyncClient(timeout=_UPSTREAM_MODELS_TIMEOUT) as client:
                resp = await client.get(
                    f"{endpoint}/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                if resp.status_code != 200:
                    logger.warning(
                        "openai_provider: upstream /models returned %d from %s",
                        resp.status_code, endpoint,
                    )
                    continue

                raw_models = resp.json().get("data", [])
        except Exception:
            logger.warning(
                "openai_provider: upstream /models query failed for %s", endpoint,
                exc_info=True,
            )
            continue

        ids = [
            str(raw["id"]).strip()
            for raw in raw_models
            if isinstance(raw, dict) and raw.get("id")
        ]
        windows = {
            str(raw["id"]).strip(): _row_window(raw)
            for raw in raw_models
            if isinstance(raw, dict) and raw.get("id")
        }

        if not ids:
            logger.warning("openai_provider: /models returned 0 models from %s", endpoint)
            continue

        _write_snapshot(ids)
        logger.info(
            "openai_provider: upstream /models returned %d models from %s",
            len(ids), endpoint,
        )
        return _format_models(ids, windows, default_window=default_window)

    return _degraded_models(default_window=default_window)


def _patch_api_models_fallback(
    base_url: str, api_key: str, fallback_base_url: str = "",
    default_window: int = 0,
) -> None:
    """Replace ``/api/models`` with an upstream-first model catalog.

    Queries the upstream OpenAI-compatible ``/v1/models`` endpoint.  If
    unavailable, falls back to the stock ``kiro-cli --list-models`` handler.
    If both fail, returns a 503.

    Also patches the ``handlers`` package-level binding so the aiohttp
    router (which resolves via ``handlers.api_models``, not
    ``handlers.agents.api_models``) picks up the replacement.
    """
    from kiro_crew.dashboard.handlers import agents as _agents_mod

    _original_handler = _agents_mod.api_models

    async def _api_models_with_upstream(request):  # type: ignore[no-untyped-def]
        """Upstream-first /api/models, memoized, with kiro-cli fallback."""
        from aiohttp import web

        global _MODELS_MEMO
        stamped, cached = _MODELS_MEMO
        if cached and time.time() - stamped < _MODELS_MEMO_TTL:
            return web.json_response(cached)

        # Try upstream first — it's the authoritative source.
        models = await _fetch_upstream_models(
            base_url, api_key, fallback_base_url, default_window=default_window,
        )
        if models is not None:
            _MODELS_MEMO = (time.time(), models)
            return web.json_response(models)

        # Upstream unavailable — fall back to stock kiro-cli handler.
        logger.info("openai_provider: falling back to stock kiro-cli /api/models")
        try:
            return await _original_handler(request)
        except Exception:
            logger.warning(
                "openai_provider: stock /api/models also failed", exc_info=True,
            )
            return web.json_response(
                {"error": "model list unavailable"}, status=503,
            )

    # Patch both the module-level and package-level bindings — ``server.py``
    # routes via ``handlers.api_models`` (the package __init__ re-export),
    # not ``handlers.agents.api_models``.
    # Seed the memo from the snapshot: the first dashboard refresh after a start
    # used to pay the whole cold path (measured 4.9 s while the gateway was still
    # restoring memory), and the answer it finally produced was a list the disk
    # already knew. The snapshot is that same list — but only while it is still
    # young enough to be the same list; a stale one seeds dead model ids into the
    # picker for the whole first minute, so an old snapshot is left on disk and
    # simply not trusted here.
    global _MODELS_MEMO
    _seed = _read_snapshot()
    if _seed and _snapshot_age_secs() <= _SNAPSHOT_MAX_AGE_SECS:
        _MODELS_MEMO = (time.time(), _format_models(_seed, default_window=default_window))

    _agents_mod.api_models = _api_models_with_upstream  # type: ignore[assignment]
    import kiro_crew.dashboard.handlers as _handlers_pkg
    _handlers_pkg.api_models = _api_models_with_upstream  # type: ignore[assignment]

    logger.info("openai_provider: /api/models replaced with upstream catalog ✅")


def _build_mcp_executor() -> Any:
    """Build a ``ToolExecutor`` backed by KiroCrew's MCP infrastructure."""
    try:
        from .mcp_executor import McpToolExecutor
        return McpToolExecutor()
    except Exception:
        from .provider import DefaultToolExecutor
        return DefaultToolExecutor()
