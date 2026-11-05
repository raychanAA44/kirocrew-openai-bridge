#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_tool_search.py — independent review of the on-demand tool loader.

Read-only w.r.t. production code: this file never imports the bridge's runtime
dependencies for real (``kiro_crew.*`` is stubbed before ``provider.py`` loads),
never touches the network, and never writes outside a temp dir.

Run:
    python tests/test_tool_search.py      (or: py -3 tests/test_tool_search.py)

Exit code 0 = every case PASSed.
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENDOR = HERE.parent / "vendor"
PKG_DIR = VENDOR / "openai_provider"
PROVIDER_PY = PKG_DIR / "provider.py"

PASSED: list[str] = []
FAILED: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    (PASSED if ok else FAILED).append(label)
    mark = "PASS" if ok else "FAIL"
    line = f"{mark}  {label}"
    if detail:
        line += f"   [{detail}]"
    print(line)
    return ok


# ── 1. stub kiro_crew so provider.py can be exec'd without the app ──────────
def _stub_kiro_crew() -> None:
    for name in ("kiro_crew", "kiro_crew.acp", "kiro_crew.providers"):
        sys.modules.setdefault(name, types.ModuleType(name))

    types_mod = types.ModuleType("kiro_crew.acp.types")

    class AcpEvent:  # noqa: D401 - dummy
        def __init__(self, *args, **kwargs):
            self.args, self.kwargs = args, kwargs

    class TurnUsage:
        def __init__(self, *args, **kwargs):
            self.args, self.kwargs = args, kwargs

    for const in (
        "EVENT_TEXT_CHUNK", "EVENT_THINKING_CHUNK", "EVENT_TOOL_CALL",
        "EVENT_TOOL_CALL_UPDATE", "EVENT_TOOL_RESULT", "EVENT_PERMISSION_REQUEST",
        "EVENT_COMPLETE", "EVENT_CLEAR_STATUS",
        "STOP_REASON_END_TURN", "STOP_REASON_CANCELLED",
        "OPTION_ALLOW_ONCE", "OPTION_ALLOW_ALWAYS",
    ):
        setattr(types_mod, const, const.lower())
    types_mod.AcpEvent = AcpEvent
    types_mod.TurnUsage = TurnUsage

    base_mod = types.ModuleType("kiro_crew.providers.base")

    class LLMProvider:  # noqa: D401 - dummy base
        pass

    class CancelOutcome:  # noqa: D401 - dummy
        pass

    base_mod.LLMProvider = LLMProvider
    base_mod.CancelOutcome = CancelOutcome

    sys.modules["kiro_crew.acp.types"] = types_mod
    sys.modules["kiro_crew.providers.base"] = base_mod
    sys.modules["kiro_crew"].acp = sys.modules["kiro_crew.acp"]
    sys.modules["kiro_crew"].providers = sys.modules["kiro_crew.providers"]
    sys.modules["kiro_crew.acp"].types = types_mod


def _load_provider():
    """Import provider.py as ``openai_provider.provider`` without running __init__.py."""
    _stub_kiro_crew()
    pkg = types.ModuleType("openai_provider")
    pkg.__path__ = [str(PKG_DIR)]
    sys.modules["openai_provider"] = pkg
    spec = importlib.util.spec_from_file_location("openai_provider.provider", PROVIDER_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["openai_provider.provider"] = mod
    spec.loader.exec_module(mod)
    return mod


# ── 2. fake tool pool (20 tools, English descriptions) ──────────────────────
def _mk(name: str, desc: str, params: dict | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": params or {"type": "object", "properties": {}},
        },
    }


