"""Single source of truth for the Stintara Agent identity.

The agent version is independent of the server version and revision. Build
metadata (channel and revision) is injected at build time into the generated,
Git-ignored ``_build_info.py``; source runs fall back to conservative defaults.
"""
from __future__ import annotations

AGENT_VERSION = "0.3.0"
PROTOCOL = "stintara-agent-v1"
# Capabilities this build really implements and declares in agent_hello. Only add a name once the code does it.
# source_epoch: every forwarded message carries a random id of the current Pitwall connection (not the uplink).
CAPABILITIES = ("source_epoch",)

DEFAULT_CHANNEL = "dev"
DEFAULT_REVISION = "unknown"

try:
    from ._build_info import BUILD_CHANNEL, BUILD_REVISION
except ImportError:
    try:  # python agent/stintara_agent.py or the PyInstaller entry script
        from _build_info import BUILD_CHANNEL, BUILD_REVISION
    except ImportError:
        BUILD_CHANNEL, BUILD_REVISION = DEFAULT_CHANNEL, DEFAULT_REVISION


def version_text(channel: str = BUILD_CHANNEL, revision: str = BUILD_REVISION) -> str:
    return (f"Stintara Agent {AGENT_VERSION}\n"
            f"Build: {channel}\n"
            f"Revision: {revision}\n"
            f"Protocol: {PROTOCOL}")


def hello_info(channel: str = BUILD_CHANNEL, revision: str = BUILD_REVISION) -> dict:
    """Identification sent in agent_hello (no config, token, host or user data)."""
    return {"version": AGENT_VERSION, "channel": channel, "revision": revision}


def banner(channel: str = BUILD_CHANNEL, revision: str = BUILD_REVISION) -> str:
    return f"Stintara Agent {AGENT_VERSION} (build {channel}, revision {revision})"
