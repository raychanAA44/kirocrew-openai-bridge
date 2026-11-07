"""effort_picker.py — let KiroCrew's effort pickers render for every reasoning model.

KiroCrew decides whether to ENABLE a settings-page reasoning-effort dropdown from
a MODEL-NAME heuristic shipped inside its built frontend bundle:

    function a(e){if(!e)return!1;let t=e.toLowerCase();
      return t==="auto"||t.includes("haiku") ? !1
           : t.includes("opus")||t.includes("sonnet")||t.includes("fable")||t.includes("gpt")}

Only Anthropic-ish and ``gpt`` names pass, so a route like ``od/deepseek-v4.1-flash``
or ``oc/beam-free`` — both of which DO advertise reasoning variants upstream — gets
its dropdown disabled. The chat composer is unaffected: it reads the server-side
``selection-capabilities`` answer, so this heuristic only gates the settings page.

This rewrites the predicate to "anything but auto / haiku", which is the same rule
without the vendor allowlist. Idempotent; fails loudly (and changes nothing) when a
crew upgrade reshapes the bundle.

    python bootstrap/effort_picker.py            # apply
    python bootstrap/effort_picker.py --check    # exit 1 when unpatched
    python bootstrap/effort_picker.py --revert   # restore the pristine predicate
"""

from __future__ import annotations

import glob
import hashlib
import os
import shutil
import sys

OLD = (
    "function a(e){if(!e)return!1;let t=e.toLowerCase();return t===`auto`||"
    "t.includes(`haiku`)?!1:t.includes(`opus`)||t.includes(`sonnet`)||"
    "t.includes(`fable`)||t.includes(`gpt`)}"
)
NEW = (
    "function a(e){if(!e)return!1;let t=e.toLowerCase();"
    "return t!==`auto`&&!t.includes(`haiku`)}"
)

_BACKUP_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
    "kirocrew-openai-bridge", "backups",
)


def assets_dir() -> str:
    """The installed crew build's ``static/dist/assets`` directory."""
    roots = [
        os.environ.get("KIROCREW_ASSETS_DIR") or "",
        os.path.join(
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            "KiroCrew", "resources", "backend-dist", "kirocrew-backend",
            "Lib", "site-packages", "kiro_crew", "static", "dist", "assets",
        ),
        os.path.join(
            os.environ.get("LOCALAPPDATA", ""), "Programs", "KiroCrew",
            "resources", "backend-dist", "kirocrew-backend",
            "Lib", "site-packages", "kiro_crew", "static", "dist", "assets",
        ),
    ]
    for path in roots:
        if path and os.path.isdir(path):
            return path
    raise FileNotFoundError("KiroCrew static/dist/assets not found")


def bundle_path(assets: str | None = None) -> str:
    """The one ``effort-*.js`` chunk the predicate lives in."""
    directory = assets or assets_dir()
    hits = sorted(glob.glob(os.path.join(directory, "effort-*.js")))
    if len(hits) != 1:
        raise FileNotFoundError(
            f"expected exactly one effort-*.js in {directory}, found {len(hits)}"
        )
    return hits[0]


def _read(path: str) -> str:
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16].upper()


def state(assets: str | None = None) -> tuple[str, str, str]:
    """``(state, sha16, path)`` where state is patched / pristine / unknown."""
    path = bundle_path(assets)
    text = _read(path)
    if NEW in text:
        return "patched", _sha(text), path
    if OLD in text:
        return "pristine", _sha(text), path
    return "unknown", _sha(text), path


def apply(assets: str | None = None, *, revert: bool = False) -> tuple[str, str, str]:
    """Patch (or revert) the predicate. ``(action, before_sha, after_sha)``.

    ``action`` is ``patched`` / ``reverted`` / ``already-patched`` /
    ``already-pristine``. Raises FileNotFoundError when the bundle holds neither
    the known predicate nor its replacement — an upgrade reshaped it and the
    replacement must be re-derived by hand, not guessed.
    """
    current, before, path = state(assets)
    wanted = "pristine" if revert else "patched"
    already = "already-pristine" if revert else "already-patched"
    if current == wanted:
        return already, before, before
    if current != ("patched" if revert else "pristine"):
        raise FileNotFoundError(f"{path} holds an unknown predicate")

    text = _read(path)
    source, target = (NEW, OLD) if revert else (OLD, NEW)

    backup = os.path.join(_BACKUP_DIR, os.path.basename(path) + ".orig")
    os.makedirs(_BACKUP_DIR, exist_ok=True)
    # The pristine copy is a rollback artefact: refresh it only while the file
    # still holds the original bytes, never with an already-patched body.
    if not revert or not os.path.exists(backup):
        shutil.copy2(path, backup)

    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text.replace(source, target))

    return ("reverted" if revert else "patched"), before, _sha(_read(path))


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    check = "--check" in args
    revert = "--revert" in args
    assets = None
    for i, arg in enumerate(args):
        if arg == "--assets" and i + 1 < len(args):
            assets = args[i + 1]

    try:
        if check:
            current, sha, path = state(assets)
            print(f"{current}: {os.path.basename(path)} sha={sha}")
            return 0 if current == "patched" else 1
        action, before, after = apply(assets, revert=revert)
    except FileNotFoundError as exc:
        print(f"error: {exc}")
        return 2

    if action.startswith("already-"):
        print(f"{action}: sha={after}")
        return 0
    print(f"{action}: sha={before} -> {after} (pristine copy in {_BACKUP_DIR})")
    print("reload the settings page; a cached bundle needs Ctrl+F5")
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI
    sys.exit(main())
