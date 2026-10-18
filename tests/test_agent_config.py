import json
import sys
import traceback
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agent import config, stintara_agent


def write_config(tmp_path, **updates):
    data = {"server_ws": "wss://stintara.example.invalid/ws/ingest", "token": "test-only-token"}
    data.update(updates)
    path = tmp_path / "stintara-agent.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_json_defaults_and_immutable_config(tmp_path):
    result = config.load_config(write_config(tmp_path), {})
    assert result.server_ws == "wss://stintara.example.invalid/ws/ingest"
    assert result.pitwall_ws == config.DEFAULT_PITWALL_WS
    assert result.token == "test-only-token"
    assert result.token not in repr(result)
    with pytest.raises(FrozenInstanceError):
        result.token = "changed"


def test_environment_overrides_json_and_can_work_without_default_file(tmp_path, monkeypatch):
    env = {"STINTARA_WS": "ws://localhost:8080/ws/ingest", "PITWALL_WS": "ws://localhost:9000",
           "STINTARA_AGENT_TOKEN": " test-only-env "}
    result = config.load_config(write_config(tmp_path), env)
    assert result == config.AgentConfig(env["STINTARA_WS"], "test-only-env", env["PITWALL_WS"])
    monkeypatch.setattr(config, "get_default_config_path", lambda: tmp_path / "missing.json")
    assert config.load_config(environ=env) == result
    with pytest.raises(ValueError, match="Explicit config"):
        config.load_config(tmp_path / "missing.json", env)


@pytest.mark.parametrize("updates", [
    {"token": None}, {"token": ""}, {"token": " \t"}, {"token": "change-test-only"},
    {"token": "replace-test-only"}, {"token": 42}, {"server_ws": None}, {"server_ws": ""},
    {"server_ws": "https://stintara.example.invalid"}, {"server_ws": "ws://"},
    {"server_ws": "ws://localhost:invalid"}, {"pitwall_ws": ""}, {"pitwall_ws": "file:invalid"},
])
def test_invalid_values_are_rejected_without_secret_disclosure(tmp_path, updates):
    with pytest.raises(ValueError) as exc:
        config.load_config(write_config(tmp_path, **updates), {})
    assert "test-only-token" not in "".join(traceback.format_exception(exc.value))


@pytest.mark.parametrize("contents", ['{"token":"test-only-private",', '[]', 'null'])
def test_bad_json_is_safe(tmp_path, contents):
    path = tmp_path / "bad.json"
    path.write_text(contents)
    with pytest.raises(ValueError) as exc:
        config.load_config(path, {})
    assert "test-only-private" not in "".join(traceback.format_exception(exc.value))


def test_unreadable_config_and_empty_environment_do_not_fall_back(tmp_path):
    with pytest.raises(ValueError):
        config.load_config(tmp_path, {})
    with pytest.raises(ValueError):
        config.load_config(write_config(tmp_path), {"STINTARA_AGENT_TOKEN": ""})


def test_default_path_uses_executable_not_extraction_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "StintaraAgent.exe"))
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "extracted"), raising=False)
    assert config.get_default_config_path() == tmp_path / "stintara-agent.json"
    monkeypatch.setattr(sys, "frozen", False)
    assert config.get_default_config_path() == Path(config.__file__).resolve().with_name("stintara-agent.json")


def test_cli_explicit_config_and_help(tmp_path, monkeypatch):
    for name in ("STINTARA_WS", "PITWALL_WS", "STINTARA_AGENT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    run = AsyncMock()
    monkeypatch.setattr(stintara_agent, "main", run)
    stintara_agent.cli(["--config", str(write_config(tmp_path))])
    assert run.await_args.args[0].token == "test-only-token"
    with pytest.raises(SystemExit) as exc:
        stintara_agent.cli(["--help"])
    assert exc.value.code == 0
    assert run.await_count == 1


def test_cli_error_does_not_echo_arguments(capsys):
    with pytest.raises(SystemExit):
        stintara_agent.cli(["--test-only-private"])
    assert "test-only-private" not in capsys.readouterr().err


@pytest.mark.parametrize("missing", ["token", "server_ws"])
def test_missing_required_keys(tmp_path, missing):
    path = write_config(tmp_path)
    data = json.loads(path.read_text())
    del data[missing]
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        config.load_config(path, {})
