"""Conservative public-source checks; never enumerate private operator files."""
import ipaddress
import re
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DIRECTORIES = ("agent", "server", "tools", "docs", "unraid", "tests", ".github")
EXCLUDED = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache", "build", "dist"}
PUBLIC_HOSTS = {"github.com", "ghcr.io", "hub.docker.com", "docs.docker.com", "localhost", "127.0.0.1",
                "stintara.example.invalid"}


def public_sources():
    yield from (p for p in ROOT.iterdir() if p.is_file() and (not p.name.startswith(".")
                or p.name in {".env.example", ".gitignore"}))
    for directory in PUBLIC_DIRECTORIES:
        for path in (ROOT / directory).rglob("*"):
            if path.is_file() and not any(part in EXCLUDED for part in path.relative_to(ROOT).parts):
                yield path


def test_no_private_paths_addresses_or_unapproved_url_hosts():
    for path in public_sources():
        text = path.read_text(encoding="utf-8-sig")
        label = path.relative_to(ROOT).as_posix()
        assert not re.search(r"(?i)\b[a-z]:[\\/]", text), f"Absolute Windows path in {label}"
        assert not re.search(r"/(?:Users|home)/[a-zA-Z0-9_-]+/", text), f"Personal home path in {label}"
        for value in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text):
            address = ipaddress.ip_address(value)
            assert address.is_loopback or address.is_unspecified, f"Non-placeholder address in {label}"
        for value in re.findall(r'\b(?:https?|wss?|postgresql)://[^\s\'"`<>]+', text):
            normalized = re.sub(r"\[PORT:(\d+)\]", r"\1", value.replace("[IP]", "localhost"))
            normalized = re.sub(r"\{[^}]*\}", "runtime-placeholder", normalized)
            hostname = urlsplit(normalized).hostname
            # Runtime interpolation is not a literal infrastructure name.
            if hostname and hostname != "runtime-placeholder":
                assert hostname in PUBLIC_HOSTS, f"Review new literal URL host in {label}"


def test_no_obvious_credentials_in_public_sources():
    patterns = (r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
                r"\bgh[pousr]_[A-Za-z0-9]{30,}\b", r"\bgithub_pat_[A-Za-z0-9_]{30,}\b")
    for path in public_sources():
        text = path.read_text(encoding="utf-8-sig")
        for pattern in patterns:
            assert not re.search(pattern, text), f"Potential credential in {path.relative_to(ROOT)}"


def test_private_operator_directory_is_ignored():
    assert "deploy/local/" in (ROOT / ".gitignore").read_text().splitlines()


def assert_synthetic_replay(path):
    import json

    from tools.replay.format import load

    metadata, rows = load(path)
    assert metadata["source"] == "synthetic", "Only wholly synthetic replay fixtures may be committed"
    text = json.dumps(rows).lower()
    for forbidden in ('"token"', '"server_ws"', '"pitwall_ws"', '"agent_hello"', '://'):
        assert forbidden not in text, "Replay fixture contains configuration or connection metadata"


def test_replay_fixtures_and_private_capture_hygiene():
    import subprocess

    assert "data/" in (ROOT / ".gitignore").read_text().splitlines()
    allowed = ROOT / "tests/fixtures/replay"
    fixtures = list(allowed.glob("*.jsonl"))
    assert fixtures
    for path in fixtures:
        assert_synthetic_replay(path)
    # Tracked files only: never read private capture directories or operator data.
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout.decode().split("\0")
    for name in filter(None, tracked):
        if name.endswith(".jsonl"):
            assert name.startswith("tests/fixtures/replay/"), "Capture outside synthetic fixture directory"
            assert_synthetic_replay(ROOT / name)
        assert not name.startswith("data/captures/"), "Private capture tracked"
        if name.startswith("tests/fixtures/replay/"):
            assert name.endswith(".jsonl"), "Only synthetic JSONL fixtures permitted, no mapping/config files"


def test_replay_policy_rejects_non_synthetic_sources(tmp_path):
    import json

    import pytest

    from tools.replay.format import header

    path = tmp_path / "fixture.jsonl"
    for source in ("real", "sanitized_real", "unknown"):
        path.write_text(json.dumps(header(source))+"\n")
        with pytest.raises((AssertionError, ValueError)):
            assert_synthetic_replay(path)
