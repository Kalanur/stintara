from pathlib import Path
from xml.etree import ElementTree

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_unraid_templates():
    server = ElementTree.parse(ROOT / "unraid/stintara.xml").getroot()
    postgres = ElementTree.parse(ROOT / "unraid/stintara-postgres.xml").getroot()
    assert server.findtext("Repository") == "ghcr.io/kalanur/stintara:latest"
    assert postgres.findtext("Repository") == "postgres:17"
    assert server.findtext("Network") == postgres.findtext("Network") == "stintara"
    assert server.findtext("WebUI") == "http://[IP]:[PORT:8080]/"
    assert not postgres.findall("Config[@Type='Port']")
    port = server.find("Config[@Type='Port']")
    assert port.get("Target") == port.get("Default") == port.text == "8080"
    for root in (server, postgres):
        assert len(root.findall("Network")) == 1
        assert not root.findtext("PostArgs")
        assert root.findtext("ExtraParams") == "--restart=unless-stopped --security-opt=no-new-privileges"
        assert not root.findall("Config[@Type='Network']")
        for field in root.findall("Config"):
            if field.get("Target") in ("POSTGRES_PASSWORD", "BOOTSTRAP_AGENT_TOKEN", "DATABASE_URL"):
                assert not field.get("Default") and not field.text
                assert field.get("Mask") == "true"
    assert server.find("Config[@Type='Path']").get("Target") == "/data"
    assert postgres.find("Config[@Type='Path']").get("Target") == "/var/lib/postgresql/data"


def test_compose_structure_and_required_secrets():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    pg, app = compose["services"]["postgres"], compose["services"]["app"]
    assert pg["image"] == "postgres:17" and "ports" not in pg
    assert compose["networks"]["default"]["name"] == "stintara"
    assert set(compose["networks"]) == {"default"}
    assert not compose["networks"]["default"].get("external", False)
    for service in (pg, app):
        assert "network_mode" not in service
        assert service.get("networks", ["default"]) == ["default"]
    assert app["ports"] == ["${APP_PORT:-8080}:8080"]
    assert app["environment"]["POSTGRES_HOST"] == pg["container_name"]
    assert ":?" in pg["environment"]["POSTGRES_PASSWORD"]
    assert ":?" in app["environment"]["BOOTSTRAP_AGENT_TOKEN"]


def test_workflow_yaml_and_publish_contract():
    # BaseLoader preserves GitHub's `on` key rather than treating it as YAML 1.1 boolean.
    workflow = yaml.load((ROOT / ".github/workflows/docker.yml").read_text(), Loader=yaml.BaseLoader)
    assert "push" in workflow["on"]
    job = workflow["jobs"]["docker"]
    assert job["needs"] == "test"
    assert job["permissions"]["packages"] == "write"
    steps = job["steps"]
    build = next(step for step in steps if step.get("uses", "").startswith("docker/build-push-action"))
    assert build["with"]["platforms"] == "linux/amd64"
    assert build["with"]["push"] == "${{ github.event_name != 'pull_request' }}"
    login = next(step for step in steps if step.get("uses", "").startswith("docker/login-action"))
    assert login["if"] == "github.event_name != 'pull_request'"
    assert job["if"] == ("github.event_name == 'pull_request' || "
                         "github.ref == format('refs/heads/{0}', github.event.repository.default_branch) || "
                         "startsWith(github.ref, 'refs/tags/')")
    metadata = next(step for step in steps if step.get("uses", "").startswith("docker/metadata-action"))
    assert metadata["with"]["flavor"] == "latest=false"
    tags = next(step["with"]["tags"] for step in steps if step.get("uses", "").startswith("docker/metadata-action"))
    assert "is_default_branch" in tags and "type=semver" in tags and "type=sha" in tags


def test_generic_bootstrap_defaults_agree():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    server = ElementTree.parse(ROOT / "unraid/stintara.xml").getroot()
    example = dict(line.split("=", 1) for line in (ROOT / ".env.example").read_text().splitlines()
                   if line and not line.startswith("#"))
    defaults = {"BOOTSTRAP_DRIVER_NAME": "Stintara Driver", "BOOTSTRAP_DRIVER_SLUG": "driver",
                "BOOTSTRAP_AGENT_NAME": "gaming-pc"}
    for key, value in defaults.items():
        assert example[key] == value
        assert compose["services"]["app"]["environment"][key] == "${" + key + ":-" + value + "}"
        field = server.find(f"Config[@Target='{key}']")
        assert field.get("Default") == field.text == value
    for secret in ("POSTGRES_PASSWORD", "BOOTSTRAP_AGENT_TOKEN", "DATABASE_URL"):
        assert not example[secret]
