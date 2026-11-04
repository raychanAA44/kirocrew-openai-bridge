"""Config resolution: precedence, read-through, and model-id normalisation."""

import importlib.util
import json
import pathlib
import sys

import pytest

BRIDGE = pathlib.Path(__file__).resolve().parents[1]


def load_module(name: str):
    path = BRIDGE / "bootstrap" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"bridge_{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def cfg(monkeypatch, tmp_path):
    """A provider_config module wired to temp files instead of the real home."""
    monkeypatch.setenv("HUB_BRIDGE_TEST_HOME", str(tmp_path))
    module = load_module("provider_config")
    monkeypatch.setattr(module, "PROVIDER_JSON", str(tmp_path / "provider.json"), raising=False)
    monkeypatch.setattr(module, "OPENCODE_JSON", str(tmp_path / "opencode.json"), raising=False)
    monkeypatch.setattr(module, "CREW_JSON", str(tmp_path / "crew.json"), raising=False)
    for var in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL",
                "OPENAI_MAX_TOKENS", "OPENAI_CONTEXT_WINDOW"):
        monkeypatch.delenv(var, raising=False)
    return module


def test_provider_json_is_the_fallback(cfg, tmp_path):
    (tmp_path / "provider.json").write_text(json.dumps({
        "base_url": "http://gw.local/v1", "api_key": "k", "model": "m", "max_tokens": 128,
    }), encoding="utf-8")
    resolved = cfg.resolve()
    assert resolved["base_url"] == "http://gw.local/v1"
    assert resolved["model"] == "m"
    assert resolved["max_tokens"] == 128
    assert resolved["sources"]["model"] == "provider.json"


def test_environment_beats_files(cfg, tmp_path, monkeypatch):
    (tmp_path / "provider.json").write_text(json.dumps({"model": "from-file"}), encoding="utf-8")
    monkeypatch.setenv("OPENAI_MODEL", "from-env")
    assert cfg.resolve()["model"] == "from-env"
    assert cfg.resolve()["sources"]["model"] == "env"


def test_reads_through_to_kirocrew_managed_ids(cfg, tmp_path):
    """An existing KiroCrew control panel keeps working without extra config."""
    (tmp_path / "opencode.json").write_text(json.dumps({
        "provider": {"hub-neo": {"options": {"baseURL": "http://127.0.0.1:8888/v1/", "apiKey": "sk-x"}}}
    }), encoding="utf-8")
    (tmp_path / "crew.json").write_text(json.dumps({
        "agent": {"model": "hub-neo/ollama/llama3.3:70b"}
    }), encoding="utf-8")

    resolved = cfg.resolve()
    assert resolved["base_url"] == "http://127.0.0.1:8888/v1"   # trailing slash trimmed
    assert resolved["api_key"] == "sk-x"
    assert resolved["model"] == "ollama/llama3.3:70b"           # provider prefix stripped
    assert resolved["sources"]["model"] == "crew config.json"


def test_as_env_round_trips(cfg, tmp_path):
    (tmp_path / "provider.json").write_text(json.dumps({
        "base_url": "http://gw/v1", "api_key": "k", "model": "m",
    }), encoding="utf-8")
    env = cfg.as_env()
    assert env["OPENAI_BASE_URL"] == "http://gw/v1"
    assert env["OPENAI_MODEL"] == "m"
    assert env["OPENAI_API_KEY"] == "k"


def test_defaults_when_nothing_is_configured(cfg):
    resolved = cfg.resolve()
    assert resolved["base_url"].startswith("http://")
    assert resolved["model"]
