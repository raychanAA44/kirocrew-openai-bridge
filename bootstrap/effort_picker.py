"""effort_picker.py — let KiroCrew's effort pickers render for every reasoning model.

Two gates keep the settings-page reasoning-effort dropdowns disabled here:

1. A MODEL-NAME heuristic compiled into the frontend bundle —

       function a(e){...return t==="auto"||t.includes("haiku") ? !1
         : t.includes("opus")||t.includes("sonnet")||t.includes("fable")||t.includes("gpt")}

   Only Anthropic-ish and ``gpt`` names pass, so ``od/deepseek-v4.1-flash`` and
   ``oc/beam-free`` — both of which DO advertise reasoning variants upstream —
   render with their dropdown disabled.

2. ``/assets/`` is served ``Cache-Control: public, max-age=31536000, immutable``
   because Vite content-hashes those filenames. Editing one IN PLACE therefore
   changes nothing any browser will see again: it already holds the old bytes
   under that exact URL. An earlier version of this script did exactly that and
   silently did nothing.

So the fix is a URL change, not a content change. The import closure of the effort
chunk — every chunk that transitively imports it, down from the shell — is reissued
as ``<name>.hub.js``, and references are repointed inside the reissued copies and in
``index.html`` (served ``no-store``, so it is the one file a reload re-reads).
Pristine files are never modified, which is what makes ``--revert`` a delete.

Idempotent and reversible; it refuses a bundle whose predicate it does not
recognise instead of guessing (a crew upgrade reshapes the chunk).

    python bootstrap/effort_picker.py            # apply
    python bootstrap/effort_picker.py --check    # exit 1 when unpatched
    python bootstrap/effort_picker.py --revert   # drop the reissued closure
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

#: Inserted before ``.js`` to mint a URL no browser has cached. The token itself is
#: arbitrary; this one says why the copy exists.
MARK = ".hub"

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


def _dist(assets: str) -> str:
    return os.path.dirname(assets.rstrip("/\\"))


def _marked(name: str) -> str:
    """``app-ABC.js`` -> ``app-ABC.hub.js``; already-marked names pass through."""
    if name.endswith(MARK + ".js"):
        return name
    return name[: -len(".js")] + MARK + ".js"


def _read(path: str) -> str:
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16].upper()


def effort_chunk(assets: str) -> str:
    """The pristine ``effort-*.js`` the predicate lives in."""
    hits = [
        h for h in sorted(glob.glob(os.path.join(assets, "effort-*.js")))
        if not h.endswith(MARK + ".js")
    ]
    if len(hits) != 1:
        raise FileNotFoundError(
            f"expected exactly one pristine effort-*.js in {assets}, found {len(hits)}"
        )
    return hits[0]


def _text_files(dist: str) -> list[str]:
    out: list[str] = []
    for root, _dirs, files in os.walk(dist):
        for name in files:
            if name.endswith((".js", ".html")):
                out.append(os.path.join(root, name))
    return out


def closure(dist: str, stem: str) -> list[str]:
    """Pristine chunks that transitively name *stem*, plus *stem* and the shell.

    A chunk outside this set cannot import anything inside it — otherwise it would
    be inside it — so reissuing just this set leaves no stale reference behind.
    The chunk holding the predicate does not contain its own filename, so it is
    seeded explicitly.
    """
    shell = os.path.join(dist, "index.html")
    files = [p for p in _text_files(dist) if not p.endswith(MARK + ".js")]
    bodies = {p: _read(p) for p in files}
    chunk = next(
        (p for p in files if os.path.basename(p)[: -len(".js")] == stem), None
    )
    if chunk is None:
        raise FileNotFoundError(f"{stem}.js is not under {dist}")
    importers = {p for p in files if p not in (shell, chunk) and stem in bodies[p]}
    if not importers:
        raise FileNotFoundError(f"nothing imports {stem}; refusing to patch blind")
    # A chunk does not name itself, so the seed is explicit.
    need = importers | {chunk}
    changed = True
    while changed:
        changed = False
        for path in files:
            if path in need or path == shell:
                continue
            body = bodies[path]
            for dep in need:
                if os.path.basename(dep) in body:
                    need.add(path)
                    changed = True
                    break
    # The shell typically reaches the chunk through intermediate pages, not
    # directly, so it joins whenever it names any member of the closure.
    if any(os.path.basename(dep) in bodies.get(shell, "") for dep in need):
        need.add(shell)
    return sorted(need)


def state(assets: str | None = None) -> tuple[str, str, str]:
    """``(state, detail, path)`` where state is patched / pristine / unknown."""
    directory = assets or assets_dir()
    chunk = effort_chunk(directory)
    marked = os.path.join(directory, _marked(os.path.basename(chunk)))

    if os.path.exists(marked):
        body = _read(marked)
        if NEW in body:
            return "patched", _sha(body), marked
        return "unknown", "the reissued chunk does not hold the patch", marked

    body = _read(chunk)
    if OLD in body:
        return "pristine", _sha(body), chunk
    if NEW in body:
        return (
            "unknown",
            "patched in place under an immutable URL — no browser can see it",
            chunk,
        )
    return "unknown", "the known predicate is absent", chunk


def _restore_in_place(chunk: str) -> None:
    """Undo an old-style in-place patch, if the pristine copy is still on disk."""
    backup = os.path.join(_BACKUP_DIR, os.path.basename(chunk) + ".orig")
    if not os.path.exists(backup):
        raise FileNotFoundError(
            f"{chunk} was patched in place and no pristine copy exists at {backup}"
        )
    shutil.copy2(backup, chunk)


def apply(assets: str | None = None, *, revert: bool = False) -> tuple[str, str, str]:
    """Patch (or revert). ``(action, before, after)``.

    ``action`` is ``patched`` / ``reverted`` / ``already-patched`` /
    ``already-pristine``. Raises FileNotFoundError when the predicate is
    unrecognisable, or when an in-place patch has no pristine copy to heal from.
    """
    directory = assets or assets_dir()
    dist = _dist(directory)
    shell = os.path.join(dist, "index.html")
    chunk = effort_chunk(directory)
    marked = os.path.join(directory, _marked(os.path.basename(chunk)))

    os.makedirs(_BACKUP_DIR, exist_ok=True)
    shell_backup = os.path.join(_BACKUP_DIR, "index.html.orig")

    if revert:
        if not os.path.exists(shell_backup):
            raise FileNotFoundError(f"no shell backup at {shell_backup}")
        removed = 0
        for path in _text_files(dist):
            if path.endswith(MARK + ".js"):
                os.remove(path)
                removed += 1
        shutil.copy2(shell_backup, shell)
        return "reverted", f"{removed} reissued chunk(s) removed", _sha(_read(chunk))

    current, detail, _path = state(directory)
    if current == "patched":
        return "already-patched", detail, detail
    if current == "unknown" and "patched in place" in detail:
        _restore_in_place(chunk)
        current, detail, _path = state(directory)
    if current != "pristine":
        raise FileNotFoundError(f"effort chunk holds an unknown state: {detail}")

    if not os.path.exists(shell_backup) and os.path.exists(shell):
        shutil.copy2(shell, shell_backup)

    stem = os.path.basename(chunk)[: -len(".js")]
    group = closure(dist, stem)
    if not group:
        raise FileNotFoundError(
            f"nothing imports {stem}; refusing to patch blind"
        )
    link = {
        os.path.basename(p): _marked(os.path.basename(p))
        for p in group if p != shell
    }

    # 1. Reissue the closure verbatim, so the rename map is complete before any
    #    reference is rewritten.
    for path in group:
        if path == shell:
            continue
        shutil.copy2(path, os.path.join(os.path.dirname(path), link[os.path.basename(path)]))

    # 2. The predicate itself, in the reissued effort chunk.
    body = _read(marked)
    if OLD not in body:
        raise FileNotFoundError(f"{marked} lost the predicate during reissue")
    _write(marked, body.replace(OLD, NEW))

    # 3. Repoint references inside the reissued copies only — pristine files stay
    #    byte-identical, which is what makes --revert a delete. Longest names
    #    first so a name that is a prefix of another cannot corrupt it.
    order = sorted(link, key=len, reverse=True)
    for name, new in link.items():
        path = os.path.join(directory, new)
        text = _read(path)
        for old in order:
            if old in text:
                text = text.replace(old, link[old])
        _write(path, text)

    # 4. The shell is served no-store, so it is the one file a reload re-reads.
    if shell in group:
        text = _read(shell)
        for old in order:
            text = text.replace(old, link[old])
        _write(shell, text)

    return "patched", _sha(_read(chunk)), _sha(_read(marked))


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
            current, detail, path = state(assets)
            print(f"{current}: {os.path.basename(path)} {detail}")
            return 0 if current == "patched" else 1
        action, before, after = apply(assets, revert=revert)
    except FileNotFoundError as exc:
        print(f"error: {exc}")
        return 2

    if action.startswith("already-"):
        print(f"{action}: {after}")
        return 0
    print(f"{action}: {before} -> {after} (pristine copies in {_BACKUP_DIR})")
    print("reload the page; the chunk URLs changed, so no hard refresh is needed")
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI
    sys.exit(main())
