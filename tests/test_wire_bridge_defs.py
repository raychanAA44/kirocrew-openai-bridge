#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_wire_bridge_defs.py — adversarial check of the bridge-only tool append (a800a26).

Three cases only (review scope, not a feature suite):
  1. normal path appends the 4 bridge-only defs exactly once, and each has a handler;
  2. gap: deferral off → none of them is on the wire (crew's own copies aside);
  3. gap: crew sends only core tools → early return → none of them is on the wire.

Read-only w.r.t. production code (kiro_crew stubbed, no network, no spawn).

Run:
    $env:PYTHONIOENCODING='utf-8'; python tests\\test_wire_bridge_defs.py
"""
from __future__ import annotations

import importlib
import sys
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


def _load():
    _stub_kiro_crew()
    pkg = types.ModuleType("openai_provider")
    pkg.__path__ = [str(PKG_DIR)]
    sys.modules["openai_provider"] = pkg
    P = importlib.import_module("openai_provider.provider")
    R = importlib.import_module("openai_provider.mcp_handler_registry")
    return P, R


def tool(name: str) -> dict:
    return {"type": "function",
            "function": {"name": name, "description": name,
                         "parameters": {"type": "object", "properties": {}}}}


class AllRoutable:
    """Executor stub mirroring McpToolExecutor's two lookups.

    ``routable_names()`` empty = "no opinion" (the wire filter stays a no-op);
    ``handler_definition()`` is what ``_bridge_only_defs`` actually calls.
    """

    def routable_names(self):
        return set()

    def handler_definition(self, tool_name):
        from openai_provider.mcp_handler_registry import McpHandlerRegistry
        return McpHandlerRegistry.definition_for(tool_name)


def make_provider(P, defer=True, activated=()):
    obj = object.__new__(P.OpenAIProvider)
    obj._tool_defer = defer
    obj._activated_tools = set(activated)
    obj._tool_executor = AllRoutable()
    return obj


def names(tools):
    return [(t.get("function") or {}).get("name") for t in tools]


def main() -> int:
    P, R = _load()
    R.register_all_builtins()
    bridge_names = list(P._BRIDGE_ONLY_TOOLS)
    print(f"# _BRIDGE_ONLY_TOOLS = {bridge_names}\n")

    # crew-like turn: 18 core names + a few non-core MCP tools
    crew_tools = [tool(n) for n in sorted(P._CORE_TOOL_NAMES)]
    crew_tools += [tool(n) for n in ("cron_list", "work_ledger_read", "ghost")]

    # ── 1. normal path: appended once, and each one is really executable ──
    out1 = P.OpenAIProvider._wire_tools(make_provider(P), list(crew_tools))
    n1 = names(out1)
    appended = [n for n in n1 if n in bridge_names]
    missing_handler = [n for n in bridge_names if R.McpHandlerRegistry.get(n) is None]
    check("1  normal path appends all 4 bridge-only defs exactly once",
          appended == bridge_names and len(set(appended)) == len(appended),
          f"appended={appended} sent={len(n1)}")
    check("1b every appended name has a registered handler (advertised == executable)",
          not missing_handler, f"missing_handler={missing_handler}")
    check("1c tool_search still rides along", "tool_search" in n1, f"sent={n1[-6:]}")

    # 1d. crew's own copy already loaded → no duplicate definition on the wire
    out1d = P.OpenAIProvider._wire_tools(
        make_provider(P, activated={"spawn_run"}), list(crew_tools))
    n1d = names(out1d)
    check("1d crew's own spawn_run loaded → no duplicate spawn_run on the wire",
          n1d.count("spawn_run") == 1, f"count={n1d.count('spawn_run')}")

    # ── 2. deferral off (HUB_TOOL_DEFER=0 / 400 fallback) still carries them ─
    out2 = P.OpenAIProvider._wire_tools(make_provider(P, defer=False), list(crew_tools))
    n2 = names(out2)
    present2 = [n for n in bridge_names if n in n2]
    check("2  deferral off → bridge-only defs still appended",
          present2 == list(bridge_names), f"present={present2}")

    # ── 3. crew sends only core tools → early return still carries them ─────
    only_core = [tool(n) for n in sorted(P._CORE_TOOL_NAMES)]
    out3 = P.OpenAIProvider._wire_tools(make_provider(P), list(only_core))
    n3 = names(out3)
    present3 = [n for n in bridge_names if n in n3]
    check("3  all-core tool list → bridge-only defs still appended",
          present3 == list(bridge_names), f"present={present3} sent={len(n3)}")

    print()
    print(f"# summary: {len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("# FAILED: " + "; ".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
