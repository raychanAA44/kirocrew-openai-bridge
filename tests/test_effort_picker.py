"""The effort-picker bundle patch: reissue the import closure, keep pristine files.

The "bundle" under test is a fixture tree, never the installed crew build: these
tests must pass on a machine with no KiroCrew at all.
"""

import importlib.util
import pathlib
import sys

BRIDGE = pathlib.Path(__file__).resolve().parents[1]


def load_module():
    path = BRIDGE / "bootstrap" / "effort_picker.py"
    spec = importlib.util.spec_from_file_location("bridge_effort_picker", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def read(path):
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def make_tree(tmp_path, module, *, predicate=None):
    """``index.html`` -> ``main-X`` -> ``SettingsPage-Y`` -> ``effort-Z``, plus a loner."""
    assets = tmp_path / "dist" / "assets"
    assets.mkdir(parents=True)
    write(assets / "effort-Z.js", 'import"./t.js";' + (predicate or module.OLD) + "export{};")
    write(assets / "SettingsPage-Y.js", 'import"./effort-Z.js";export const s=1;')
    write(assets / "main-X.js", 'import"./SettingsPage-Y.js";export const m=1;')
    write(assets / "Loner-W.js", "export const w=1;")
    write(
        tmp_path / "dist" / "index.html",
        '<script type="module" src="/assets/main-X.js"></script>',
    )
    return str(assets)


def test_state_reads_pristine(monkeypatch, tmp_path):
    module = load_module()
    monkeypatch.setattr(module, "_BACKUP_DIR", str(tmp_path / "backups"))
    assets = make_tree(tmp_path, module)
    state, _detail, path = module.state(assets)
    assert state == "pristine"
    assert path.endswith("effort-Z.js")


def test_patch_reissues_only_the_import_closure(monkeypatch, tmp_path):
    module = load_module()
    monkeypatch.setattr(module, "_BACKUP_DIR", str(tmp_path / "backups"))
    assets = make_tree(tmp_path, module)
    dist = tmp_path / "dist"

    action, before, after = module.apply(assets)
    assert action == "patched"
    assert before != after
    assert module.state(assets)[0] == "patched"

    # The chain from the shell down is reissued; the unrelated chunk is not.
    for name in ("effort-Z", "SettingsPage-Y", "main-X"):
        assert (dist / "assets" / f"{name}.hub.js").is_file(), name
    assert not (dist / "assets" / "Loner-W.hub.js").exists()

    # Pristine files are untouched, which is what makes --revert a delete.
    assert module.OLD in read(dist / "assets" / "effort-Z.js")
    assert module.NEW in read(dist / "assets" / "effort-Z.hub.js")

    # The reissued copies reference each other, never the pristine names.
    main = read(dist / "assets" / "main-X.hub.js")
    assert "./SettingsPage-Y.hub.js" in main
    settings = read(dist / "assets" / "SettingsPage-Y.hub.js")
    assert "./effort-Z.hub.js" in settings

    # The shell (served no-store) is the entry point a reload re-reads.
    assert "main-X.hub.js" in read(dist / "index.html")


def test_apply_is_idempotent(monkeypatch, tmp_path):
    module = load_module()
    monkeypatch.setattr(module, "_BACKUP_DIR", str(tmp_path / "backups"))
    assets = make_tree(tmp_path, module)

    _action, _before, after = module.apply(assets)
    action2, before2, after2 = module.apply(assets)
    assert action2 == "already-patched"
    assert before2 == after2 == after


def test_revert_drops_the_reissued_closure(monkeypatch, tmp_path):
    module = load_module()
    monkeypatch.setattr(module, "_BACKUP_DIR", str(tmp_path / "backups"))
    assets = make_tree(tmp_path, module)
    dist = tmp_path / "dist"
    shell_before = read(dist / "index.html")
    effort_before = read(dist / "assets" / "effort-Z.js")

    module.apply(assets)
    action, _before, _after = module.apply(assets, revert=True)
    assert action == "reverted"
    assert module.state(assets)[0] == "pristine"

    assert not list(dist.rglob("*.hub.js"))
    assert read(dist / "index.html") == shell_before
    assert read(dist / "assets" / "effort-Z.js") == effort_before


def test_unknown_bundle_refuses_instead_of_guessing(monkeypatch, tmp_path):
    module = load_module()
    monkeypatch.setattr(module, "_BACKUP_DIR", str(tmp_path / "backups"))
    assets = make_tree(tmp_path, module, predicate='var x=1;console.log("rewritten");')

    assert module.state(assets)[0] == "unknown"
    try:
        module.apply(assets)
    except FileNotFoundError as exc:
        assert "unknown state" in str(exc) or "predicate" in str(exc)
    else:  # pragma: no cover - the assertion above is the contract
        raise AssertionError("apply() patched a bundle it did not recognise")
    assert not list((tmp_path / "dist").rglob("*.hub.js"))


def test_two_effort_chunks_are_refused(monkeypatch, tmp_path):
    module = load_module()
    monkeypatch.setattr(module, "_BACKUP_DIR", str(tmp_path / "backups"))
    assets = make_tree(tmp_path, module)
    write(pathlib.Path(assets) / "effort-SECOND.js", "var y=2;")

    try:
        module.state(assets)
    except FileNotFoundError as exc:
        assert "exactly one" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("state() picked one of two effort chunks")


def test_an_orphan_effort_chunk_is_refused(monkeypatch, tmp_path):
    """Nothing imports it — the patch would be invisible, so refuse."""
    module = load_module()
    monkeypatch.setattr(module, "_BACKUP_DIR", str(tmp_path / "backups"))
    assets = tmp_path / "dist" / "assets"
    assets.mkdir(parents=True)
    write(assets / "effort-Z.js", module.OLD)
    write(assets / "main-X.js", "export const m=1;")
    write(tmp_path / "dist" / "index.html", "<html></html>")

    try:
        module.apply(str(assets))
    except FileNotFoundError as exc:
        assert "nothing imports" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("apply() patched an unreachable chunk")
