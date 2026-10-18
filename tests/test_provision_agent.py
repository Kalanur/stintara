import base64
import hashlib
import json
from unittest.mock import AsyncMock

import pytest

from server.app import main
from server.app import provision_agent as provision


@pytest.mark.parametrize("slug", ["", "UPPER", "two words", "-start", "end-", "two--hyphens"])
def test_invalid_slug_rejected(slug):
    with pytest.raises(provision.ProvisionError):
        provision.validate_inputs(slug, "Test Driver", "tester-pc")


@pytest.fixture
def db_environment(monkeypatch):
    for name in ("DATABASE_URL", "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER",
                 "POSTGRES_PASSWORD", "POSTGRES_PASSWORD_FILE"):
        monkeypatch.delenv(name, raising=False)


def test_database_config_file_precedence_and_dsn(tmp_path, monkeypatch, db_environment):
    path = tmp_path / "password.secret"
    path.write_bytes(b"test-only-file\r\n")
    monkeypatch.setenv("POSTGRES_PASSWORD", "test-only-env")
    assert provision.database_options()["password"] == "test-only-env"
    monkeypatch.setenv("POSTGRES_PASSWORD_FILE", str(path))
    assert provision.database_options()["password"] == "test-only-file"
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/test_only")
    monkeypatch.setenv("POSTGRES_PASSWORD_FILE", "")
    assert provision.database_options() == {"dsn": "postgresql://localhost/test_only"}


@pytest.mark.parametrize("with_url", [False, True])
def test_cli_outputs_secret_exactly_once_after_success(monkeypatch, capsys, with_url):
    monkeypatch.setattr(provision, "provision_from_environment", AsyncMock(return_value="test-only-output"))
    args = ["--driver-slug", "test-driver", "--driver-name", "Test Driver", "--agent-name", "tester-pc"]
    if with_url:
        args += ["--server-ws", "wss://stintara.example.invalid/ws/ingest"]
    provision.cli(args)
    output = capsys.readouterr()
    assert output.out.count("test-only-output") == 1 and "test-only-output" not in output.err
    if with_url:
        assert json.loads(output.out)["token"] == "test-only-output"
    else:
        assert output.out == "test-only-output\n"


def test_cli_database_errors_are_safe(monkeypatch, capsys):
    monkeypatch.setattr(provision, "provision_from_environment", AsyncMock(side_effect=RuntimeError("test-only-private")))
    with pytest.raises(SystemExit) as exc:
        provision.cli(["--driver-slug", "test-driver", "--driver-name", "Test Driver", "--agent-name", "tester-pc"])
    output = capsys.readouterr()
    assert exc.value.code == 1 and output.out == "" and "test-only-private" not in output.err


def test_cli_rejects_bad_url_before_database_access(monkeypatch, capsys):
    provision_call = AsyncMock()
    monkeypatch.setattr(provision, "provision_from_environment", provision_call)
    with pytest.raises(SystemExit):
        provision.cli(["--driver-slug", "test-driver", "--driver-name", "Test Driver", "--agent-name", "tester-pc",
                       "--server-ws", "https://stintara.example.invalid/test-only-private"])
    provision_call.assert_not_awaited()
    output = capsys.readouterr()
    assert not output.out and "test-only-private" not in output.err


async def test_provisioning_identity_hash_reuse_and_duplicate_rollback(db, monkeypatch, caplog):
    monkeypatch.setattr(main, "pool", db)
    async with db.acquire() as conn:
        token = await provision.provision_agent(conn, "test-driver", "Test Driver", "tester-pc")
        assert len(base64.urlsafe_b64decode(token + "=")) == 32
        row = await conn.fetchrow("SELECT * FROM agents WHERE name='tester-pc'")
        driver_id = await conn.fetchval("SELECT id FROM drivers WHERE slug='test-driver'")
        assert row["id"].version == 7 and driver_id.version == 7
        assert row["token_sha256"] == hashlib.sha256(token.encode()).hexdigest()
        assert token not in str(dict(row))
        second = await provision.provision_agent(conn, "test-driver", "Renamed Driver", "tester-pc-two")
        assert second != token
        assert await conn.fetchval("SELECT count(*) FROM drivers") == 1
        assert await conn.fetchval("SELECT driver_id FROM agents WHERE name='tester-pc-two'") == driver_id
        with pytest.raises(provision.ProvisionError):
            await provision.provision_agent(conn, "test-driver", "Must Not Replace", "tester-pc")
        assert await conn.fetchval("SELECT display_name FROM drivers WHERE id=$1", driver_id) == "Renamed Driver"
        with pytest.raises(provision.ProvisionError):
            await provision.provision_agent(conn, "unused-driver", "Test Driver", "tester-pc")
        assert await conn.fetchval("SELECT count(*) FROM drivers") == 1
        assert await conn.fetchval("SELECT token_sha256 FROM agents WHERE name='tester-pc'") == row["token_sha256"]
        # Equal display names do not merge explicitly distinct driver slugs.
        await provision.provision_agent(conn, "other-driver", "Renamed Driver", "tester-pc-three")
        assert await conn.fetchval("SELECT count(*) FROM drivers") == 2
    assert (await main.authenticate_agent(token))["id"] == row["id"]
    assert await main.authenticate_agent("test-only-wrong") is None
    assert token not in caplog.text and second not in caplog.text
