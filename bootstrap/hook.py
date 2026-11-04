"""hook.py — the actual injection: point KiroCrew's provider at an OpenAI endpoint.

Loaded by ``hub_boot.py`` (which ``hub_boot.pth`` pulls in from KiroCrew's own
``site-packages``). Two guards keep it harmless everywhere else:

* ``HUB_BRIDGE=0`` disables it;
* it only runs for the ``gateway`` command, so ``kiro_crew mcp-core``,
  ``mcp-cron``, pool workers and every other Python process are untouched.

Everything it does is reversible and logged to ``<bootstrap>/inject.log``.
"""

from __future__ import annotations

import glob
import json
import os
import sys
import time

_TAG = "openai-bridge"
_BOOT_DIR = os.path.dirname(os.path.abspath(__file__))
_LOG = os.path.join(_BOOT_DIR, "inject.log")


def _log(msg: str) -> None:
    try:
        with open(_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} [{_TAG}] {msg}\n")
    except Exception:  # noqa: BLE001
        pass


def _sanitise_no_proxy() -> None:
    """Drop bare IPv6 literals from ``no_proxy``.

    httpx parses every ``no_proxy`` entry as ``host:port`` and raises
    ``InvalidURL: Invalid port: ':1]'`` on an entry like ``[::1]``, which breaks
    every HTTP call in the process. Some Windows setups ship exactly that.
    """
    for name in ("no_proxy", "NO_PROXY"):
        parts = [
            p for p in os.environ.get(name, "").split(",")
            if p.strip() and not p.strip().startswith("[")
        ]
        os.environ[name] = ",".join(dict.fromkeys(parts or ["localhost", "127.0.0.1"]))


def _main() -> None:
    _sanitise_no_proxy()

    if os.environ.get("HUB_BRIDGE", "1") == "0":
        return
    # 文件级停用开关。环境变量改不了已经在跑的 App（网关由壳带旧环境重启），
    # 落一个文件就能让下一次解释器启动跳过整个注入；回滚 = 删掉这个文件。
    if os.path.isfile(os.path.join(_BOOT_DIR, "DISABLED")):
        return
    if "gateway" not in " ".join(sys.argv[1:]):
        return

    import importlib
    import threading

    sys.path.insert(0, _BOOT_DIR)
    import provider_config

    import openai_provider

    # ``openai_provider/__init__.py`` re-exports ``install`` as a *function*,
    # which shadows the submodule of the same name.
    install_mod = importlib.import_module("openai_provider.install")

    cfg = provider_config.resolve()
    os.environ.update(provider_config.as_env(cfg))
    _log(
        "target base_url=%s model=%s key=%s (from %s)"
        % (
            cfg["base_url"],
            cfg["model"],
            "set" if cfg["api_key"] else "MISSING",
            ", ".join(f"{k}<-{v}" for k, v in cfg["sources"].items()) or "provider.json",
        )
    )

    # Build the 125-tool executor once per process instead of once per session
    # (measured: 3.7 s per build). The executor holds no per-session state, only
    # a lock and two caches.
    holder: dict[str, object] = {}
    lock = threading.Lock()
    original = install_mod._build_mcp_executor

    def _cached_executor():
        inst = holder.get("ex")
        if inst is not None:
            return inst
        with lock:
            if holder.get("ex") is None:
                holder["ex"] = original()
                _log("tool executor built once, shared by every session")
            return holder["ex"]

    install_mod._build_mcp_executor = _cached_executor

    openai_provider.install()
    _log(f"installed base_url={cfg['base_url']} model={cfg['model']}")

    _patch_live_model_switch()
    _patch_factory_model()
    _patch_bg_session()


