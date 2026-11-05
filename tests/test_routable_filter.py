#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_routable_filter.py — review of the "advertise only what can be executed" filter.

Read-only w.r.t. production code: ``kiro_crew.*`` is stubbed, ``openai_provider``
is loaded as a synthetic package (its ``__init__.py`` never runs), the trace file
is redirected into a temp dir, and every executor is a fake. No network, no spawn.

Run (Windows console needs UTF-8 or the emoji in the output kills it):
    $env:PYTHONIOENCODING='utf-8'; python tests\\test_routable_filter.py

Exit code 0 = every case PASSed.
"""
from __future__ import annotations

import importlib
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
    t.AcpEvent = AcpEvent
    t.TurnUsage = TurnUsage
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


# ── fixtures ───────────────────────────────────────────────────────────────
def tool(name: str) -> dict:
    return {"type": "function",
            "function": {"name": name, "description": f"{name} tool",
                         "parameters": {"type": "object", "properties": {}}}}


TOOLS4 = [tool("read"), tool("kb_read"), tool("ghost"), tool("save_notes")]


class FakeExec:
    def __init__(self, names):
        self._names = names
        self.calls = 0

    def routable_names(self):
        self.calls += 1
        return self._names


class BoomExec:
    def routable_names(self):
        raise RuntimeError("discovery blew up")


class NoMethodExec:
    pass


def make_provider(P, executor, defer=True, activated=None):
    obj = object.__new__(P.OpenAIProvider)
    obj._tool_defer = defer
    obj._activated_tools = set(activated or ())
    obj._tool_executor = executor
    return obj


def names(out) -> list[str]:
    return [(t.get("function") or {}).get("name") for t in out]


def trace_lines(P) -> list[dict]:
    path = Path(P._TRACE_PATH)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def clear_trace(P) -> None:
    Path(P._TRACE_PATH).write_text("", encoding="utf-8")


# ── cases ──────────────────────────────────────────────────────────────────
def main() -> int:
    print(f"# provider.py  {PKG_DIR / 'provider.py'}  "
          f"{__import__('datetime').datetime.fromtimestamp((PKG_DIR / 'provider.py').stat().st_mtime)}")
    print(f"# executor.py  mtime "
          f"{__import__('datetime').datetime.fromtimestamp((PKG_DIR / 'mcp_executor.py').stat().st_mtime)}\n")

    P, R, E = _load_modules()
    P._TRACE_PATH = os.path.join(tempfile.mkdtemp(prefix="routable_"), "trace.log")
    clear_trace(P)

    # a. routable = {read, kb_read} → only those survive; ghost/save_notes gone
    fake = FakeExec({"read", "kb_read"})
    out_a = P.OpenAIProvider._wire_tools(make_provider(P, fake), list(TOOLS4))
    got_a = names(out_a)
    check("a  routable={'read','kb_read'} → wire keeps only those",
          "read" in got_a and "kb_read" in got_a, f"sent={got_a}")
    check("a2 ghost/save_notes are NOT advertised", "ghost" not in got_a and "save_notes" not in got_a,
          f"sent={got_a}")
    check("a3 executor consulted exactly once per turn", fake.calls == 1, f"calls={fake.calls}")

    # a4. deferral still appends tool_search when a kept tool is non-core
    clear_trace(P)
    tools5 = TOOLS4 + [tool("work_ledger_record")]
    out_a4 = P.OpenAIProvider._wire_tools(
        make_provider(P, FakeExec({"read", "kb_read", "work_ledger_record"})), list(tools5))
    got_a4 = names(out_a4)
    check("a4 deferral still appends tool_search (non-core kept → loader present)",
          "tool_search" in got_a4 and "work_ledger_record" not in got_a4
          and {"read", "kb_read"} <= set(got_a4),
          f"sent={got_a4}")

    # b. empty routable → "no opinion", everything stays
    clear_trace(P)
    out_b = P.OpenAIProvider._wire_tools(make_provider(P, FakeExec(set()), defer=False), list(TOOLS4))
    check("b  routable=∅ → all 4 tools pass through untouched",
          names(out_b) == ["read", "kb_read", "ghost", "save_notes"], f"sent={names(out_b)}")
    check("b2 routable=∅ writes no tools_routable trace",
          not [x for x in trace_lines(P) if x.get("kind") == "tools_routable"],
          f"kinds={[x.get('kind') for x in trace_lines(P)]}")

    # b3. same, but with deferral on: filter still no-ops (deferral alone may trim)
    out_b3 = P.OpenAIProvider._wire_tools(make_provider(P, FakeExec(set())), list(TOOLS4))
    got_b3 = names(out_b3)
    check("b3 routable=∅ + deferral: core kept, filter did not drop anything extra",
          {"read", "kb_read", "save_notes"} <= set(got_b3) and "ghost" not in got_b3,
          f"sent={got_b3}")

    # c. no method / raising executor / no executor → no opinion, no crash
    clear_trace(P)
    out_c1 = P.OpenAIProvider._wire_tools(make_provider(P, NoMethodExec(), defer=False), list(TOOLS4))
    out_c2 = P.OpenAIProvider._wire_tools(make_provider(P, BoomExec(), defer=False), list(TOOLS4))
    out_c3 = P.OpenAIProvider._wire_tools(make_provider(P, None, defer=False), list(TOOLS4))
    check("c  executor without routable_names() → all 4 kept, no crash",
          names(out_c1) == ["read", "kb_read", "ghost", "save_notes"], f"sent={names(out_c1)}")
    check("c2 routable_names() raising → all 4 kept, no crash",
          names(out_c2) == ["read", "kb_read", "ghost", "save_notes"], f"sent={names(out_c2)}")
    check("c3 no executor at all → all 4 kept, no crash",
          names(out_c3) == ["read", "kb_read", "ghost", "save_notes"], f"sent={names(out_c3)}")
    check("c4 none of the degraded paths wrote tools_routable",
          not [x for x in trace_lines(P) if x.get("kind") == "tools_routable"],
          f"kinds={[x.get('kind') for x in trace_lines(P)]}")

    # d. a CORE name that is not routable must not appear in the core block
    clear_trace(P)
    out_d = P.OpenAIProvider._wire_tools(make_provider(P, FakeExec({"read"})), list(TOOLS4))
    got_d = names(out_d)
    check("d  dropped core name save_notes absent from the core block",
          "save_notes" not in got_d and "kb_read" not in got_d and "read" in got_d,
          f"sent={got_d}")
    check("d2 never sends an empty tool list (all-core kept → no loader needed)",
          bool(got_d), f"sent={got_d}")

    # e. trace line on filtering
    evts = [x for x in trace_lines(P) if x.get("kind") == "tools_routable"]
    ok_e = bool(evts)
    last = evts[-1] if evts else {}
    check("e  filtering writes kind=tools_routable with kept/of/dropped",
          ok_e and {"kept", "of", "dropped"} <= set(last)
          and last.get("kept") == 1 and last.get("of") == 4
          and "ghost" in str(last.get("dropped")) and "save_notes" in str(last.get("dropped")),
          f"last={json.dumps(last, ensure_ascii=False)[:160]}")

    # f. kept empty → filter must not empty the list (no trace either)
    clear_trace(P)
    out_f = P.OpenAIProvider._wire_tools(make_provider(P, FakeExec({"nobody"}), defer=False),
                                         list(TOOLS4))
    check("f  kept=∅ → list left intact (filter never empties the wire)",
          names(out_f) == ["read", "kb_read", "ghost", "save_notes"], f"sent={names(out_f)}")
    check("f2 kept=∅ writes no tools_routable",
          not [x for x in trace_lines(P) if x.get("kind") == "tools_routable"],
          f"kinds={[x.get('kind') for x in trace_lines(P)]}")

    # g. real executor contract: handlers ∪ _tool_server_map
    R.register_all_builtins()
    E._tool_server_map.clear()
    E._tool_server_map.update({"ext_tool": "srvX", "kb_read": "obsidian-kb"})
    real = E.McpToolExecutor()
    # Discovery finished: routable_names() answers "no opinion" (empty) until
    # the first fetch has run, so a real executor must be marked discovered.
    real._definitions_cache = []
    routable = real.routable_names()
    check("g  real McpToolExecutor.routable_names() = handlers ∪ _tool_server_map",
          {"read", "spawn_sub_agents", "ext_tool", "kb_read"} <= routable
          and "ghost" not in routable and "save_notes" not in routable,
          f"n={len(routable)} sample={sorted(routable)[:6]}")
    out_g = P.OpenAIProvider._wire_tools(make_provider(P, real), list(TOOLS4))
    got_g = names(out_g)
    check("g2 provider._routable_names() sees the real executor (kb_read routable, save_notes not)",
          "kb_read" in got_g and "save_notes" not in got_g and "ghost" not in got_g,
          f"sent={got_g}")

    print()
    print(f"# summary: {len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("# FAILED: " + "; ".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