POOL = [
    _mk("work_ledger_record",
        "Create and record a work ledger entry: bind a session, write status, "
        "send the result to the ledger.", {"type": "object", "properties": {"entry": {"type": "string"}}, "required": ["entry"]}),
    _mk("work_ledger_read", "Read work ledger entries for a session or role."),
    _mk("work_ledger_update", "Update an existing work ledger record."),
    _mk("session_create", "Create a session and bind it to an agent role."),
    _mk("session_send", "Send a message to a bound session and get the reply."),
    _mk("session_close", "Close a session and release its slot."),
    _mk("monitor_start", "Start monitoring a session or job and watch its output."),
    _mk("monitor_stop", "Stop a running monitor."),
    _mk("cron_list", "List cron jobs and scheduled tasks."),
    _mk("cron_create", "Create a cron job that runs a task on a schedule."),
    _mk("schedule_task", "Schedule a one-off task for a future time."),
    _mk("memory_recall", "Search long-term memory for facts about the user."),
    _mk("memory_save", "Save a durable note into long-term memory."),
    _mk("spawn_sub_agents", "Spawn sub agents that run tasks in parallel."),
    _mk("subagent_status", "Show the status of every spawned sub agent."),
    _mk("agent_list", "List the available agent roles and their models."),
    _mk("task_assign", "Assign a task to an agent role."),
    _mk("task_complete", "Mark a task as complete with a summary."),
    _mk("log_read", "Read a log file from the session workspace."),
    _mk("log_tail", "Tail the newest lines of a log file."),
    _mk("watch_start", "Watch a file or directory for changes."),
    _mk("kb_search", "Full-text search of the knowledge base."),
]

REQUIRED_POOL = {
    "work_ledger_record", "work_ledger_read", "session_create", "session_send",
    "monitor_start", "cron_list", "memory_recall", "spawn_sub_agents",
}

Q1 = ("work ledger record create bind session create send role-b-worker "
      "role-c-worker monitor_start")
Q2 = "subagent spawn agents sessions task ledger monitor start watch work-ledger"

_LOADED_RE = re.compile(r"^-\s+([A-Za-z0-9_]+)")


def loaded_names(text: str) -> list[str]:
    return [m.group(1) for m in (_LOADED_RE.match(ln) for ln in text.splitlines()) if m]


def make_provider(P, holder: dict | None = None):
    obj = object.__new__(P.OpenAIProvider)          # bypass __init__ (no network, no env)
    obj._all_tools = list(POOL)
    obj._activated_tools = set()
    if holder is not None:
        holder["obj"] = obj
    return obj


