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


def _sanitise_no_proxy(extra_hosts: tuple[str, ...] = ()) -> None:
    """Clean ``no_proxy`` and add the gateway hosts to it.

    Two jobs:

    * drop bare IPv6 literals — httpx parses every ``no_proxy`` entry as
      ``host:port`` and raises ``InvalidURL: Invalid port: ':1]'`` on an entry
      like ``[::1]``, which breaks every HTTP call in the process. Some Windows
      setups ship exactly that;
    * list the gateway's own hostnames, so reaching it never depends on the
      operator's proxy (Clash/mihomo on ``127.0.0.1:7897`` here). The gateway is
      public HTTPS and answers direct — DNS 73 ms, TCP 187 ms, TLS 217 ms,
      ``GET /v1/models`` 633 ms measured with the proxy out of the path — so a
      proxy outage must not take the model list or the chat down with it. Both
      ``httpx`` and ``urllib`` honour ``NO_PROXY``, which is why this is set once
      here instead of threaded through every client.
    """
    hosts = ["localhost", "127.0.0.1", "::1", *extra_hosts]
    for name in ("no_proxy", "NO_PROXY"):
        parts = [
            p for p in os.environ.get(name, "").split(",")
            if p.strip() and not p.strip().startswith("[")
        ]
        os.environ[name] = ",".join(dict.fromkeys([*parts, *hosts]))


def _endpoint_hosts(cfg: dict) -> tuple[str, ...]:
    """Hostnames of the configured endpoints, for the no-proxy list."""
    out: list[str] = []
    for key in ("base_url", "fallback_base_url"):
        value = str(cfg.get(key) or "")
        if "://" not in value:
            continue
        host = value.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0].strip()
        if host and host not in out:
            out.append(host)
    return tuple(out)


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
    # The endpoints must never be reached through the operator's proxy: a proxy
    # outage would otherwise empty the model list and fail every turn.
    _sanitise_no_proxy(_endpoint_hosts(cfg))
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
    _patch_live_effort()
    _patch_effort_reapply()
    _patch_factory_model()
    _patch_bg_session()
    _prewarm_upstream(cfg)


def _patch_live_effort() -> None:
    """Let the effort control change the level WITHOUT a session rebuild.

    ``slot_effort`` only pushes a level live for ``isinstance(provider,
    AcpProvider)``; for anything else it falls through to the reset path, which
    tears the session down and rebuilds it from ``agent``'s configured default.
    On this bridge that rebuild lands on ``provider.json``'s model -- so changing
    the reasoning level silently moved a chat off the model the user picked and
    onto the (paid) default. Widening the module's ``AcpProvider`` name to a
    tuple makes every ``isinstance`` check in that module accept this provider
    too; the provider answers ``supports_effort`` / ``accepts_effort_level`` /
    ``change_effort`` itself, so the live branch then runs unchanged.
    """
    try:
        from kiro_crew.dashboard.chat_api import slot_effort as _slot_effort
        from kiro_crew.providers.acp import AcpProvider as _AcpProvider

        from openai_provider.provider import OpenAIProvider as _OpenAIProvider
    except Exception as exc:  # noqa: BLE001
        _log(f"live effort patch skipped: {type(exc).__name__}: {exc}")
        return

    # This build RELAYS the slot handlers: the function objects are defined in
    # ``chat_api.slot_effort``/``slot_model`` but their ``__globals__`` is
    # ``dashboard.chat_handlers``, and the TYPE_CHECKING-only name ``AcpProvider``
    # resolves there at call time. Widening the wrong module is a silent no-op, so
    # every module that holds either the name or the handler is patched.
    widened = (_AcpProvider, _OpenAIProvider)
    patched: list[str] = []
    for module_name, module in _relay_modules("slot_effort", _slot_effort):
        current = getattr(module, "AcpProvider", None)
        if isinstance(current, type):
            module.AcpProvider = widened  # type: ignore[attr-defined]
            patched.append(module_name)
    if patched:
        _log(f"live effort patch installed: {', '.join(patched)}")
    else:
        _log("live effort patch skipped: no module holds a bare AcpProvider")


def _relay_modules(package: str, first) -> list[tuple[str, object]]:
    """``[(name, module)]`` for every module that may hold a relayed handler."""
    import importlib

    out: list[tuple[str, object]] = [(package, first)]
    for name in ("chat_handlers",):
        try:
            out.append((name, importlib.import_module(f"kiro_crew.dashboard.{name}")))
        except Exception:  # noqa: BLE001
            continue
    return out


