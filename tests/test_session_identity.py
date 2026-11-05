#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_session_identity.py — review of the bound-identity fix (parent_session / tokens).

Read-only w.r.t. production code: ``kiro_crew.*`` is stubbed, ``openai_provider``
is loaded as a synthetic package, the trace file is redirected to a temp dir, and
every HTTP helper is either a recording stub or a fake ``urlopen``. No network, no
spawn, no gateway.

Run (Windows console needs UTF-8, the output carries emoji):
    $env:PYTHONIOENCODING='utf-8'; python tests\\test_session_identity.py

Exit code 0 = every case PASSed.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import tempfile
import types
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENDOR = HERE.parent / "vendor"
PKG_DIR = VENDOR / "openai_provider"

PASSED: list[str] = []
FAILED: list[str] = []

ENV_NAME = "KIROCREW_SESSION_KEY"


def check(label: str, ok: bool, detail: str = "") -> bool:
    (PASSED if ok else FAILED).append(label)
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"   [{detail}]" if detail else ""))
    return ok


# ── bootstrap ──────────────────────────────────────────────────────────────
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
    t.AcpEvent, t.TurnUsage = AcpEvent, TurnUsage
    b = types.ModuleType("kiro_crew.providers.base")

    class LLMProvider:
        pass

    class CancelOutcome:
        pass

    b.LLMProvider, b.CancelOutcome = LLMProvider, CancelOutcome
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


