from pathlib import Path

import yaml


def test_disposable_postgres_is_loopback_only_and_separate_from_deployment():
    config = yaml.safe_load((Path(__file__).parents[1] / "docker-compose.test.yml").read_text())
    assert set(config["services"]) == {"postgres"}
    postgres = config["services"]["postgres"]
    assert postgres["image"] == "postgres:17"
    assert postgres["ports"] == ["127.0.0.1:55432:5432"]
    assert postgres["environment"] == {
        "POSTGRES_DB": "stintara_tests",
        "POSTGRES_USER": "stintara_test",
        "POSTGRES_PASSWORD": "local-test-only",
    }
    assert postgres["tmpfs"] == ["/var/lib/postgresql/data"]
    assert "volumes" not in postgres
    assert "networks" not in postgres  # Dedicated Compose project default network.
    assert "volumes" not in config
    assert "networks" not in config
    assert postgres["healthcheck"]["test"] == [
        "CMD-SHELL", "pg_isready -U stintara_test -d stintara_tests",
    ]
