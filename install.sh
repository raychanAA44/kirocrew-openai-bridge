#!/usr/bin/env bash
# install.sh — point KiroCrew's agent loop at any OpenAI-compatible endpoint.
#
# Mirrors install.ps1. Untested on Linux/macOS (see docs/compatibility.md);
# it is written to be conservative: it only writes hub_boot.pth + hub_boot.py
# into KiroCrew's bundled site-packages and a support folder of its own.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/kirocrew-openai-bridge"
CREW_BACKEND=""
BASE_URL=""
API_KEY=""
MODEL=""
MAX_TOKENS=8192
CONTEXT_WINDOW=128000
DRY_RUN=0
UNINSTALL=0

usage() {
  cat <<'EOF'
usage: ./install.sh [options]

  --crew-backend DIR   KiroCrew backend dir (the one containing python + Lib/site-packages)
  --base-url URL       e.g. http://127.0.0.1:28791/v1
  --api-key KEY        bearer token (prefer the OPENAI_API_KEY environment variable)
  --model NAME         e.g. oc/beam-free
  --max-tokens N       default 8192
  --context-window N   default 128000
  --install-dir DIR    default ~/.local/share/kirocrew-openai-bridge
  --dry-run            print what would change
  --uninstall          remove the hook files
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --crew-backend)   CREW_BACKEND="$2"; shift 2 ;;
    --base-url)       BASE_URL="$2"; shift 2 ;;
    --api-key)        API_KEY="$2"; shift 2 ;;
    --model)          MODEL="$2"; shift 2 ;;
    --max-tokens)     MAX_TOKENS="$2"; shift 2 ;;
    --context-window) CONTEXT_WINDOW="$2"; shift 2 ;;
    --install-dir)    INSTALL_DIR="$2"; shift 2 ;;
    --dry-run)        DRY_RUN=1; shift ;;
    --uninstall)      UNINSTALL=1; shift ;;
    -h|--help)        usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

find_backend() {
  local c
  for c in "$CREW_BACKEND" \
           "/opt/KiroCrew/resources/backend-dist/kirocrew-backend" \
           "/usr/lib/KiroCrew/resources/backend-dist/kirocrew-backend" \
           "$HOME/KiroCrew/resources/backend-dist/kirocrew-backend" \
           "/Applications/KiroCrew.app/Contents/Resources/backend-dist/kirocrew-backend"; do
    if [ -n "$c" ] && [ -x "$c/python3" ] || [ -n "$c" ] && [ -x "$c/python" ]; then
      if [ -d "$c/Lib/site-packages/kiro_crew" ] || [ -d "$c/lib/python3/site-packages/kiro_crew" ] || [ -d "$c/lib/python3.12/site-packages/kiro_crew" ]; then
        echo "$c"; return 0
      fi
    fi
  done
  return 1
}

BACKEND="$(find_backend)" || { echo "KiroCrew backend not found; pass --crew-backend" >&2; exit 1; }
PY="$BACKEND/python3"; [ -x "$PY" ] || PY="$BACKEND/python"
SITE=""
for d in "$BACKEND/Lib/site-packages" "$BACKEND"/lib/python3*/site-packages; do
  [ -d "$d/kiro_crew" ] && SITE="$d" && break
done
[ -n "$SITE" ] || { echo "site-packages not found under $BACKEND" >&2; exit 1; }

echo "== KiroCrew backend"; echo "   $BACKEND"
"$PY" -c "import importlib.metadata as m; print('   kiro_crew version:', m.version('kiro-crew'))" 2>/dev/null || true

if [ "$UNINSTALL" = "1" ]; then
  echo "== Uninstall"
  for f in "$SITE/hub_boot.pth" "$SITE/hub_boot.py"; do
    if [ -e "$f" ]; then
      if [ "$DRY_RUN" = "1" ]; then echo "   would remove $f"; else rm -f "$f"; echo "   removed $f"; fi
    else echo "   absent  $f"; fi
  done
  echo "   support folder kept: $INSTALL_DIR"
  exit 0
fi

echo "== Support folder"
if [ "$DRY_RUN" = "1" ]; then
  echo "   would populate $INSTALL_DIR"
else
  mkdir -p "$INSTALL_DIR"
  cp "$HERE/bootstrap/hook.py" "$HERE/bootstrap/provider_config.py" "$INSTALL_DIR/"
  rm -rf "$INSTALL_DIR/openai_provider"
  cp -r "$HERE/vendor/openai_provider" "$INSTALL_DIR/"
  echo "   populated $INSTALL_DIR"
fi

echo "== Configuration"
if [ -n "$BASE_URL" ] || [ -n "$MODEL" ]; then
  [ -n "$BASE_URL" ] && [ -n "$MODEL" ] || { echo "   --base-url and --model are both required" >&2; exit 1; }
  if [ "$DRY_RUN" = "1" ]; then
    echo "   would write $INSTALL_DIR/provider.json"
  else
    umask 077
    cat > "$INSTALL_DIR/provider.json" <<JSON
{
  "base_url": "$BASE_URL",
  "api_key": "$API_KEY",
  "model": "$MODEL",
  "max_tokens": $MAX_TOKENS,
  "context_window": $CONTEXT_WINDOW
}
JSON
    echo "   wrote $INSTALL_DIR/provider.json (model=$MODEL)"
  fi
elif [ -f "$INSTALL_DIR/provider.json" ]; then
  echo "   kept existing $INSTALL_DIR/provider.json"
else
  cp "$HERE/config/provider.example.json" "$INSTALL_DIR/provider.json"
  echo "   wrote provider.json from the example - edit it or re-run with --base-url/--model"
fi

echo "== Injection hook"
if [ "$DRY_RUN" = "1" ]; then
  echo "   would write $SITE/hub_boot.py"
  echo "   would write $SITE/hub_boot.pth"
else
  sed "s|@@BOOTSTRAP_DIR@@|$INSTALL_DIR|g" "$HERE/bootstrap/hub_boot.py" > "$SITE/hub_boot.py"
  printf 'import hub_boot' > "$SITE/hub_boot.pth"
  echo "   wrote $SITE/hub_boot.py and hub_boot.pth"
fi

echo "== Seam check"
if [ "$DRY_RUN" = "1" ]; then
  echo "   (skipped in dry run)"
else
  "$PY" - "$INSTALL_DIR" <<'PY'
import importlib, sys
sys.path.insert(0, sys.argv[1])
import openai_provider
mod = importlib.import_module("openai_provider.install")
from kiro_crew.providers.registry import ProviderRegistry
assert hasattr(ProviderRegistry, "create_factory"), "ProviderRegistry.create_factory missing"
assert hasattr(mod, "_build_mcp_executor"), "_build_mcp_executor missing"
print("   seams ok")
PY
fi

echo "== Next"
echo "   1) restart KiroCrew"
echo "   2) open a new chat"
echo "   3) ./doctor.sh if anything looks off"