def _patch_bg_session() -> None:
    """Give background work (auto-title, link summaries) a session it can use.

    ``BackgroundSessionRuntime._provider_backed_bg_session`` asks the owner to
    start the shared ``_bg`` session and then expects to find it in the session
    map. With a provider-backed (non-ACP) backend that start can return without
    registering anything, and the call then dies with

        RuntimeError: background session unavailable for non-kiro _bg provider

    so every background task fails. Here the session is built and registered the
    same way the foreground path does it. The background *model* stays whatever
    KiroCrew is configured with (``agent.role_models``) -- this patch only removes
    the plumbing failure.
    """
    try:
        from kiro_crew import session_background as _bg
    except Exception as exc:  # noqa: BLE001
        _log(f"bg session patch skipped: {type(exc).__name__}: {exc}")
        return

    runtime_cls = getattr(_bg, "BackgroundSessionRuntime", None)
    original = getattr(runtime_cls, "_provider_backed_bg_session", None) if runtime_cls else None
    if original is None:
        _log("bg session patch skipped: seam not found")
        return

    async def _fallback(self):
        deps = self._deps
        owner = self._owner
        factory = getattr(owner, "_provider_factory", None)
        if factory is None:
            raise RuntimeError("no provider factory for background session")
        provider = factory(deps.background_key, agent=deps.background_agent)
        await provider.start()
        sess = deps.session_factory(
            provider=provider,
            first_turn=deps.first_turn_nothing_armed,
            agent=deps.background_agent,
        )
        owner._sessions[deps.background_key] = sess
        try:
            owner._advance_session_generation(deps.background_key)
        except Exception:  # noqa: BLE001
            pass
        _log(f"bg session created directly (agent={deps.background_agent})")
        return deps.provider_bg_session_factory(sess)

    async def _patched(self, *args, **kwargs):
        try:
            return await original(self, *args, **kwargs)
        except RuntimeError as exc:
            if "background session unavailable" not in str(exc):
                raise
            _log("bg session missing after ensure_background - building it here")
            try:
                return await _fallback(self)
            except Exception as inner:  # noqa: BLE001
                _log(f"bg fallback failed: {type(inner).__name__}: {inner}")
                raise exc from inner

    runtime_cls._provider_backed_bg_session = _patched  # type: ignore[assignment]
    _log("bg session patch installed")


def _role_model(role: str) -> str:
    """Read ``agent.role_models.<role>`` from KiroCrew's own config.

    The provider factory is handed an agent name, not a role, so the role's model
    pin (which KiroCrew normally resolves from the agent-spec snapshot) has to be
    looked up here -- otherwise background work silently runs on the chat model.
    """
    try:
        with open(os.path.join(os.path.expanduser("~"), ".kiro", "crew", "config.json"),
                  encoding="utf-8-sig") as fh:
            roles = (json.load(fh).get("agent") or {}).get("role_models") or {}
        return str(roles.get(role) or "")
    except Exception:  # noqa: BLE001
        return ""


def _last_pick_path() -> str:
    return os.path.join(_BOOT_DIR, "last_model.json")


def _remember_pick(model: str) -> None:
    try:
        with open(_last_pick_path(), "w", encoding="utf-8") as fh:
            json.dump({"model": model, "at": time.time()}, fh)
    except Exception:  # noqa: BLE001
        pass


def _last_pick() -> str:
    try:
        with open(_last_pick_path(), encoding="utf-8") as fh:
            return str(json.load(fh).get("model") or "")
    except Exception:  # noqa: BLE001
        return ""


def _session_model(session_key: str | None) -> str:
    """Read the model KiroCrew recorded for a session, if any.

    New sessions are created with a sentinel (``auto``/``""``) as ``model_override``
    even when the dashboard just recorded a concrete pick, and the factory then
    falls back to the configured default -- which is how a session ends up running
    a different model than the one on its own header.
    """
    if not session_key:
        return ""
    sessions = os.path.join(os.path.expanduser("~"), ".kiro", "crew", "sessions")
    names = [f"dashboard_{session_key}.jsonl"]
    try:
        names += [os.path.basename(p) for p in glob.glob(os.path.join(sessions, f"*{session_key}*.jsonl"))]
    except Exception:  # noqa: BLE001
        pass
    for name in names:
        path = os.path.join(sessions, name)
        try:
            with open(path, encoding="utf-8") as fh:
                head = fh.readline()
            marker = '"model": "'
            at = head.find(marker)
            if at != -1:
                return head[at + len(marker):head.find('"', at + len(marker))]
        except Exception:  # noqa: BLE001
            continue
    return ""


