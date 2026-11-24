"""Single-instance protection: identity, decision logic and cli() ordering (platform independent, fake backend)."""
from unittest.mock import AsyncMock

import pytest

from agent import single_instance, stintara_agent
from agent.single_instance import ERROR_ACCESS_DENIED, ERROR_ALREADY_EXISTS, AlreadyRunning, CreateResult

WS = "wss://stintara.example.invalid/ws/ingest"
TOKEN = "test-only-secret-token"


class FakeBackend:
    def __init__(self, *results, probe=single_instance.MISSING):
        self.results = list(results)
        self.names, self.closed, self.probed = [], [], []
        self.probe_result = probe

    def create(self, name):
        self.names.append(name)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def probe(self, name):
        self.probed.append(name)
        if isinstance(self.probe_result, Exception):
            raise self.probe_result
        return self.probe_result

    def close(self, handle):
        self.closed.append(handle)


@pytest.fixture(autouse=True)
def clean_held():
    yield
    single_instance._held.clear()


def test_name_is_stable_hashed_and_leaks_nothing():
    name = single_instance.mutex_name(WS, TOKEN)
    assert name == single_instance.mutex_name(WS, TOKEN)
    assert name.startswith("Global\\StintaraAgent-") and len(name.split("-")[-1]) == 32
    assert TOKEN not in name and "stintara.example" not in name
    assert single_instance.mutex_name(WS, TOKEN, "Local").startswith("Local\\StintaraAgent-")


def test_identity_differs_per_token_and_server_but_not_per_config_path_or_case():
    base = single_instance.instance_id(WS, TOKEN)
    assert single_instance.instance_id(WS, TOKEN + "x") != base
    assert single_instance.instance_id("ws://localhost:9/ws/ingest", TOKEN) != base
    assert single_instance.instance_id(WS.replace("stintara", "STINTARA").replace("wss", "WSS"), TOKEN) == base
    # field boundaries cannot be shifted
    assert single_instance.instance_id("ws://localhost/b", "c") != single_instance.instance_id("ws://localhost/", "bc")


def test_first_instance_holds_the_lock_and_keeps_a_reference():
    backend = FakeBackend(CreateResult("h1", False))
    lock = single_instance.acquire(WS, TOKEN, backend=backend)
    assert lock.active and lock.degraded is None
    assert backend.closed == [] and single_instance._held == [lock]


def test_second_instance_is_rejected_and_only_closes_its_own_handle():
    backend = FakeBackend(CreateResult("h2", True))
    with pytest.raises(AlreadyRunning):
        single_instance.acquire(WS, TOKEN, backend=backend)
    assert backend.closed == ["h2"] and single_instance._held == []


def test_access_denied_with_nothing_existing_falls_back_to_local():
    backend = FakeBackend(CreateResult(None, False, ERROR_ACCESS_DENIED), CreateResult("h", False))
    lock = single_instance.acquire(WS, TOKEN, backend=backend)
    assert [n.split("\\")[0] for n in backend.names] == ["Global", "Local"]
    assert backend.probed == [backend.names[0]]
    assert lock.active and "logon session" in lock.degraded


@pytest.mark.parametrize("found", [single_instance.DENIED, single_instance.PRESENT])
def test_access_denied_on_an_existing_global_object_blocks_instead_of_falling_back(found):
    backend = FakeBackend(CreateResult(None, False, ERROR_ACCESS_DENIED), probe=found)
    with pytest.raises(AlreadyRunning):
        single_instance.acquire(WS, TOKEN, backend=backend)
    assert len(backend.names) == 1, "no independent Local object may be created"
    assert single_instance._held == []


@pytest.mark.parametrize("probe", [6, OSError("boom")])
def test_access_denied_with_an_unclear_probe_fails_open_without_local_object(probe):
    backend = FakeBackend(CreateResult(None, False, ERROR_ACCESS_DENIED), probe=probe)
    lock = single_instance.acquire(WS, TOKEN, backend=backend)
    assert not lock.active and lock.degraded and "boom" not in lock.degraded
    assert len(backend.names) == 1


