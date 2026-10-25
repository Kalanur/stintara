import re
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_docker_build_identity_propagation():
    dockerfile = (ROOT / "server/Dockerfile").read_text()
    assert "ARG VERSION=dev" in dockerfile and "ARG REVISION=unknown" in dockerfile
    environment = dockerfile.replace("\\\n", " ").split("ENV ", 1)[1].split("\n", 1)[0]
    assert "STINTARA_BUILD_VERSION=$VERSION" in environment
    assert "STINTARA_BUILD_REVISION=$REVISION" in environment
    assert 'org.opencontainers.image.version="$VERSION"' in dockerfile
    assert 'org.opencontainers.image.revision="$REVISION"' in dockerfile
    assert "FROM python:3.13-slim" in dockerfile
    assert "http://127.0.0.1:8080/health" in dockerfile
    assert not re.search(r"\b[0-9a-f]{40}\b", dockerfile)
    workflow = yaml.load((ROOT / ".github/workflows/docker.yml").read_text(), Loader=yaml.BaseLoader)
    build = next(step for step in workflow["jobs"]["docker"]["steps"]
                 if step.get("uses", "").startswith("docker/build-push-action"))
    assert build["with"]["build-args"].splitlines() == [
        "VERSION=${{ steps.meta.outputs.version }}", "REVISION=${{ github.sha }}"]


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
    assert workflow["on"]["push"] == {"branches": ["**"], "tags": ["v*"]}
    assert set(workflow["on"]) == {"push", "pull_request", "workflow_dispatch"}
    assert not workflow["on"]["workflow_dispatch"]  # No channel override inputs.
    assert workflow["concurrency"] == {
        "group": "stintara-${{ github.workflow }}-${{ github.ref }}",
        "cancel-in-progress": "true",
    }
    test = workflow["jobs"]["test"]
    assert "if" not in test
    assert test["services"]["postgres"]["image"] == "postgres:17"
    runs = [step.get("run", "") for step in test["steps"]]
    for command in ("python -m compileall -q server/app agent tools tests", "ruff check server/app agent tools tests",
                    "python -m pytest -q", "node --test tests/ui.test.cjs"):
        assert command in runs
    assert any("docker compose config --quiet" in command for command in runs)
    job = workflow["jobs"]["docker"]
    assert job["needs"] == "test"
    assert job["permissions"]["packages"] == "write"
    steps = job["steps"]
    build = next(step for step in steps if step.get("uses", "").startswith("docker/build-push-action"))
    assert build["with"]["platforms"] == "linux/amd64"
    login = next(step for step in steps if step.get("uses", "").startswith("docker/login-action"))
    assert build["with"]["push"] == "${{ " + login["if"] + " }}"
    assert "github.event_name" in login["if"] and "github.ref" in login["if"]
    metadata = next(step for step in steps if step.get("uses", "").startswith("docker/metadata-action"))
    assert metadata["with"]["flavor"] == "latest=false"
    tags = metadata["with"]["tags"].splitlines()
    assert len(tags) == 5
    assert "type=sha,prefix=sha-" in tags
    assert any("pattern={{version}}" in tag for tag in tags)
    assert any("pattern={{major}}.{{minor}}" in tag for tag in tags)


def workflow_condition(expression, event, ref, default="main"):
    """Evaluate only the boolean expression subset used here, not the Actions runtime."""
    expression = expression.removeprefix("${{").removesuffix("}}").strip()
    github = SimpleNamespace(event_name=event, ref=ref,
                             event=SimpleNamespace(repository=SimpleNamespace(default_branch=default)))
    return bool(eval(expression.replace("&&", " and ").replace("||", " or "),
                     {"__builtins__": {}},
                     {"github": github, "startsWith": lambda value, prefix: value.startswith(prefix)}))


@pytest.mark.parametrize("event,ref,default,builds,pushes,channels,semver", [
    ("push", "refs/heads/feat/v0.1-baseline", "main", False, False, set(), False),
    ("pull_request", "refs/pull/1/merge", "main", True, False, set(), False),
    ("push", "refs/heads/dev", "main", True, True, {"dev"}, False),
    ("push", "refs/heads/main", "main", True, True, {"latest"}, False),
    ("push", "refs/tags/v1.2.3", "main", True, True, set(), True),
    ("workflow_dispatch", "refs/heads/feature/test", "main", False, False, set(), False),
    ("workflow_dispatch", "refs/heads/dev", "main", True, True, {"dev"}, False),
    ("workflow_dispatch", "refs/heads/main", "main", True, True, {"latest"}, False),
    ("workflow_dispatch", "refs/tags/v1.2.3", "main", True, True, set(), True),
    ("workflow_dispatch", "refs/tags/other", "main", False, False, set(), False),
    ("push", "refs/heads/dev-extra", "main", False, False, set(), False),
    ("push", "refs/heads/main", "other", False, False, set(), False),
    ("push", "refs/heads/other", "other", False, False, set(), False),
    ("push", "refs/heads/dev", "dev", True, True, {"dev"}, False),
    ("pull_request", "refs/heads/dev", "main", True, False, set(), False),
    ("pull_request", "refs/heads/main", "main", True, False, set(), False),
])
def test_workflow_channel_matrix(event, ref, default, builds, pushes, channels, semver):
    workflow = yaml.load((ROOT / ".github/workflows/docker.yml").read_text(), Loader=yaml.BaseLoader)
    job = workflow["jobs"]["docker"]
    steps = job["steps"]
    build = next(step for step in steps if step.get("uses", "").startswith("docker/build-push-action"))
    login = next(step for step in steps if step.get("uses", "").startswith("docker/login-action"))
    metadata = next(step for step in steps if step.get("uses", "").startswith("docker/metadata-action"))

    def enabled(expression):
        return workflow_condition(expression, event, ref, default)

    assert enabled(job["if"]) == builds
    # Check the independent step gates even when the whole job is skipped.
    assert enabled(login["if"]) == pushes
    assert enabled(build["with"]["push"]) == pushes
    raw_tags = set()
    version_rules = []
    for line in metadata["with"]["tags"].splitlines():
        attributes, separator, condition = line.partition(",enable=")
        rule = dict(item.split("=", 1) for item in attributes.split(","))
        active = enabled(condition) if separator else True
        if rule["type"] == "raw" and active:
            raw_tags.add(rule["value"])
        if rule["type"] == "semver":
            version_rules.append(active)
    assert raw_tags == channels
    assert version_rules == [semver, semver]


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
