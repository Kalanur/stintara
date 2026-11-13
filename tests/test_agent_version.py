import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

from agent import stintara_agent, version
from tools import agent_release as rel

ROOT = Path(__file__).resolve().parents[1]
SHA = "0123456789abcdef0123456789abcdef01234567"


def run_cli(*args, cwd=None):
    return subprocess.run([sys.executable, str(ROOT / "agent/stintara_agent.py"), *args],
                          capture_output=True, text=True, cwd=cwd, timeout=60)


def test_version_constants_and_source_defaults():
    assert version.AGENT_VERSION == "0.2.0"
    assert version.PROTOCOL == "stintara-agent-v1"
    assert version.DEFAULT_CHANNEL == "dev" and version.DEFAULT_REVISION == "unknown"
    assert version.version_text("release", SHA) == (
        f"Stintara Agent 0.2.0\nBuild: release\nRevision: {SHA}\nProtocol: stintara-agent-v1")


def test_protocol_matches_server():
    from server.app.protocol import Hello
    assert Hello.model_fields["protocol"].annotation.__args__ == (version.PROTOCOL,)


def test_version_flag_without_config_or_network(tmp_path):
    result = run_cli("--version", cwd=tmp_path)
    assert result.returncode == 0
    assert result.stdout.splitlines() == [
        "Stintara Agent 0.2.0", "Build: dev", "Revision: unknown", "Protocol: stintara-agent-v1"]


def test_version_flag_ignores_broken_config(tmp_path):
    broken = tmp_path / "bad.json"
    broken.write_text("{not json")
    assert run_cli("--version", "--config", str(broken)).returncode == 0


def test_cli_uses_central_version():
    source = (ROOT / "agent/stintara_agent.py").read_text()
    assert "0.1.0" not in source and "stintara-agent-v1" not in source
    assert stintara_agent.PROTOCOL == version.PROTOCOL


def test_agent_changelog_and_git_ignored_build_info():
    text = (ROOT / "agent/CHANGELOG.md").read_text()
    assert f"## {version.AGENT_VERSION}" in text and "## 0.1.0" in text
    assert "agent/_build_info.py" in (ROOT / ".gitignore").read_text().splitlines()
    assert "retroactively" in rel.changelog_section("0.1.0")
    with pytest.raises(ValueError):
        rel.changelog_section("9.9.9")


def test_build_info_injection(tmp_path):
    target = rel.write_build_info("release", SHA, tmp_path / "_build_info.py")
    ns = {}
    exec(target.read_text(), ns)
    assert (ns["BUILD_CHANNEL"], ns["BUILD_REVISION"]) == ("release", SHA)


@pytest.mark.parametrize("channel,revision", [
    ("release", "unknown"), ("ci", "unknown"), ("release", ""), ("release", SHA[:39]),
    ("release", SHA.upper()), ("ci", "abc"), ("bogus", SHA), ("dev", "not-a-sha")])
def test_non_dev_builds_reject_unknown_or_malformed_revision(tmp_path, channel, revision):
    with pytest.raises(ValueError):
        rel.write_build_info(channel, revision, tmp_path / "b.py")
    assert not (tmp_path / "b.py").exists()


def test_build_info_cli_fails_before_writing(monkeypatch, tmp_path):
    monkeypatch.setattr(rel, "AGENT_DIR", tmp_path)
    assert rel.main(["build-info", "--channel", "release", "--revision", "unknown"]) == 1
    assert not (tmp_path / "_build_info.py").exists()


def test_dev_build_allows_unknown():
    rel.validate_identity("dev", "unknown")
    rel.validate_identity("dev", SHA)


def test_windows_version_resource():
    text = rel.version_file_text("0.2.0")
    assert "filevers=(0, 2, 0, 0)" in text and "prodvers=(0, 2, 0, 0)" in text
    assert "'ProductVersion', '0.2.0'" in text and "'ProductName', 'Stintara Agent'" in text
    assert "'OriginalFilename', 'StintaraAgent.exe'" in text