def _patch_effort_reapply() -> None:
    """Re-apply the slot's level after an in-place model switch, without a reset.

    ``slot_model._reapply_effort_after_live_switch`` calls
    ``AcpProvider.reapply_live_effort``, which this provider does not implement;
    it returns False on failure, and False tells the caller to RESET the session
    -- which rebuilds on the configured default model. That is how a model pick
    made just before the first message ended up running the default model on the
    message right after. Answering it through ``change_effort`` keeps the pick.
    """
    try:
        from kiro_crew.dashboard.chat_api import slot_model as _slot_model
    except Exception as exc:  # noqa: BLE001
        _log(f"effort-reapply patch skipped: {type(exc).__name__}: {exc}")
        return

    targets = _relay_modules("slot_model", _slot_model)
    original = None
    for _name, module in targets:
        candidate = getattr(module, "_reapply_effort_after_live_switch", None)
        if candidate is not None:
            original = candidate
            break
    if original is None:
        _log("effort-reapply patch skipped: seam not found")
        return

    async def _patched(name, slot, provider):
        change = getattr(provider, "change_effort", None)
        if change is None:
            return await original(name, slot, provider)
        try:
            return bool(await change(slot.reasoning_effort))
        except Exception as exc:  # noqa: BLE001
            _log(f"effort reapply failed: {type(exc).__name__}: {exc}")
            return False

    patched: list[str] = []
    for module_name, module in targets:
        if getattr(module, "_reapply_effort_after_live_switch", None) is not None:
            module._reapply_effort_after_live_switch = _patched  # type: ignore[attr-defined]
            patched.append(module_name)
    _log(f"effort-reapply patch installed: {', '.join(patched)}")


def _prewarm_upstream(cfg: dict) -> None:
    """Warm DNS + TCP + TLS for every endpoint, off the event loop.

    Measured on this host: a cold public endpoint costs 102 ms of DNS, 195 ms of
    TCP and 208 ms of TLS before the first byte of the FIRST request, and one
    cold-DNS lookup was observed at 18 s. Paying that at startup (in a thread,
    where nobody waits on it) keeps it out of the user's first turn, and the
    OS-level DNS answer it caches is what the httpx client reuses afterwards.
    """
    try:
        import threading
    except Exception as exc:  # noqa: BLE001
        _log(f"prewarm skipped: {type(exc).__name__}: {exc}")
        return

    def _warm() -> None:
        import urllib.request

        seen: list[str] = []
        for base in (cfg.get("base_url"), cfg.get("fallback_base_url")):
            base = str(base or "").rstrip("/")
            if not base or base in seen:
                continue
            seen.append(base)
            try:
                req = urllib.request.Request(
                    f"{base}/models",
                    headers={
                        "Authorization": f"Bearer {cfg.get('api_key', '')}",
                        "Accept": "application/json",
                    },
                )
                with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                    resp.read(4096)
                _log(f"prewarm ok {base}")
            except Exception as exc:  # noqa: BLE001
                _log(f"prewarm failed {base}: {type(exc).__name__}: {exc}")

    try:
        threading.Thread(target=_warm, name="hub-neo-prewarm", daemon=True).start()
    except Exception as exc:  # noqa: BLE001
        _log(f"prewarm skipped: {type(exc).__name__}: {exc}")


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


def _last_pick_age_secs() -> float:
    try:
        with open(_last_pick_path(), encoding="utf-8") as fh:
            return max(0.0, time.time() - float(json.load(fh).get("at") or 0.0))
    except Exception:  # noqa: BLE001
        return float("inf")


#: How long a composer pick keeps standing in when a rebuild hands the factory a
#: sentinel. KiroCrew commits a pick on an idle slot right before the first
#: message spawns the provider, and that spawn carries ``model_override=""`` --
#: the pick is applied only after the provider exists, so the first turn used to
#: run the configured DEFAULT model (a different, possibly paid, model) while the
#: composer showed the pick. Bounded: outside this window a sentinel keeps meaning
#: "follow the configured default", so a chat started much later is not silently
#: pinned to whatever was picked last.
_PICK_GRACE_SECS = 900.0


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
                            # No session identity and no record: a pick made
                            # moments ago in the composer still stands (see
                            # _PICK_GRACE_SECS); otherwise let KiroCrew's own
                            # configured default win. Remembering the last UI pick
                            # forever made every new chat answer with whichever
                            # model was picked last, even though the UI showed the
                            # default.
                            recent = _last_pick()
                            if recent and _last_pick_age_secs() <= _PICK_GRACE_SECS:
                                fkw["model_override"] = recent
                                _log(
                                    f"factory session={session_key} sentinel -> recent pick {recent}"
                                )
                            else:
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
    # Same relay hazard as the effort handler: the caller may resolve this name
    # from ``dashboard.chat_handlers``' namespace instead of ``slot_model``.
    for _name, _module in _relay_modules("slot_model", _slot_model):
        if getattr(_module, "_try_live_model_switch", None) is not None:
            _module._try_live_model_switch = _bridge_live_switch  # type: ignore[attr-defined]
    _log("model-switch patch installed")


try:
    _main()
except Exception as _exc:  # noqa: BLE001
    _log(f"INJECTION FAILED: {type(_exc).__name__}: {_exc}")
