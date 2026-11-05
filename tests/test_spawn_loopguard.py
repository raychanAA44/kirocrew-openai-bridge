"""A spawned sub-agent that repeats one tool must be steered, then stopped.

One real run made 236 execute_bash calls in six minutes across 131 turns and
ended only when it was abandoned. These checks pin the guard that ends that
shape instead of leaving it to a human to notice.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

PASSED: list[str] = []
FAILED: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"   [{detail}]" if detail else ""))
    (PASSED if ok else FAILED).append(label)


def load_module():
    path = Path(__file__).resolve().parents[1] / "vendor" / "openai_provider" / "mcp_handler_registry.py"
    spec = importlib.util.spec_from_file_location("mcp_handler_registry_loop", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    R = load_module()
    handler = R.SpawnSubAgentsHandler()

    posted: list[tuple[str, dict]] = []
    deleted: list[str] = []

    async def fake_post(path, body):
        posted.append((path, body))
        return {"ok": True}

    async def fake_delete(path):
        deleted.append(path)
        return {"ok": True}

    R._gateway_post = fake_post
    R._gateway_delete = fake_delete

    # 1. a changing tool never triggers anything
    watch: dict = {}
    notes: list[str] = []
    for turns, tool in ((10, "read"), (11, "grep"), (12, "read")):
        asyncio.run(handler._watch_for_loop("a1", {"turns": turns, "last_tool": tool}, watch, notes))
    check("1  a changing last_tool never steers", posted == [] and notes == [],
          f"posted={len(posted)}")

    # 2. same tool for LOOP_TURNS turns → exactly one steer, interrupt mode
    watch, notes = {}, []
    base = 100
    asyncio.run(handler._watch_for_loop("a2", {"turns": base, "last_tool": "execute_bash"}, watch, notes))
    for i in range(1, handler._LOOP_TURNS + 1):
        asyncio.run(handler._watch_for_loop(
            "a2", {"turns": base + i, "last_tool": "execute_bash"}, watch, notes))
    check("2  repeats reach the window → one steer to /steer in interrupt mode",
          len(posted) == 1 and posted[0][0] == "/api/spawn/a2/steer"
          and posted[0][1].get("mode") == "interrupt"
          and "execute_bash" in posted[0][1].get("message", ""),
          f"posted={posted[:1]}")
    check("3  the steer is recorded in the notes", len(notes) == 1, f"notes={notes}")

    # 4. it keeps repeating after the steer → the run is deleted, once
    for i in range(handler._LOOP_TURNS + 1, handler._LOOP_TURNS * 2 + 1):
        asyncio.run(handler._watch_for_loop(
            "a2", {"turns": base + i, "last_tool": "execute_bash"}, watch, notes))
    check("4  still looping after the steer → deleted once",
          deleted == ["/api/spawn/a2"], f"deleted={deleted}")
    check("5  the watch marks it stopped", watch["a2"].get("stopped") is True, f"watch={watch['a2']}")

    # 6. no tool reported (older payloads) → nothing happens
    watch, notes = {}, []
    for turns in range(0, 60, 5):
        asyncio.run(handler._watch_for_loop("a3", {"turns": turns, "last_tool": ""}, watch, notes))
    check("6  an empty last_tool never triggers", posted[1:] == [] and notes == [],
          f"posted_extra={len(posted) - 1}")

    # 7. a default budget is applied when the caller names none
    check("7  _DEFAULT_MAX_TURNS is a finite budget", R._DEFAULT_MAX_TURNS > 0,
          f"default={R._DEFAULT_MAX_TURNS}")

    # 8. the guard window is shorter than the wait that can observe it
    check("8  loop window is observable inside one wait", handler._LOOP_TURNS > 0,
          f"loop_turns={handler._LOOP_TURNS}")

    print()
    print(f"# summary: {len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("# FAILED: " + "; ".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
