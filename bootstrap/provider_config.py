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


def _looks_like_key(value: str) -> bool:
    """Reject values that are obviously not a bearer token.

    Catches the whole-JSON-accidentally-copied case, which otherwise surfaces at
    request time as httpx's ``Illegal header value b'Bearer {\\r\\n ...'``.
    """
    if not value or len(value) > 200:
        return False
    if any(ch in value for ch in '{}"\r\n\t '):
        return False
    return True


def resolve() -> dict:
    own = _read(PROVIDER_JSON)

    cfg: dict = {
        "base_url": str(os.environ.get("OPENAI_BASE_URL") or own.get("base_url") or ""),
        "model": str(os.environ.get("OPENAI_MODEL") or own.get("model") or ""),
        "api_key": str(os.environ.get("OPENAI_API_KEY") or own.get("api_key") or ""),
        "max_tokens": int(os.environ.get("OPENAI_MAX_TOKENS") or own.get("max_tokens") or 32768),
        "context_window": int(
            os.environ.get("OPENAI_CONTEXT_WINDOW") or own.get("context_window") or 1000000
        ),
        # "" defers to KiroCrew's agent.reasoning_effort, which is the setting the
        # dashboard writes. The provider drops the level anyway unless the served
        # model documents it, so an inherited level cannot 400 a subagent route.
        "reasoning_effort": str(
            os.environ.get("OPENAI_REASONING_EFFORT") or own.get("reasoning_effort") or ""
        ).strip().lower(),
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

    # These two are what a FRESH install lands on when nothing else is configured,
    # and the old pair was a trap: 8888 was a gateway that rejects this key, and
    # gpt-4o-mini is served by none of the endpoints, so the first request failed
    # with "Illegal header value" or "Model is unavailable" and nothing said why.
    # A working default matters more than a generic-looking one.
    cfg["base_url"] = cfg["base_url"].strip().rstrip("/") or "http://127.0.0.1:28791/v1"
    cfg["model"] = _strip_provider(cfg["model"].strip()) or "oc/beam-free"

    # A key that is not a key (whole JSON pasted in, stray newline, ...) produces
    # "Illegal header value b'Bearer ...'" at request time and is hard to trace
    # back to its file. Drop it here and fall back to the KiroCrew-managed id.
    cfg["api_key"] = "".join(cfg["api_key"].split())
    if not _looks_like_key(cfg["api_key"]):
        fallback = str(options.get("apiKey") or "").strip()
        cfg["api_key"] = "".join(fallback.split()) if _looks_like_key(fallback) else ""
        cfg["sources"]["api_key"] = "opencode.json (provider.json key rejected)" if cfg["api_key"] else "none (no usable key)"
    return cfg


def as_env(cfg: dict | None = None) -> dict:
    """Return the ``OPENAI_*`` environment mapping for a resolved config."""
    cfg = cfg or resolve()
    env = {
        "OPENAI_BASE_URL": cfg["base_url"],
        "OPENAI_API_KEY": cfg["api_key"],
        "OPENAI_MODEL": cfg["model"],
        "OPENAI_MAX_TOKENS": str(cfg["max_tokens"]),
        "OPENAI_CONTEXT_WINDOW": str(cfg["context_window"]),
    }
    if cfg.get("reasoning_effort"):
        env["OPENAI_REASONING_EFFORT"] = cfg["reasoning_effort"]
    return env
