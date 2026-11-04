"""hook.py — the actual injection: point KiroCrew's provider at an OpenAI endpoint.

Loaded by ``hub_boot.py`` (which ``hub_boot.pth`` pulls in from KiroCrew's own
``site-packages``). Two guards keep it harmless everywhere else:

* ``HUB_BRIDGE=0`` disables it;
* it only runs for the ``gateway`` command, so ``kiro_crew mcp-core``,
  ``mcp-cron``, pool workers and every other Python process are untouched.

Everything it does is reversible and logged to ``<bootstrap>/inject.log``.
"""

from __future__ import annotations

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


try:
    _main()
except Exception as _exc:  # noqa: BLE001
    _log(f"INJECTION FAILED: {type(_exc).__name__}: {_exc}")
