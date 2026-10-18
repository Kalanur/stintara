import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_windows_workflow_only_distributes_exe_and_public_example():
    workflow = yaml.safe_load((ROOT / ".github/workflows/agent-windows.yml").read_text())
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["windows-agent"]
    assert job["runs-on"] == "windows-latest"
    steps = job["steps"]
    assert next(s for s in steps if "setup-python" in s.get("uses", ""))["with"]["python-version"] == "3.13"
    upload = next(s for s in steps if "upload-artifact" in s.get("uses", ""))
    assert upload["with"]["path"].splitlines() == ["dist/StintaraAgent.exe", "dist/stintara-agent.example.json"]
    assert any("StintaraAgent.exe --help" in s.get("run", "") for s in steps)
    assert not any("release" in s.get("uses", "").lower() for s in steps)
    example = json.loads((ROOT / "agent/stintara-agent.example.json").read_text())
    assert example == {"server_ws": "wss://stintara.example.invalid/ws/ingest",
                       "pitwall_ws": "ws://127.0.0.1:9000?format=json", "token": "replace-with-provisioned-token"}
    ignored = (ROOT / ".gitignore").read_text().splitlines()
    assert all(name in ignored for name in ("stintara-agent.json", "build/", "dist/"))
