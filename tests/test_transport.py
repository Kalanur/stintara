import asyncio
import json
from contextlib import suppress
from unittest.mock import AsyncMock

import httpx
import pytest
import uvicorn
import websockets
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.exceptions import ConnectionClosed

from agent import stintara_agent
from agent.config import AgentConfig
from server.app import main
from tests.conftest import make_agent


@pytest.fixture
def ingest_client(monkeypatch):
    main.active_agents.clear()
    monkeypatch.setattr(main, "authenticate_agent", AsyncMock(
        side_effect=lambda token: {"id": "test-agent", "slug": "driver"} if token == "test-secret" else None))
    handler = AsyncMock()
    monkeypatch.setattr(main, "handle_message", handler)
    # No lifespan: transport contract tests isolate authentication/persistence.
    yield TestClient(main.app), handler
    main.active_agents.clear()


def hello(ws, token="test-secret"):
    ws.send_json({"type": "agent_hello", "protocol": "stintara-agent-v1", "token": token, "simulator": "lmu"})


def test_welcome_version_is_application_version_not_build_channel(ingest_client, monkeypatch):
    monkeypatch.setenv("STINTARA_BUILD_VERSION", "dev")
    monkeypatch.setenv("STINTARA_BUILD_REVISION", "0123456789abcdef" * 2 + "01234567")
    client, _ = ingest_client
    with client.websocket_connect("/ws/ingest") as ws:
        hello(ws)
        assert ws.receive_json() == {"type": "agent_welcome", "driver_slug": "driver", "version": "0.1.0"}


def frame(ws, kind="telemetry", payload=None, protocol="stintara-agent-v1"):
    ws.send_json({"protocol": protocol, "message_type": kind, "payload": payload if payload is not None else {"fuel": 42}})


def test_socket_disconnect_clears_only_pending_convergence_state(ingest_client, monkeypatch):
    client, _ = ingest_client
    state = {"pending_convergence": {"candidate_id": "synthetic"},
             "convergence_invalidated": True, "session_id": None, "track_name": "Synthetic Circuit"}
    monkeypatch.setitem(main.runtime, "test-agent", state)
    with client.websocket_connect("/ws/ingest") as ws:
        hello(ws)
        assert ws.receive_json()["type"] == "agent_welcome"
    assert state == {"session_id": None, "track_name": "Synthetic Circuit"}


def test_transient_session_info_is_not_forwarded_over_existing_connection(ingest_client, caplog):
    client, handler = ingest_client
    messages = [
        {"type": "ConnectionStatus", "game_connected": True},
        {"type": "SessionInfo", "track_name": "Paul Ricard - ELMS"},
        *[{"type": "SessionInfo", "track_name": ""} for _ in range(30)],
        {"type": "SessionInfo", "track_name": " \t"},
        {"type": "ScoringUpdate", "session_type": "Race", "vehicles": [{"id": 7}]},
        {"type": "TelemetryUpdate", "fuel": 42},
        {"type": "AllDriversUpdate", "session_type": "Race", "drivers": []},
        {"type": "SessionInfo", "track_name": "Paul Ricard - ELMS"},
    ]
    async def source():
        for message in messages:
            yield json.dumps(message)

    with client.websocket_connect("/ws/ingest") as ws:
        hello(ws)
        assert ws.receive_json()["type"] == "agent_welcome"
        uplink = AsyncMock()
        uplink.send.side_effect = ws.send_text
        asyncio.run(stintara_agent.forward(source(), uplink))
        assert uplink.send.await_count == 6
        # FIFO barrier: only this deliberately invalid protocol closes the socket.
        frame(ws, protocol="invalid-test-protocol")
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1008
    assert [call.args[2] for call in handler.await_args_list] == [
        "connection", "session_info", "scoring", "telemetry", "lap_snapshots", "session_info"]
    assert handler.await_args_list[1].args[3]["track_name"] == "Paul Ricard - ELMS"
    assert handler.await_args_list[-1].args[3]["track_name"] == "Paul Ricard - ELMS"
    logs = [json.loads(r.message) for r in caplog.records if r.name == "stintara.ingest"]
    assert len(logs) == 1 and logs[0]["error_class"] == "ValueError"