def make_dist(path):
    path.mkdir(parents=True)
    (path / "StintaraAgent.exe").write_bytes(b"MZ-fake")
    return path


def test_package_contains_exactly_public_files_and_checksum(tmp_path):
    dist = make_dist(tmp_path / "dist")
    # Operator files in dist must never be picked up.
    (dist / "stintara-agent.json").write_text('{"token": "secret"}')
    (dist / "agent.log").write_text("log")
    archive, sums = rel.package(dist, "0.2.0")
    assert archive.name == "StintaraAgent-0.2.0-windows-x64.zip"
    with zipfile.ZipFile(archive) as zf:
        assert sorted(zf.namelist()) == ["README.txt", "StintaraAgent.exe", "stintara-agent.example.json"]
        blob = b"".join(zf.read(n) for n in zf.namelist())
    assert b"secret" not in blob
    line = sums.read_text().strip()
    assert re.fullmatch(r"[0-9a-f]{64}  StintaraAgent-0\.2\.0-windows-x64\.zip", line)
    assert line.split()[0] == rel.sha256(archive)
    rel.verify_package(archive, sums, "0.2.0")


def test_package_is_deterministic(tmp_path):
    first = rel.sha256(rel.package(make_dist(tmp_path / "a"), "0.2.0")[0])
    second = rel.sha256(rel.package(make_dist(tmp_path / "b"), "0.2.0")[0])
    assert first == second


def test_verify_package_rejects_extra_files_and_bad_checksum(tmp_path):
    dist = make_dist(tmp_path / "dist")
    archive, sums = rel.package(dist, "0.2.0")
    with zipfile.ZipFile(archive, "a") as zf:
        zf.writestr("stintara-agent.json", "{}")
    with pytest.raises(ValueError):
        rel.verify_package(archive, sums, "0.2.0")
    archive, sums = rel.package(dist, "0.2.0")
    sums.write_text("0" * 64 + "  " + archive.name + "\n")
    with pytest.raises(ValueError):
        rel.verify_package(archive, sums, "0.2.0")


def test_release_readme_and_example_are_public_only():
    readme = (ROOT / "agent/RELEASE-README.txt").read_text()
    assert "not code-signed" in readme and "SHA256SUMS.txt" in readme
    example = json.loads((ROOT / "agent/stintara-agent.example.json").read_text())
    assert "invalid" in example["server_ws"] and example["token"].startswith("replace-with")


def test_tag_must_match_version():
    rel.check_tag("agent-v0.2.0", "0.2.0")
    for tag in ("v0.2.0", "agent-v0.2.1", "agent-v0.2", "agent-v0.2.0-rc1", "server-v0.2.0"):
        with pytest.raises(ValueError):
            rel.check_tag(tag, "0.2.0")


def test_verify_exe_rejects_unknown_revision_and_mismatch(tmp_path, monkeypatch):
    class Done:
        stdout = "Stintara Agent 0.2.0\nBuild: release\nRevision: unknown\nProtocol: stintara-agent-v1\n"
    monkeypatch.setattr(rel.subprocess, "run", lambda *a, **k: Done)
    with pytest.raises(ValueError):
        rel.verify_exe(tmp_path / "x.exe", "0.2.0", "release", SHA)


def test_workflows_release_only_on_explicit_agent_tag():
    path = ROOT / ".github/workflows/agent-release.yml"
    release = yaml.safe_load(path.read_text())
    trigger = release.get("on", release.get(True))
    assert set(trigger) == {"push"} and "branches" not in trigger["push"]
    assert trigger["push"]["tags"] == ["agent-v[0-9]+.[0-9]+.[0-9]+"]
    text = path.read_text()
    assert "--draft" in text and "--prerelease" in text and "check-tag" in text
    assert "ghcr" not in text.lower() and "docker" not in text.lower()
    ci = (ROOT / ".github/workflows/agent-windows.yml").read_text()
    assert "gh release" not in ci
    assert yaml.safe_load(ci)["permissions"] == {"contents": "read"}
