"""provider_config.py — decide where the provider points and which model it uses.

Precedence (first hit wins, per field):

1. environment: ``OPENAI_BASE_URL`` / ``OPENAI_API_KEY`` / ``OPENAI_MODEL``
2. ``provider.json`` in this folder (written by the installer)
3. ids KiroCrew already manages, so an existing control panel keeps working:
   ``~/.config/opencode/opencode.json`` → ``provider.hub-neo.options`` for
   ``baseURL`` / ``apiKey``, and ``~/.kiro/crew/config.json`` → ``agent.model``

Step 3 exists because KiroCrew's own model panels write there; reading them keeps
one source of truth and lets a user change models from the UI without editing
this bridge.
"""

from __future__ import annotations

import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
PROVIDER_JSON = os.path.join(_HERE, "provider.json")
OPENCODE_JSON = os.path.expanduser(r"~/.config/opencode/opencode.json")
CREW_JSON = os.path.expanduser(r"~/.kiro/crew/config.json")

_PROVIDER_PREFIXES = ("hub-neo/",)


def _read(path: str) -> dict:
    # utf-8-sig: Windows PowerShell 5.1 writes a BOM with Set-Content -Encoding UTF8,
    # and a BOM makes json.load fail, which would silently ignore provider.json.
    try:
        with open(path, encoding="utf-8-sig") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _strip_provider(name: str) -> str:
    name = (name or "").strip()
    for prefix in _PROVIDER_PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def resolve() -> dict:
    own = _read(PROVIDER_JSON)

    cfg: dict = {
        "base_url": str(os.environ.get("OPENAI_BASE_URL") or own.get("base_url") or ""),
        "model": str(os.environ.get("OPENAI_MODEL") or own.get("model") or ""),
        "api_key": str(os.environ.get("OPENAI_API_KEY") or own.get("api_key") or ""),
        "max_tokens": int(os.environ.get("OPENAI_MAX_TOKENS") or own.get("max_tokens") or 8192),
        "context_window": int(
            os.environ.get("OPENAI_CONTEXT_WINDOW") or own.get("context_window") or 1048000
        ),
        "sources": {},
    }
    if cfg["base_url"]:
        cfg["sources"]["base_url"] = "env" if os.environ.get("OPENAI_BASE_URL") else "provider.json"
    if cfg["model"]:
        cfg["sources"]["model"] = "env" if os.environ.get("OPENAI_MODEL") else "provider.json"
    if cfg["api_key"]:
        cfg["sources"]["api_key"] = "env" if os.environ.get("OPENAI_API_KEY") else "provider.json"

    options = (((_read(OPENCODE_JSON).get("provider") or {}).get("hub-neo") or {}).get("options") or {})
    if not cfg["base_url"]:
        base = options.get("baseURL") or options.get("baseUrl")
        if base:
            cfg["base_url"] = str(base).rstrip("/")
            cfg["sources"]["base_url"] = "opencode.json"
    if not cfg["api_key"] and options.get("apiKey"):
        cfg["api_key"] = str(options["apiKey"])
        cfg["sources"]["api_key"] = "opencode.json"
    if not cfg["model"]:
        model = _strip_provider(str(((_read(CREW_JSON).get("agent") or {}).get("model") or "")))
        if model:
            cfg["model"] = model
            cfg["sources"]["model"] = "crew config.json"

    cfg["base_url"] = cfg["base_url"].strip().rstrip("/") or "http://127.0.0.1:8888/v1"
    cfg["model"] = _strip_provider(cfg["model"].strip()) or "gpt-4o-mini"
    # A stray newline in the key produces httpx's
    # "Illegal header value b'Bearer '" / "invalid CRLF characters" at request time.
    cfg["api_key"] = "".join(cfg["api_key"].split())
    return cfg


def as_env(cfg: dict | None = None) -> dict:
    """Return the ``OPENAI_*`` environment mapping for a resolved config."""
    cfg = cfg or resolve()
    return {
        "OPENAI_BASE_URL": cfg["base_url"],
        "OPENAI_API_KEY": cfg["api_key"],
        "OPENAI_MODEL": cfg["model"],
        "OPENAI_MAX_TOKENS": str(cfg["max_tokens"]),
        "OPENAI_CONTEXT_WINDOW": str(cfg["context_window"]),
    }
