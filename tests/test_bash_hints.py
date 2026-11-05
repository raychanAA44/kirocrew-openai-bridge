"""cmd.exe pitfalls must be NAMED, not just suffered.

A worker once spent 174 turns re-running the same python verification because
each failure said something that had nothing to do with its cause. These checks
pin the hints that close that loop.
"""

from __future__ import annotations

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
    spec = importlib.util.spec_from_file_location("mcp_handler_registry_standalone", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    R = load_module()

    # 1. odd number of double quotes
    hint = R._shell_pitfall_hint('python -c "print(1)', "")
    check("1  unbalanced double quotes are named", "odd number of double quotes" in hint, hint[:80])

    # 2. balanced quotes: no hint
    hint = R._shell_pitfall_hint('python -c "print(1)"', "")
    check("2  balanced quotes produce no hint", hint == "", hint[:80])

    # 3. single quotes in cmd
    hint = R._shell_pitfall_hint("python -c 'print(1)'", "")
    check("3  single quotes in cmd /c are named", "single quotes are literal" in hint, hint[:80])

    # 4. a real %VAR% expansion
    hint = R._shell_pitfall_hint("echo %FOO%", "")
    check("4  %FOO% expansion is named", "expanded by cmd" in hint, hint[:80])

    # 5. a bare percent is NOT an expansion: no hint
    hint = R._shell_pitfall_hint("echo 100%", "")
    check("5  a bare percent produces no hint", hint == "", hint[:80])

    # 6. a known system variable is not flagged
    hint = R._shell_pitfall_hint("echo %PATH%", "")
    check("6  %PATH% is not flagged", hint == "", hint[:80])

    # 7. "not recognized" output names the PATH/quoting cause
    hint = R._shell_pitfall_hint(
        "pythn x.py", "'pythn' is not recognized as an internal or external command")
    check("7  not-recognized output names the cause",
          "could not find the program" in hint, hint[:80])

    # 8. zh-CN spelling is recognised too
    hint = R._shell_pitfall_hint("pythn x.py", "'pythn' 不是内部或外部命令")
    check("8  zh-CN not-recognized output names the cause",
          "could not find the program" in hint, hint[:80])

    # 9. clean command + clean output: no hint at all
    hint = R._shell_pitfall_hint("dir", "[exit code: 0]")
    check("9  a clean run produces no hint", hint == "", hint[:80])

    print()
    print(f"# summary: {len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("# FAILED: " + "; ".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