@pytest.mark.parametrize("kind,payload", [
    ("telemetry", {"tires": [{"remaining": "test-secret"}]}),
    ("scoring", {"session_type": "Practice", "vehicles": [{"id": "test-secret"}]}),
    ("lap_snapshots", {"session_type": "Practice", "drivers": [{"id": "test-secret"}]}),
])
def test_invalid_provider_payload_is_dropped_then_valid_frame_processed(ingest_client, caplog, kind, payload):
    client, handler = ingest_client
    with client.websocket_connect("/ws/ingest") as ws:
        hello(ws)
        assert ws.receive_json()["type"] == "agent_welcome"
        frame(ws, kind, payload)
        frame(ws)
        frame(ws, protocol="bad-protocol-test-secret")
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1008
    assert handler.await_count == 1
    assert handler.call_args.args[2:] == ("telemetry", main.PAYLOADS["telemetry"](fuel=42).model_dump())
    logs = [json.loads(r.message) for r in caplog.records if r.name == "stintara.ingest"]
    assert logs[0]["agent_id"] == "test-agent" and logs[0]["message_type"] == kind
    assert logs[0]["error_class"] == "ValidationError" and logs[0]["validation"]
    assert logs[0]["close_code"] is None and logs[-1]["close_code"] == 1008
    assert "test-secret" not in caplog.text


def test_invalid_token_and_duplicate_connection_still_rejected(ingest_client, caplog):
    client, _ = ingest_client
    with client.websocket_connect("/ws/ingest") as ws:
        hello(ws, "invalid-secret")
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1008
    with client.websocket_connect("/ws/ingest") as first:
        hello(first)
        first.receive_json()
        with client.websocket_connect("/ws/ingest") as duplicate:
            hello(duplicate)
            with pytest.raises(WebSocketDisconnect) as exc:
                duplicate.receive_json()
            assert exc.value.reason == "agent already connected"
        assert "test-agent" in main.active_agents
    assert "invalid-secret" not in caplog.text
    assert not main.active_agents
    with client.websocket_connect("/ws/ingest") as reconnected:
        hello(reconnected)
        assert reconnected.receive_json()["type"] == "agent_welcome"


def test_invalid_payload_budget_cannot_be_reset_by_valid_frames(ingest_client):
    client, handler = ingest_client
    with client.websocket_connect("/ws/ingest") as ws:
        hello(ws)
        ws.receive_json()
        for _ in range(main.MAX_INVALID_PAYLOADS):
            frame(ws)
            frame(ws, payload={"tires": [{"remaining": 9}]})
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1008 and exc.value.reason == "too many invalid payloads"
    assert handler.await_count == main.MAX_INVALID_PAYLOADS


@pytest.mark.parametrize("message", [
    {"protocol": "stintara-agent-v1", "message_type": "unknown", "payload": {}},
    {"protocol": "stintara-agent-v1", "message_type": "telemetry", "payload": []},
    {"protocol": "stintara-agent-v1", "message_type": "connection", "payload": {}},
    [],
])
def test_fundamental_protocol_errors_remain_fatal(ingest_client, message):
    client, handler = ingest_client
    with client.websocket_connect("/ws/ingest") as ws:
        hello(ws)
        ws.receive_json()
        ws.send_json(message)
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1008
    handler.assert_not_awaited()


def test_messages_before_hello_are_rejected(ingest_client):
    client, handler = ingest_client
    with client.websocket_connect("/ws/ingest") as ws:
        frame(ws)
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1008
    handler.assert_not_awaited()


def test_ctrl_c_is_clean_but_unexpected_errors_remain_visible(monkeypatch, capsys):
    monkeypatch.setattr(stintara_agent, "load_config", lambda path: AgentConfig("ws://localhost", "test-secret"))
    def interrupted(coro):
        coro.close()
        raise KeyboardInterrupt
    monkeypatch.setattr(stintara_agent.asyncio, "run", interrupted)
    stintara_agent.cli([])
    assert capsys.readouterr().out == "Stintara agent stopped.\n"

    def broken(coro):
        coro.close()
        raise RuntimeError("unexpected")
    monkeypatch.setattr(stintara_agent.asyncio, "run", broken)
    with pytest.raises(RuntimeError):
        stintara_agent.cli([])


