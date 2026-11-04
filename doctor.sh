#!/usr/bin/env bash
# doctor.sh — six-point health check (bash port of doctor.ps1).
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/kirocrew-openai-bridge"
CREW_BACKEND=""
LEDGER=""

while [ $# -gt 0 ]; do
  case "$1" in
    --ledger) LEDGER="$2"; shift 2 ;;
    --install-dir) INSTALL_DIR="$2"; shift 2 ;;
    -h|--help) echo "usage: ./doctor.sh [--crew-backend DIR] [--install-dir DIR] [--ledger PATH]"; exit 0 ;;
    *) CREW_BACKEND="$1"; shift ;;
  esac
done

step() { printf '\n[%s] %s\n' "$1" "$2"; }
ok()   { printf '    OK   %s\n' "$1"; }
bad()  { printf '    FAIL %s\n' "$1"; }
info() { printf '    ..   %s\n' "$1"; }

find_backend() {
  local c
  for c in "$CREW_BACKEND" \
           "/opt/KiroCrew/resources/backend-dist/kirocrew-backend" \
           "$HOME/KiroCrew/resources/backend-dist/kirocrew-backend" \
           "/Applications/KiroCrew.app/Contents/Resources/backend-dist/kirocrew-backend"; do
    [ -n "$c" ] && [ -d "$c" ] && echo "$c" && return 0
  done
  return 1
}

BACKEND="$(find_backend)" || { bad "KiroCrew backend not found"; exit 1; }
PY="$BACKEND/python3"; [ -x "$PY" ] || PY="$BACKEND/python"
SITE=""
for d in "$BACKEND/Lib/site-packages" "$BACKEND"/lib/python3*/site-packages; do
  [ -d "$d/kiro_crew" ] && SITE="$d" && break
done

step 1 'injection hook'
[ -f "$SITE/hub_boot.pth" ]  && ok "pth: $SITE/hub_boot.pth"  || bad "missing $SITE/hub_boot.pth (run install.sh)"
[ -f "$SITE/hub_boot.py" ]   && ok "module: $SITE/hub_boot.py" || bad "missing $SITE/hub_boot.py"
if [ -f "$INSTALL_DIR/inject.log" ]; then tail -n 3 "$INSTALL_DIR/inject.log" | while read -r l; do info "$l"; done
else bad "no inject.log at $INSTALL_DIR/inject.log"; fi

step 2 'child harness processes (expect 0)'
for name in opencode dsh goose; do
  n=$(pgrep -fc "$name" 2>/dev/null || echo 0)
  [ "$n" = "0" ] && ok "$name: 0" || info "$name: $n"
done
pgrep -af 'kiro_crew gateway' >/dev/null 2>&1 && ok 'gateway running' || bad 'no kiro_crew gateway process'

step 3 'KiroCrew seams'
"$PY" - "$INSTALL_DIR" <<'PY' 2>&1 | while read -r l; do
import importlib, sys
sys.path.insert(0, sys.argv[1])
import kiro_crew
try:
    import importlib.metadata as md
    print("version", md.version("kiro-crew"))
except Exception:
    print("version ?")
from kiro_crew.providers.registry import ProviderRegistry
import openai_provider
mod = importlib.import_module("openai_provider.install")
for name, value in (
    ("ProviderRegistry.create_factory", hasattr(ProviderRegistry, "create_factory")),
    ("install._build_mcp_executor", hasattr(mod, "_build_mcp_executor")),
):
    print(("ok " if value else "MISSING ") + name)
PY
  case "$l" in
    MISSING*) bad "$l" ;;
    ok*)      ok "$l" ;;
    *)        info "$l" ;;
  esac
done

step 4 'configured endpoint'
"$PY" - "$INSTALL_DIR" <<'PY' 2>&1 | while read -r l; do info "$l"; done
import json, sys
sys.path.insert(0, sys.argv[1])
import provider_config
c = provider_config.resolve()
print("base_url =", c["base_url"], "[" + c["sources"].get("base_url", "?") + "]")
print("model    =", c["model"], "[" + c["sources"].get("model", "?") + "]")
print("api key  =", "set" if c["api_key"] else "MISSING")
PY

step 5 'tool catalogue'
n=$("$PY" - "$INSTALL_DIR" <<'PY' 2>/dev/null
import sys
sys.path.insert(0, sys.argv[1])
from openai_provider.mcp_executor import McpToolExecutor
print(len(McpToolExecutor().tool_definitions()))
PY
)
if [ "${n:-0}" -gt 0 ] 2>/dev/null; then ok "tool definitions: $n"; else bad "tool definitions: ${n:-?} (expected > 0)"; fi

step 6 'ledger (requested vs served)'
ledger="${LEDGER:-}"
if [ -z "$ledger" ]; then
  ledger=$(find "$HOME" /d /opt -maxdepth 6 -name ledger.db -path '*hub*' 2>/dev/null | head -n 1)
fi
if [ -n "${ledger:-}" ]; then
  info "ledger: $ledger"
  "$PY" - "$ledger" <<'PY' 2>&1 | while read -r l; do info "$l"; done
import sqlite3, sys
c = sqlite3.connect(sys.argv[1]); c.row_factory = sqlite3.Row
for r in c.execute("select at, model, requested_model from usage order by id desc limit 5"):
    print(round(r["at"]), "served:", r["model"], "| requested:", r["requested_model"])
PY
else info 'no hub ledger found - skip if you do not use a hub gateway'; fi

printf '\nDone. Paste this output when reporting an issue.\n'
