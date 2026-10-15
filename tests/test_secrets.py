import traceback
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from server.app import main
from server.app.secrets import read_secret


@pytest.fixture(autouse=True)
def isolated_secrets(monkeypatch):
    for name in ("POSTGRES_PASSWORD", "BOOTSTRAP_AGENT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name + "_FILE", raising=False)
    monkeypatch.setattr(main, "POSTGRES_PASSWORD", "")
    monkeypatch.setattr(main, "DATABASE_URL", "")


@pytest.mark.parametrize("name", ["POSTGRES_PASSWORD", "BOOTSTRAP_AGENT_TOKEN"])
def test_environment_and_default(name, monkeypatch):
    assert read_secret(name, name + "_FILE", "test-only-default") == "test-only-default"
    monkeypatch.setenv(name, " test-only-environment \n")
    assert read_secret(name, name + "_FILE") == " test-only-environment \n"


@pytest.mark.parametrize("ending", ["", "\n", "\r\n", "\n\n"])
@pytest.mark.parametrize("name", ["POSTGRES_PASSWORD", "BOOTSTRAP_AGENT_TOKEN"])
def test_file_precedence_utf8_and_exact_content(name, ending, tmp_path, monkeypatch):
    value = " test-only-ä\r\ninside \t"
    path = tmp_path / "value.secret"
    path.write_bytes((value + ending).encode("utf-8"))
    monkeypatch.setenv(name + "_FILE", str(path))
    assert read_secret(name, name + "_FILE") == value
    monkeypatch.setenv(name, "test-only-ignored-environment")
    assert read_secret(name, name + "_FILE") == value


@pytest.mark.parametrize("case", ["empty", "newlines", "missing", "directory", "bad_utf8", "unreadable", "empty_path"])
@pytest.mark.parametrize("name", ["POSTGRES_PASSWORD", "BOOTSTRAP_AGENT_TOKEN"])
def test_file_errors_are_safe_and_never_fall_back(name, case, tmp_path, monkeypatch, caplog):
    value = "test-only-private-marker"
    path = tmp_path / value
    if case == "directory":
        path.mkdir()
    elif case not in ("missing", "empty_path"):
        path.write_bytes({"empty": b"", "newlines": b"\r\n", "bad_utf8": b"test-only-private-marker\xff"}.get(case, value.encode()))
    monkeypatch.setenv(name, value)
    monkeypatch.setenv(name + "_FILE", "" if case == "empty_path" else str(path))
    if case == "unreadable":
        def denied(*args):
            raise PermissionError(value)
        monkeypatch.setattr(Path, "read_bytes", denied)
    with pytest.raises(ValueError) as exc:
        read_secret(name, name + "_FILE")
    diagnostic = "".join(traceback.format_exception(exc.value)) + caplog.text
    assert value not in diagnostic
    assert str(path) not in str(exc.value)
    assert name + "_FILE" in str(exc.value)
    if case == "directory":
        assert "directory" in str(exc.value)


@pytest.mark.parametrize("name", ["POSTGRES_PASSWORD", "BOOTSTRAP_AGENT_TOKEN"])
@pytest.mark.parametrize("source", ["env", "file"])
@pytest.mark.parametrize("value", ["", "change-test-only", "replace-test-only"])
async def test_bootstrap_rejects_placeholders_without_disclosure(name, source, value, tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("POSTGRES_PASSWORD", "test-only-db")
    monkeypatch.setenv("BOOTSTRAP_AGENT_TOKEN", "test-only-agent")
    if source == "env":
        monkeypatch.setenv(name, value)
    else:
        path = tmp_path / "value.secret"
        path.write_text(value, encoding="utf-8")
        monkeypatch.setenv(name + "_FILE", str(path))
    connect = AsyncMock()
    monkeypatch.setattr(main, "create_db_pool", connect)
    with pytest.raises(ValueError) as exc:
        await main.bootstrap()
    connect.assert_not_awaited()
    if value:
        assert value not in "".join(traceback.format_exception(exc.value)) + caplog.text


@pytest.mark.parametrize("files", [False, True])
async def test_bootstrap_and_database_connection_use_selected_secrets(files, tmp_path, monkeypatch, caplog):
    password, token = " test-only-db ", " test-only-agent "
    for name, value in (("POSTGRES_PASSWORD", password), ("BOOTSTRAP_AGENT_TOKEN", token)):
        monkeypatch.setenv(name, value if not files else "test-only-ignored")
        if files:
            path = tmp_path / (name + ".secret")
            path.write_bytes((value + "\r\n").encode())
            monkeypatch.setenv(name + "_FILE", str(path))
    conn = AsyncMock()
    conn.fetchrow.side_effect = [{"id": "driver-id"}, {"id": "agent-id"}]
    conn.transaction = MagicMock()
    conn.transaction.return_value.__aenter__ = AsyncMock()
    conn.transaction.return_value.__aexit__ = AsyncMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock()
    connect = AsyncMock(return_value=pool)
    monkeypatch.setattr(main.asyncpg, "create_pool", connect)
    monkeypatch.setattr(main, "migrate_sample_ids", AsyncMock())
    monkeypatch.setattr(main, "pool", None)
    await main.bootstrap()
    assert connect.call_args.kwargs["password"] == password
    assert conn.execute.call_args.args[-1] == main.token_hash(token if files else token.strip())
    assert password not in caplog.text and token not in caplog.text


async def test_database_url_bypasses_unused_password_file(monkeypatch):
    monkeypatch.setattr(main, "DATABASE_URL", "postgresql://test-only-user:test-only-password@localhost/test_only")
    monkeypatch.setenv("POSTGRES_PASSWORD_FILE", "")
    monkeypatch.setenv("BOOTSTRAP_AGENT_TOKEN", "test-only-agent")
    # Stop after secret validation, before any database operation.
    monkeypatch.setenv("BOOTSTRAP_DRIVER_SLUG", "INVALID SLUG")
    with pytest.raises(ValueError, match="BOOTSTRAP_DRIVER_SLUG"):
        await main.bootstrap()
    connect = AsyncMock()
    monkeypatch.setattr(main.asyncpg, "create_pool", connect)
    await main.create_db_pool()
    connect.assert_awaited_once_with(main.DATABASE_URL, min_size=1, max_size=10)