# ── 3. cases ────────────────────────────────────────────────────────────────
def main() -> int:
    if not PROVIDER_PY.exists():
        print(f"FATAL provider.py not found: {PROVIDER_PY}")
        return 2
    print(f"# provider.py  {PROVIDER_PY}")
    print(f"# mtime        {__import__('datetime').datetime.fromtimestamp(PROVIDER_PY.stat().st_mtime)}")
    print(f"# size/lines   {PROVIDER_PY.stat().st_size} B / {len(PROVIDER_PY.read_text(encoding='utf-8').splitlines())} lines\n")

    P = _load_provider()
    P._TRACE_PATH = os.path.join(tempfile.mkdtemp(prefix="tool_search_"), "trace.log")

    # 0. symbols present (helper functions are module-level; _load_tools is a method)
    sym = {n: hasattr(P, n) for n in ("_words", "_score_tools", "_tool_name",
                                      "_tool_description", "_trace")}
    sym["_load_tools(method)"] = callable(getattr(P.OpenAIProvider, "_load_tools", None))
    sym["_TOOL_SEARCH_DEF"] = isinstance(getattr(P, "_TOOL_SEARCH_DEF", None), dict)
    sym["_CORE_TOOL_NAMES"] = bool(getattr(P, "_CORE_TOOL_NAMES", None))
    check("0  symbols present: _load_tools/_words/_score_tools/_trace/_TOOL_SEARCH_DEF/_CORE_TOOL_NAMES",
          all(sym.values()), ", ".join(f"{k}={v}" for k, v in sym.items()))
    check("0b pool has >=20 tools incl. required 8",
          len(POOL) >= 20 and REQUIRED_POOL <= {t["function"]["name"] for t in POOL},
          f"n={len(POOL)}")

    # a. real word-bag query #1
    pa = make_provider(P)
    ta = pa._load_tools([], Q1)
    na = loaded_names(ta)
    check("a  Q1 hits work_ledger_record", "work_ledger_record" in na, f"loaded={na[:6]}")
    check("a2 Q1 also hits session_create/session_send/monitor_start",
          any(x in na for x in ("session_create", "session_send", "monitor_start")),
          f"loaded={na[:6]}")

    # g (extra). real word-bag query #2
    pg = make_provider(P)
    tg = pg._load_tools([], Q2)
    ng = loaded_names(tg)
    check("G  Q2 (subagent/ledger/monitor bag) loads >=1 relevant tool",
          bool(ng) and any(x in ng for x in ("work_ledger_record", "monitor_start", "cron_list",
                                             "task_assign", "subagent_status", "log_tail")),
          f"loaded={ng[:6]}")

    # b. names only, empty query
    pb = make_provider(P)
    tb = pb._load_tools(["monitor_start"], "")
    nb = loaded_names(tb)
    check("b  names=['monitor_start'] + empty query loads it", nb == ["monitor_start"], f"loaded={nb}")

    # c. names + query together, names kept
    pc = make_provider(P)
    tc = pc._load_tools(["monitor_start"], "cron schedule task list")
    nc = loaded_names(tc)
    check("c  names+query merged, no exception, names survives", "monitor_start" in nc, f"loaded={nc[:8]}")

    # d. Chinese query: no crash; 0-hit text must list candidates + names hint
    pd = make_provider(P)
    try:
        td = pd._load_tools([], "读取日志")
        crash = ""
    except Exception as exc:  # noqa: BLE001
        td, crash = "", f"{type(exc).__name__}: {exc}"
    check("d  Chinese query does not raise", not crash, crash)
    cands = [n for n in ("work_ledger_record", "session_create", "cron_list", "log_read")
             if n in td]
    check("d2 Chinese 0-hit text lists candidate tool names", bool(cands), f"found={cands}")
    check("d3 Chinese 0-hit text tells the caller to use names:", "names" in td,
          f"head={td[:120]!r}")

    # e. junk query: candidate list, no raise
    pe = make_provider(P)
    try:
        te = pe._load_tools([], "zzzz")
        crash = ""
    except Exception as exc:  # noqa: BLE001
        te, crash = "", f"{type(exc).__name__}: {exc}"
    check("e  junk query 'zzzz' does not raise", not crash, crash)
    check("e2 junk query returns a candidate list (>=5 names)",
          len(re.findall(r"[a-z0-9_]+", te)) >= 5 and "names" in te, f"head={te[:120]!r}")
    check("e3 junk query text is not the bare old dead-end sentence",
          "Try different keywords, or name the tool exactly" not in te, f"head={te[:120]!r}")

    # f. idempotent: same query twice
    pf = make_provider(P)
    t1 = pf._load_tools([], Q1)
    act1 = set(pf._activated_tools)
    t2 = pf._load_tools([], Q1)
    act2 = set(pf._activated_tools)
    check("f  same query twice: no raise, same activation set",
          act1 == act2 and loaded_names(t1) == loaded_names(t2),
          f"n1={len(act1)} n2={len(act2)}")

    # schema: required["query"] must be gone
    params = P._TOOL_SEARCH_DEF["function"]["parameters"]
    required = params.get("required") or []
    check("s1 schema no longer requires 'query'", "query" not in required, f"required={required!r}")
    check("s2 schema still exposes query + names",
          set(params.get("properties") or {}) >= {"query", "names"},
          f"props={sorted((params.get('properties') or {}).keys())}")

    # schema: description examples must be word bags, not a long phrase
    desc = P._TOOL_SEARCH_DEF["function"]["description"]
    examples = re.findall(r'\{"query": "([^"]*)"\}', desc)
    longest = max((len(e.split()) for e in examples), default=0)
    check("s3 description examples are <=4 separate words", longest <= 4,
          f"examples={examples} longest={longest} words")

    # unit: _words splits snake/camel/punctuation
    got = P._words("workLedgerRecord create_bind, monitor-start!")
    check("u1 _words peels snake/camel/punctuation",
          got == ["work", "ledger", "record", "create", "bind", "monitor", "start"], f"{got}")

    # unit: name hits outweigh description hits
    mini = [_mk("alpha_tool", "nothing here"),
            _mk("beta_tool", "mentions alpha only in prose")]
    ranked = P._score_tools(["alpha"], mini)
    check("u2 name hit outranks description hit",
          bool(ranked) and ranked[0][1] == "alpha_tool" and ranked[0][0] > (ranked[1][0] if len(ranked) > 1 else 0),
          f"{ranked}")

    print()
    print(f"# summary: {len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("# FAILED: " + "; ".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