def _patch_factory_model() -> None:
    """Feed the session's own model into the factory when the caller sends a sentinel."""
    try:
        from kiro_crew.config import loader as _loader
    except Exception as exc:  # noqa: BLE001
        _log(f"factory model patch skipped: {type(exc).__name__}: {exc}")
        return

    sentinels = {"", "auto"}

    def _wrap(build):
        def _wrapped_build(*args, **kwargs):
            factory = build(*args, **kwargs)

            def _factory(*fargs, **fkw):
                # The factory is called both keyword-only and positionally
                # (``provider_factory(session_key, agent=...)``), so nothing here may
                # assume a signature -- and nothing may raise into KiroCrew: a broken
                # patch would surface as HTTP 500 on "new chat".
                try:
                    override = str(fkw.get("model_override") or "").strip()
                    session_key = fkw.get("session_key") or (fargs[0] if fargs else None)
                    key = str(session_key or "")
                    is_background = key in ("_bg", "bg")
                    is_subagent = bool(key) and os.path.isdir(
                        os.path.join(os.path.expanduser("~"), ".kiro", "crew", "subagents", key)
                    )
                    if is_subagent:
                        # A subagent run is its own session. KiroCrew expects the
                        # factory to resolve the agent spec's model pin; without it
                        # the run silently executes on the chat model instead of the
                        # configured subagent role model.
                        role_model = _role_model("subagent")
                        if role_model:
                            fkw["model_override"] = role_model
                            _log(f"factory session={key} subagent -> {role_model}")
                        else:
                            _log(f"factory session={key} subagent, no role model configured")
                    elif is_background:
                        # Background work must run on the role's own model, not the
                        # chat model: agent.role_models.background is the user's pick.
                        role_model = _role_model("background")
                        if role_model:
                            fkw["model_override"] = role_model
                            _log(f"factory session={session_key} background -> {role_model}")
                        else:
                            _log(f"factory session={session_key} background, no role model configured")
                    elif override in sentinels:
                        recorded = _session_model(session_key)
                        if recorded:
                            fkw["model_override"] = recorded
                            _log(f"factory session={session_key} sentinel -> session model {recorded}")
                        else:
                            # No session identity and no record: let KiroCrew's own
                            # configured default win. Remembering the last UI pick here
                            # made every new chat answer with whichever model was picked
                            # last, even though the UI showed the default.
                            fkw.pop("model_override", None)
                            _log(f"factory session={session_key} sentinel -> configured default")
                    else:
                        _log(f"factory session={session_key} model_override={override}")
                except Exception as exc:  # noqa: BLE001
                    _log(f"factory model patch error: {type(exc).__name__}: {exc}")
                return factory(*fargs, **fkw)

            return _factory

        return _wrapped_build

    _loader.build_provider_factory = _wrap(_loader.build_provider_factory)
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        original_create = KiroCrewConfig.create_provider_factory

        def _create(self, *args, **kwargs):
            return _wrap(lambda *a, **k: original_create(self, *a, **k))(*args, **kwargs)

        KiroCrewConfig.create_provider_factory = _create  # type: ignore[assignment]
    except Exception as exc:  # noqa: BLE001
        _log(f"factory model patch (config) skipped: {type(exc).__name__}: {exc}")

    _log("factory model patch installed")


def _patch_live_model_switch() -> None:
    """Let the dashboard's model picker reach this provider.

    ``slot_model._try_live_model_switch`` bails out early for anything that is not
    an ``AcpProvider``, so a pick in the UI was only committed to the slot while the
    provider kept the model it was constructed with -- the classic "the UI says one
    model, the requests use another". The caller then falls back to resetting the
    session, which for this bridge does not rebuild the provider either.

    Here the switch is applied directly through ``provider.set_model`` when the
    provider has it, and ``True`` is returned so the caller keeps the conversation.
    """
    try:
        from kiro_crew.dashboard.chat_api import slot_model as _slot_model
    except Exception as exc:  # noqa: BLE001
        _log(f"model-switch patch skipped: {type(exc).__name__}: {exc}")
        return

    original = getattr(_slot_model, "_try_live_model_switch", None)
    if original is None:
        _log("model-switch patch skipped: seam not found")
        return

    async def _bridge_live_switch(name, slot, provider, model_name):
        setter = getattr(provider, "set_model", None)
        if setter is None:
            return await original(name, slot, provider, model_name)

        busy = getattr(provider, "has_active_turn", None)
        if callable(busy):
            try:
                if busy():
                    return False
            except Exception:  # noqa: BLE001
                pass

        wire = (model_name or "").strip()
        if not wire or wire == "auto":
            return await original(name, slot, provider, model_name)

        try:
            await setter(wire)
        except Exception as exc:  # noqa: BLE001
            _log(f"live model switch to {wire} failed: {type(exc).__name__}: {exc}")
            return await original(name, slot, provider, model_name)
        # Remember it: when this provider is later rebuilt (idle timeout, app
        # restart) KiroCrew creates it with a sentinel override, and without this
        # the session silently reverts to the configured default model.
        _remember_pick(wire)
        _log(f"live model switch -> {wire}")
        return True

    _slot_model._try_live_model_switch = _bridge_live_switch  # type: ignore[assignment]
    _log("model-switch patch installed")


try:
    _main()
except Exception as _exc:  # noqa: BLE001
    _log(f"INJECTION FAILED: {type(_exc).__name__}: {_exc}")
