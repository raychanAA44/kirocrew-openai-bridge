"""The hook must be inert outside the gateway command, and must fix no_proxy.

These run without KiroCrew installed: with ``HUB_BRIDGE=0`` the hook stops right
after sanitising the environment, before it ever imports ``openai_provider``.
"""

import importlib.util
import os
import pathlib
import sys

import pytest

BRIDGE = pathlib.Path(__file__).resolve().parents[1]


def load_hook():
    path = BRIDGE / "bootstrap" / "hook.py"
    spec = importlib.util.spec_from_file_location("bridge_hook", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def clean_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HUB_BRIDGE", "0")
    monkeypatch.setattr(sys, "path", list(sys.path), raising=False)
    monkeypatch.setattr(os, "path", os.path)
    monkeypatch.setitem(sys.modules, "__bridge_tmp__", type(sys)("__bridge_tmp__"))
    return monkeypatch


def test_no_proxy_drops_bare_ipv6(clean_env):
    clean_env.setenv("no_proxy", "localhost,127.0.0.1,::1,[::1]")
    load_hook()
    value = os.environ["no_proxy"]
    assert "[::1]" not in value
    assert "localhost" in value and "127.0.0.1" in value


def test_no_proxy_defaults_when_empty(clean_env):
    clean_env.setenv("no_proxy", "[::1]")
    load_hook()
    assert os.environ["no_proxy"] == "localhost,127.0.0.1"


def test_hook_does_not_import_the_provider_when_disabled(clean_env):
    load_hook()
    assert "openai_provider" not in sys.modules


def test_log_line_is_written(clean_env, tmp_path, monkeypatch):
    module = load_hook()
    monkeypatch.setattr(module, "_LOG", str(tmp_path / "inject.log"), raising=False)
    module._log("hello")
    assert (tmp_path / "inject.log").read_text(encoding="utf-8").strip().endswith("hello")


def _sessions(module, monkeypatch, tmp_path):
    """Point ``_session_model`` at a throwaway ``~/.kiro/crew/sessions``."""
    home = tmp_path / "home"
    (home / ".kiro" / "crew" / "sessions").mkdir(parents=True)
    monkeypatch.setattr(module.os.path, "expanduser", lambda p: str(home))
    return home / ".kiro" / "crew" / "sessions"


def test_session_model_reads_the_colon_flattened_file(clean_env, tmp_path, monkeypatch):
    """The slot key carries ``dashboard:``; the file carries ``dashboard_``."""
    module = load_hook()
    sessions = _sessions(module, monkeypatch, tmp_path)
    (sessions / "dashboard_chat-11-1793965845.jsonl").write_text(
        '{"_type": "metadata", "model": "youzi/gpt-6.1-sol"}\n', encoding="utf-8"
    )
    assert module._session_model("dashboard:chat-11-1793965845") == "youzi/gpt-6.1-sol"


def test_session_model_refuses_an_ambiguous_prefix(clean_env, tmp_path, monkeypatch):
    """A shared prefix must not hand one session another session's pick."""
    module = load_hook()
    sessions = _sessions(module, monkeypatch, tmp_path)
    for name in ("dashboard_lead.jsonl", "x_dashboard_lead.jsonl"):
        (sessions / name).write_text('{"model": "oc/beam-free"}\n', encoding="utf-8")
    assert module._session_model("dashboard:lead") == ""


def test_session_model_missing_file_is_empty(clean_env, tmp_path, monkeypatch):
    module = load_hook()
    _sessions(module, monkeypatch, tmp_path)
    assert module._session_model("dashboard:chat-99-1") == ""

