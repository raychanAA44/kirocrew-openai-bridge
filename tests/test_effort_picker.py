"""The effort-picker bundle patch: idempotent, reversible, loud on drift.

The bundle under test is a fixture, never the installed crew build: these tests
must pass on a machine with no KiroCrew at all.
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


def make_assets(tmp_path, body):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "effort-TESTID.js").write_text(body, encoding="utf-8", newline="")
    return str(assets)


def pristine_body(module):
    return (
        'import{t as e}from"./t.js";var i=[`low`];'
        + module.OLD
        + "export{i as t};"
    )


def test_state_reads_pristine(monkeypatch, tmp_path):
    module = load_module()
    monkeypatch.setattr(module, "_BACKUP_DIR", str(tmp_path / "backups"))
    assets = make_assets(tmp_path, pristine_body(module))
    state, _sha, path = module.state(assets)
    assert state == "pristine"
    assert path.endswith("effort-TESTID.js")


def test_apply_is_idempotent(monkeypatch, tmp_path):
    module = load_module()
    monkeypatch.setattr(module, "_BACKUP_DIR", str(tmp_path / "backups"))
    assets = make_assets(tmp_path, pristine_body(module))

    action, before, after = module.apply(assets)
    assert action == "patched"
    assert before != after
    assert module.state(assets)[0] == "patched"

    action2, before2, after2 = module.apply(assets)
    assert action2 == "already-patched"
    assert before2 == after2 == after


def test_revert_restores_the_original_bytes(monkeypatch, tmp_path):
    module = load_module()
    monkeypatch.setattr(module, "_BACKUP_DIR", str(tmp_path / "backups"))
    body = pristine_body(module)
    assets = make_assets(tmp_path, body)

    module.apply(assets)
    action, _before, after = module.apply(assets, revert=True)
    assert action == "reverted"
    assert module.state(assets)[0] == "pristine"
    assert (pathlib.Path(assets) / "effort-TESTID.js").open(
        encoding="utf-8", newline=""
    ).read() == body


def test_unknown_bundle_refuses_instead_of_guessing(monkeypatch, tmp_path):
    module = load_module()
    monkeypatch.setattr(module, "_BACKUP_DIR", str(tmp_path / "backups"))
    assets = make_assets(tmp_path, 'var x=1;console.log("rewritten by an upgrade");')

    assert module.state(assets)[0] == "unknown"
    try:
        module.apply(assets)
    except FileNotFoundError as exc:
        assert "unknown predicate" in str(exc)
    else:  # pragma: no cover - the assertion above is the contract
        raise AssertionError("apply() patched a bundle it did not recognise")
    assert module.state(assets)[0] == "unknown"


def test_two_effort_chunks_are_refused(monkeypatch, tmp_path):
    module = load_module()
    monkeypatch.setattr(module, "_BACKUP_DIR", str(tmp_path / "backups"))
    assets = make_assets(tmp_path, pristine_body(module))
    (pathlib.Path(assets) / "effort-SECOND.js").write_text("var y=2;", encoding="utf-8")

    try:
        module.state(assets)
    except FileNotFoundError as exc:
        assert "exactly one" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("state() picked one of two effort chunks")


def test_backup_keeps_the_pristine_bytes(monkeypatch, tmp_path):
    module = load_module()
    backups = tmp_path / "backups"
    monkeypatch.setattr(module, "_BACKUP_DIR", str(backups))
    body = pristine_body(module)
    assets = make_assets(tmp_path, body)

    module.apply(assets)
    stored = (backups / "effort-TESTID.js.orig").open(
        encoding="utf-8", newline=""
    ).read()
    assert stored == body
    assert module.NEW not in stored