class FakeResp:
    def __init__(self, payload: bytes = b"{}"):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def main() -> int:
    print(f"# registry.py {PKG_DIR / 'mcp_handler_registry.py'}  "
          f"{__import__('datetime').datetime.fromtimestamp((PKG_DIR / 'mcp_handler_registry.py').stat().st_mtime)}")
    print(f"# executor.py mtime "
          f"{__import__('datetime').datetime.fromtimestamp((PKG_DIR / 'mcp_executor.py').stat().st_mtime)}\n")

    P, R, E = _load_modules()
    P._TRACE_PATH = os.path.join(tempfile.mkdtemp(prefix="ident_"), "trace.log")

    saved_env = os.environ.get(ENV_NAME)
    real_post_sync = R._gateway_post_sync      # restored before the on-the-wire check
    real_get_sync = R._gateway_get_sync

    # ── a. bound identity wins over env ────────────────────────────────────
    os.environ[ENV_NAME] = "env-other-session"
    tok = E.set_tool_identity("bound-key", "bound-token")
    try:
        got_a = R._resolve_session_key()
        bound_a = E._TOOL_IDENTITY.get()
    finally:
        E.reset_tool_identity(tok)
    check("a  bound identity wins over env KIROCREW_SESSION_KEY",
          got_a == "bound-key" and bound_a.get("session_key") == "bound-key",
          f"resolved={got_a!r} contextvar={bound_a}")

    # ── b. env fallback, then nothing ──────────────────────────────────────
    E._TOOL_IDENTITY.set({})
    got_b1 = R._resolve_session_key()
    os.environ.pop(ENV_NAME, None)
    got_b2 = R._resolve_session_key()
    check("b  unbound → falls back to env value", got_b1 == "env-other-session", f"resolved={got_b1!r}")
    check("b2 unbound + no env → returns '' (str), no crash",
          got_b2 == "" and isinstance(got_b2, str), f"resolved={got_b2!r} type={type(got_b2).__name__}")

    # ── c. token reaches the sync helpers (and survives to_thread) ─────────
    rec_posts: list[dict] = []
    rec_gets: list[dict] = []

    def fake_post_sync(path, body, secret, session_key, api, token=""):
        rec_posts.append({"path": path, "session_key": session_key, "token": token,
                          "thread_ident": (E._TOOL_IDENTITY.get() or {})})
        return {"id": "a1"}

    def fake_get_sync(path, secret, session_key, api, token=""):
        rec_gets.append({"path": path, "session_key": session_key, "token": token,
                         "thread_ident": (E._TOOL_IDENTITY.get() or {})})
        return {"agents": []}

    R._gateway_post_sync, R._gateway_get_sync = fake_post_sync, fake_get_sync
    R._resolve_gateway_api = lambda: "http://127.0.0.1:0"
    R._internal_secret = lambda: "secret-test"

    os.environ[ENV_NAME] = "env-other-session"
    tok = E.set_tool_identity("bound-key", "bound-token")
    try:
        asyncio.run(R._gateway_post("/api/spawn", {"task": "x"}))
        asyncio.run(R._gateway_get("/api/spawn"))
    finally:
        E.reset_tool_identity(tok)
    p0 = rec_posts[0] if rec_posts else {}
    g0 = rec_gets[0] if rec_gets else {}
    check("c  _gateway_post passes the bound token to the sync helper",
          p0.get("token") == "bound-token" and p0.get("session_key") == "bound-key",
          f"post={ {k: p0.get(k) for k in ('session_key', 'token')} }")
    check("c2 _gateway_get passes the bound token too",
          g0.get("token") == "bound-token" and g0.get("session_key") == "bound-key",
          f"get={ {k: g0.get(k) for k in ('session_key', 'token')} }")
    check("c3 asyncio.to_thread copies the contextvar (same identity in the worker)",
          p0.get("thread_ident", {}).get("token") == "bound-token"
          and g0.get("thread_ident", {}).get("token") == "bound-token",
          f"thread_post={p0.get('thread_ident')} thread_get={g0.get('thread_ident')}")

    # unbound → no token handed over
    rec_posts.clear()
    rec_gets.clear()
    E._TOOL_IDENTITY.set({})
    os.environ[ENV_NAME] = "env-other-session"
    asyncio.run(R._gateway_post("/api/spawn", {}))
    asyncio.run(R._gateway_get("/api/spawn"))
    check("c4 unbound → empty token passed, key still declared from env",
          rec_posts[0]["token"] == "" and rec_posts[0]["session_key"] == "env-other-session"
          and rec_gets[0]["token"] == "",
          f"post={ {k: rec_posts[0].get(k) for k in ('session_key', 'token')} }")

    # ── d. _session_headers rules (real function) ─────────────────────────
    h_full = R._session_headers("sec", "sk", "tk")
    h_nokey = R._session_headers("sec", "", "tk")
    h_notok = R._session_headers("sec", "sk", "")
    check("d  key+token → X-Session-Key and X-Session-Token both sent",
          h_full.get("X-Internal-Secret") == "sec" and h_full.get("X-Session-Key") == "sk"
          and h_full.get("X-Session-Token") == "tk", f"headers={sorted(h_full)}")
    check("d2 no key → token suppressed even when one exists",
          "X-Session-Token" not in h_nokey and "X-Session-Key" not in h_nokey,
          f"headers={sorted(h_nokey)}")
    check("d3 key without token → key only", "X-Session-Key" in h_notok
          and "X-Session-Token" not in h_notok, f"headers={sorted(h_notok)}")

    # d4. the real sync helper emits those headers on the wire (fake urlopen)
    captured: list[dict] = []
    real_urlopen = urllib.request.urlopen

    def fake_urlopen(req, timeout=None):
        captured.append({"url": req.full_url, "method": req.get_method(),
                         "headers": dict(req.header_items())})
        return FakeResp(b'{"ok": true}')

    urllib.request.urlopen = fake_urlopen
    try:
        real_post_sync("/api/spawn", {"task": "x"}, "sec", "sk", "http://127.0.0.1:0", "tk")
        real_post_sync("/api/spawn", {"task": "x"}, "sec", "sk", "http://127.0.0.1:0", "")
        real_post_sync("/api/spawn", {"task": "x"}, "sec", "", "http://127.0.0.1:0", "tk")
    finally:
        urllib.request.urlopen = real_urlopen
    hdr0 = {k.lower(): v for k, v in captured[0]["headers"].items()}
    hdr1 = {k.lower(): v for k, v in captured[1]["headers"].items()}
    hdr2 = {k.lower(): v for k, v in captured[2]["headers"].items()}
    check("d4 _gateway_post_sync puts X-Session-Token on the wire",
          hdr0.get("x-session-token") == "tk" and hdr0.get("x-session-key") == "sk"
          and hdr0.get("x-internal-secret") == "sec" and captured[0]["method"] == "POST",
          f"headers={sorted(hdr0)}")
    check("d5 no token → header absent on the wire", "x-session-token" not in hdr1,
          f"headers={sorted(hdr1)}")
    check("d6 no key → neither key nor token header on the wire",
          "x-session-key" not in hdr2 and "x-session-token" not in hdr2,
          f"headers={sorted(hdr2)}")

    # ── e. SpawnListHandler uses GET /api/spawn, never POST ───────────────
    post_calls: list[tuple[str, dict]] = []
    get_calls: list[str] = []
    list_payload = {"agents": [{"id": "a1", "done": False, "task": "t1"},
                               {"id": "a2", "done": True, "task": "t2", "error": "boom"}]}

    async def fake_post(path, body):
        post_calls.append((path, body))
        return {"error": "POST must not be used by spawn_list"}

    async def fake_get(path):
        get_calls.append(path)
        return list_payload

    R._gateway_post, R._gateway_get = fake_post, fake_get
    h_list = R.SpawnListHandler()
    out_e = asyncio.run(h_list.execute({}))
    check("e  spawn_list goes through GET /api/spawn and never POST",
          get_calls == ["/api/spawn"] and post_calls == [],
          f"get={get_calls} post={[p for p, _ in post_calls]}")
    check("e2 rendered list shows running/done + error",
          "a1" in out_e and "[running]" in out_e and "a2" in out_e
          and "[done]" in out_e and "error: boom" in out_e,
          f"out={out_e!r}")

    async def fake_get_err(path):
        get_calls.append(path)
        return {"error": "x"}

    R._gateway_get = fake_get_err
    out_e3 = asyncio.run(h_list.execute({}))
    check("e3 {'error':'x'} → 'Error: x'", out_e3 == "Error: x", f"out={out_e3!r}")
    check("e4 definition name is spawn_list",
          h_list.definition()["function"]["name"] == "spawn_list",
          f"name={h_list.definition()['function']['name']!r}")

    # ── f. SpawnRunHandler inherits the bound identity ───────────────────
    run_posts: list[tuple[str, dict]] = []

    async def fake_post_run(path, body):
        run_posts.append((path, body))
        return {"id": "rid1"}

    R._gateway_post = fake_post_run
    tok = E.set_tool_identity("bound-key", "bound-token")
    try:
        out_f = asyncio.run(R.SpawnRunHandler().execute({"task": "hello"}))
    finally:
        E.reset_tool_identity(tok)
    body_f = run_posts[0][1] if run_posts else {}
    check("f  spawn_run posts to /api/spawn with the BOUND parent_session",
          run_posts and run_posts[0][0] == "/api/spawn"
          and body_f.get("parent_session") == "bound-key" and body_f.get("task") == "hello",
          f"body={ {k: body_f.get(k) for k in ('task', 'parent_session')} }")
    check("f2 spawn_run reports the child id", "rid1" in out_f, f"out={out_f!r}")

    # ── g. MCP child env carries the same key+token pair ─────────────────
    tok = E.set_tool_identity("bound-key", "bound-token")
    try:
        env_g = E._child_env()
    finally:
        E.reset_tool_identity(tok)
    E._TOOL_IDENTITY.set({})
    os.environ[ENV_NAME] = "env-other-session"
    env_g2 = E._child_env()
    check("g  _child_env writes the bound pair (KIROCREW_SESSION_KEY + STUB_SESSION_TOKEN)",
          env_g.get("KIROCREW_SESSION_KEY") == "bound-key"
          and env_g.get("KIROCREW_STUB_SESSION_TOKEN") == "bound-token",
          f"key={env_g.get('KIROCREW_SESSION_KEY')!r} token={env_g.get('KIROCREW_STUB_SESSION_TOKEN')!r}")
    check("g2 unbound → stale key/token are REMOVED, not inherited",
          env_g2.get("KIROCREW_STUB_SESSION_TOKEN") is None
          and env_g2.get("KIROCREW_SESSION_KEY") is None,
          f"key={env_g2.get('KIROCREW_SESSION_KEY')!r} token={env_g2.get('KIROCREW_STUB_SESSION_TOKEN')!r}")

    # restore env
    if saved_env is None:
        os.environ.pop(ENV_NAME, None)
    else:
        os.environ[ENV_NAME] = saved_env

    print()
    print(f"# summary: {len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("# FAILED: " + "; ".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