@pytest.mark.parametrize("code,reason", [(1000, ""), (1008, "invalid Stintara message"), (1008, "test-secret")])
async def test_agent_close_diagnostics(monkeypatch, capsys, code, reason):
    async def uplink(socket):
        await socket.recv()
        await socket.send(json.dumps({"type": "agent_welcome", "driver_slug": "driver"}))
        await socket.close(code=code, reason=reason)
    async with websockets.serve(uplink, "127.0.0.1", 0) as server:
        config = AgentConfig(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", "test-secret", "ws://127.0.0.1:1")
        await asyncio.wait_for(stintara_agent.run_once(config), 3)
    output = capsys.readouterr().out
    assert f"code={code}" in output and "outcome=" in output
    assert "test-secret" not in output


async def test_real_transport_abort_allows_same_agent_to_reconnect(monkeypatch, unused_tcp_port):
    main.active_agents.clear()
    monkeypatch.setattr(main, "authenticate_agent", AsyncMock(return_value={"id": "transport-agent", "slug": "driver"}))
    handler = AsyncMock()
    monkeypatch.setattr(main, "handle_message", handler)
    server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=unused_tcp_port,
                                         lifespan="off", log_level="error"))
    serving = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(5):
            while not server.started:
                await asyncio.sleep(.01)
        url = f"ws://127.0.0.1:{unused_tcp_port}/ws/ingest"
        greeting = json.dumps({"type": "agent_hello", "protocol": "stintara-agent-v1",
                               "token": "test-secret", "simulator": "lmu"})
        async with websockets.connect(url, proxy=None) as socket:
            await socket.send(greeting)
            assert json.loads(await socket.recv())["type"] == "agent_welcome"
            socket.transport.abort()
        async with asyncio.timeout(5):
            while main.active_agents:
                await asyncio.sleep(.01)
        async with websockets.connect(url, proxy=None) as socket:
            await socket.send(greeting)
            assert json.loads(await socket.recv())["type"] == "agent_welcome"
            await socket.send(json.dumps({"protocol": "stintara-agent-v1", "message_type": "telemetry", "payload": {"fuel": 42}}))
            async with asyncio.timeout(5):
                while not handler.await_count:
                    await asyncio.sleep(.01)
            assert handler.call_args.args[3]["fuel"] == 42
    finally:
        server.should_exit = True
        await serving
        main.active_agents.clear()


async def test_agent_announces_retry_and_does_not_retry_cancellation(monkeypatch, capsys):
    monkeypatch.setenv("STINTARA_AGENT_TOKEN", "test-secret")
    monkeypatch.setattr(stintara_agent, "run_once", AsyncMock(return_value=None))

    async def cancel_sleep(delay):
        assert delay == 3
        raise asyncio.CancelledError
    monkeypatch.setattr(stintara_agent.asyncio, "sleep", cancel_sleep)
    with pytest.raises(asyncio.CancelledError):
        await stintara_agent.main(AgentConfig("ws://localhost", "test-secret"))
    stintara_agent.run_once.assert_awaited_once()
    assert "Retrying Stintara connection in 3s" in capsys.readouterr().out


def test_bad_handshake_validation_never_logs_token(ingest_client, caplog):
    client, _ = ingest_client
    with client.websocket_connect("/ws/ingest") as ws:
        ws.send_json({"type": "agent_hello", "protocol": "test-secret", "token": "test-secret", "simulator": "lmu"})
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    assert "test-secret" not in caplog.text
    assert '"ValidationError"' in caplog.text


async def test_close_diagnostic_when_forwarder_finishes_before_close_waiter(monkeypatch, capsys):
    class Uplink:
        close_code = 1008
        close_reason = "invalid Stintara message"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def send(self, raw):
            pass

        async def recv(self):
            return json.dumps({"type": "agent_welcome", "driver_slug": "driver"})

        async def wait_closed(self):
            await asyncio.Event().wait()

    monkeypatch.setattr(stintara_agent.websockets, "connect", lambda *args, **kwargs: Uplink())
    monkeypatch.setattr(stintara_agent, "bridge", AsyncMock(return_value=None))
    config = AgentConfig("ws://localhost", "test-secret")
    await asyncio.wait_for(stintara_agent.run_once(config), 3)
    assert "code=1008" in capsys.readouterr().out


async def test_bootstrap_and_authentication(db, monkeypatch):
    async def existing_pool():
        return db
    monkeypatch.setattr(main, "create_db_pool", existing_pool)
    monkeypatch.setattr(main, "POSTGRES_PASSWORD", "test-only-local-password")
    monkeypatch.setenv("BOOTSTRAP_AGENT_TOKEN", "test-only-local-token")
    await main.bootstrap()
    assert await main.authenticate_agent("wrong") is None
    assert await main.authenticate_agent("test-only-local-token") is not None
    await main.bootstrap()
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM drivers") == 1
        assert await conn.fetchval("SELECT count(*) FROM agents") == 1


async def test_missing_or_placeholder_secrets_fail_fast(monkeypatch):
    for token in ("", "change-this-agent-token", "replace-with-token"):
        monkeypatch.setenv("BOOTSTRAP_AGENT_TOKEN", token)
        with pytest.raises(ValueError, match="BOOTSTRAP_AGENT_TOKEN"):
            await main.bootstrap()


