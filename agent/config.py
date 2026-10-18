"""Startup configuration; never include configuration values in diagnostics."""
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_PITWALL_WS = "ws://127.0.0.1:9000?format=json"


@dataclass(frozen=True)
class AgentConfig:
    server_ws: str = field(repr=False)
    token: str = field(repr=False)
    pitwall_ws: str = field(default=DEFAULT_PITWALL_WS, repr=False)


def get_default_config_path() -> Path:
    program = Path(sys.executable) if getattr(sys, "frozen", False) else Path(__file__)
    return program.resolve().parent / "stintara-agent.json"


def validate_ws_url(value, name):
    try:
        if not isinstance(value, str) or not value or any(c.isspace() for c in value):
            raise ValueError
        url = urlsplit(value)
        if url.scheme not in ("ws", "wss") or not url.hostname or url.fragment or url.username or url.password:
            raise ValueError
        if url.port is not None and not 1 <= url.port <= 65535:
            raise ValueError
    except ValueError:
        raise ValueError(f"Configure {name} with a valid ws:// or wss:// URL without embedded credentials") from None
    return value


def load_config(path=None, environ=None) -> AgentConfig:
    env = os.environ if environ is None else environ
    explicit = path is not None
    path = Path(path) if explicit else get_default_config_path()
    data = {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        if explicit:
            raise ValueError("Explicit config file does not exist") from None
    except (OSError, UnicodeError, ValueError):
        raise ValueError("Cannot read config: expected a readable UTF-8 JSON object") from None
    if not isinstance(data, dict):
        raise ValueError("Config must be a JSON object")
    server = validate_ws_url(env.get("STINTARA_WS", data.get("server_ws")), "server_ws")
    pitwall = validate_ws_url(env.get("PITWALL_WS", data.get("pitwall_ws", DEFAULT_PITWALL_WS)), "pitwall_ws")
    token = env.get("STINTARA_AGENT_TOKEN", data.get("token"))
    if not isinstance(token, str) or not token.strip() or token.strip().startswith(("change-", "replace-")):
        raise ValueError("Configure a nonempty, provisioned agent token; placeholders are rejected")
    # Retain the existing environment-token trimming behavior.
    return AgentConfig(server, token.strip() if "STINTARA_AGENT_TOKEN" in env else token, pitwall)