def test_access_denied_on_local_is_not_probed_and_fails_open():
    backend = FakeBackend(CreateResult(None, False, ERROR_ACCESS_DENIED), CreateResult(None, False, ERROR_ACCESS_DENIED))
    lock = single_instance.acquire(WS, TOKEN, backend=backend)
    assert not lock.active and len(backend.probed) == 1


def test_windows_backend_requests_synchronize_only_and_explicit_dacl():
    # The access mask is what makes an existing, differently-privileged object detectable.
    assert single_instance.WindowsMutexBackend.SYNCHRONIZE == 0x00100000
    sddl = single_instance.WindowsMutexBackend.SDDL
    assert "(A;;0x00100000;;;WD)" in sddl and sddl.count("0x00100000") == 1   # others: SYNCHRONIZE only


@pytest.mark.parametrize("results", [
    [CreateResult(None, False, 6)],
    [CreateResult(None, False, ERROR_ACCESS_DENIED), CreateResult(None, False, ERROR_ACCESS_DENIED)],
    [OSError("boom")],
])
def test_other_failures_fail_open_with_a_secret_free_warning(results):
    lock = single_instance.acquire(WS, TOKEN, backend=FakeBackend(*results))
    assert not lock.active and lock.degraded
    assert TOKEN not in lock.degraded and "boom" not in lock.degraded


def test_conflict_error_code_constant_and_non_windows_noop():
    assert ERROR_ALREADY_EXISTS == 183
    lock = single_instance.acquire(WS, TOKEN, platform="linux")
    assert not lock.active and "only available on Windows" in lock.degraded


def configured(monkeypatch, tmp_path):
    for name in ("STINTARA_WS", "PITWALL_WS", "STINTARA_AGENT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "stintara-agent.json"
    path.write_text(f'{{"server_ws": "{WS}", "token": "{TOKEN}"}}', encoding="utf-8")
    return str(path)


def test_cli_conflict_exits_3_before_banner_or_any_network(monkeypatch, tmp_path, capsys):
    def deny(server_ws, token):
        raise AlreadyRunning
    run, connect = AsyncMock(), AsyncMock()
    monkeypatch.setattr(stintara_agent, "acquire", deny)
    monkeypatch.setattr(stintara_agent, "main", run)
    monkeypatch.setattr(stintara_agent.websockets, "connect", connect)
    monkeypatch.setattr(stintara_agent.asyncio, "run", lambda *a, **k: pytest.fail("event loop started"))
    with pytest.raises(SystemExit) as exc:
        stintara_agent.cli(["--config", configured(monkeypatch, tmp_path)])
    assert exc.value.code == single_instance.EXIT_ALREADY_RUNNING == 3
    out = capsys.readouterr()
    assert "already running" in out.err and "not affected" in out.err
    assert TOKEN not in out.err + out.out and "Stintara Agent 0." not in out.out
    run.assert_not_called()
    connect.assert_not_called()


def test_cli_passes_the_effective_identity_to_the_lock(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(stintara_agent, "acquire", lambda s, t: seen.append((s, t)) or single_instance.InstanceLock())
    monkeypatch.setattr(stintara_agent, "main", AsyncMock())
    path = configured(monkeypatch, tmp_path)
    monkeypatch.setenv("STINTARA_AGENT_TOKEN", " env-token ")
    stintara_agent.cli(["--config", path])
    assert seen == [(WS, "env-token")]


def test_cli_prints_degraded_warning_and_still_runs(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(stintara_agent, "acquire", lambda s, t: single_instance.InstanceLock(degraded="Lock unavailable."))
    run = AsyncMock()
    monkeypatch.setattr(stintara_agent, "main", run)
    stintara_agent.cli(["--config", configured(monkeypatch, tmp_path)])
    assert "Warning: Lock unavailable." in capsys.readouterr().out
    run.assert_awaited_once()


def test_version_and_config_errors_exit_before_the_lock(monkeypatch, tmp_path):
    monkeypatch.setattr(stintara_agent, "acquire", lambda s, t: pytest.fail("lock taken too early"))
    with pytest.raises(SystemExit) as exc:
        stintara_agent.cli(["--version"])
    assert exc.value.code == 0
    with pytest.raises(SystemExit) as exc:
        stintara_agent.cli(["--config", str(tmp_path / "missing.json")])
    assert exc.value.code == 2