async def test_pitwall_agent_server_database_api(db, monkeypatch, unused_tcp_port):
    await make_agent(db)
    frames = asyncio.Queue()

    async def source(socket):
        while socket.state.name != "CLOSED":
            try:
                frame = await asyncio.wait_for(frames.get(), .1)
                await socket.send(json.dumps(frame))
            except TimeoutError:
                continue
            except ConnectionClosed:
                return

    async with websockets.serve(source, "127.0.0.1", 0) as pitwall:
        pitwall_port = pitwall.sockets[0].getsockname()[1]
        config = AgentConfig(f"ws://127.0.0.1:{unused_tcp_port}/ws/ingest", "test-token-one",
                             f"ws://127.0.0.1:{pitwall_port}?format=json")
        server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=unused_tcp_port, lifespan="off", log_level="error"))
        serving = asyncio.create_task(server.serve())
        forwarding = None
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    await asyncio.sleep(.01)
            async with websockets.connect(config.server_ws, proxy=None) as ws:
                await ws.send(json.dumps({"type": "agent_hello", "protocol": "stintara-agent-v1", "token": "wrong", "simulator": "lmu"}))
                with pytest.raises(ConnectionClosed) as exc:
                    await ws.recv()
                assert exc.value.rcvd.code == 1008
            forwarding = asyncio.create_task(stintara_agent.run_once(config))
            for frame in [
                {"type": "AllDriversUpdate", "session_type": "Race", "session_time": 100,
                 "drivers": [{"id": 7, "total_laps": 1, "last_lap_time": 90., "has_telemetry": True,
                              "last_sector1": 30, "last_sector2": 60, "last_sector3": 30,
                              "wheels": [{"wear": 1., "surface_temp": [80, 90, 100]}], "dent_severity": [0, 1]}]},
                {"type": "ConnectionStatus", "game_connected": True, "telemetry_valid": True},
                {"type": "SessionInfo", "track_name": "Imola", "track_length": 4909},
                {"type": "ScoringUpdate", "session_type": "Race", "session_time": 100,
                 "player_vehicle_id": 7, "vehicles": [{"id": 7, "driver_name": "<b>Driver</b>", "car_name": "Porsche", "position": 1}]},
                {"type": "TelemetryUpdate", "current_et": 100, "fuel": 42,
                 "tires": [{"wear": 1., "pressure": 175, "temp_inner": 80, "temp_mid": 90, "temp_outer": 100, "brake_temp": 450}]},
            ]:
                await frames.put(frame)
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{unused_tcp_port}", trust_env=False) as client:
                async with asyncio.timeout(10):
                    while True:
                        data = (await client.get("/api/drivers/one/live")).json()
                        if data.get("strategy"):
                            break
                        await asyncio.sleep(.02)
                assert data["strategy"]["fuel_l"] == 42
                assert data["strategy"]["tyre_remaining_fl"] == 1
                assert data["strategy"]["tyre_brake_temp_fl_c"] == 450
                assert data["participants"][0]["display_name"] == "<b>Driver</b>"
                assert (await client.get("/api/live")).json()[0]["car_name"] == "Porsche"
                assert (await client.get("/health")).status_code == 200
                assert (await client.get("/")).status_code == 200
                assert (await client.get("/drivers/one/live")).status_code == 200
                assert (await client.get("/api/drivers/missing/live")).status_code == 404
                async with db.acquire() as conn:
                    lap = await conn.fetchrow("SELECT * FROM laps")
                    assert lap["sector2"] == 30 and lap["tyre_remaining_fl"] == 1
                await frames.put({"type": "ConnectionStatus", "game_connected": False})
                async with asyncio.timeout(10):
                    while (await client.get("/api/live")).json():
                        await asyncio.sleep(.02)
                assert not (await client.get("/api/drivers/one/live")).json()["live"]
        finally:
            if forwarding:
                forwarding.cancel()
                with suppress(asyncio.CancelledError):
                    await forwarding
            server.should_exit = True
            await serving


async def test_uplink_close_is_detected_while_pitwall_is_unavailable(monkeypatch):
    async def uplink(socket):
        await socket.recv()
        await socket.send(json.dumps({"type": "agent_welcome", "driver_slug": "one"}))
        await socket.close()

    async with websockets.serve(uplink, "127.0.0.1", 0) as server:
        config = AgentConfig(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", "test-secret", "ws://127.0.0.1:1")
        await asyncio.wait_for(stintara_agent.run_once(config), timeout=3)


async def test_bridge_reconnects_after_clean_close(monkeypatch):
    connections = 0
    received = asyncio.Event()

    async def source(socket):
        nonlocal connections
        connections += 1
        await socket.send(json.dumps({"type": "SessionInfo", "track_name": "Imola"}))
        await socket.close()

    class Uplink:
        async def send(self, raw):
            if connections >= 2:
                received.set()

    async with websockets.serve(source, "127.0.0.1", 0) as server:
        config = AgentConfig("ws://localhost", "test-secret", f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
        task = asyncio.create_task(stintara_agent.bridge(Uplink(), config))
        try:
            await asyncio.wait_for(received.wait(), timeout=5)
            assert connections >= 2
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
