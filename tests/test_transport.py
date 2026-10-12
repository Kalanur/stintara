import asyncio
import json
from contextlib import suppress

import httpx
import pytest
import uvicorn
import websockets
from websockets.exceptions import ConnectionClosed

from agent import stintara_agent
from server.app import main
from tests.conftest import make_agent


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
        monkeypatch.setattr(stintara_agent, "PITWALL_WS", f"ws://127.0.0.1:{pitwall_port}?format=json")
        monkeypatch.setattr(stintara_agent, "STINTARA_WS", f"ws://127.0.0.1:{unused_tcp_port}/ws/ingest")
        server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=unused_tcp_port, lifespan="off", log_level="error"))
        serving = asyncio.create_task(server.serve())
        forwarding = None
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    await asyncio.sleep(.01)
            async with websockets.connect(stintara_agent.STINTARA_WS, proxy=None) as ws:
                await ws.send(json.dumps({"type": "agent_hello", "protocol": "stintara-agent-v1", "token": "wrong", "simulator": "lmu"}))
                with pytest.raises(ConnectionClosed) as exc:
                    await ws.recv()
                assert exc.value.rcvd.code == 1008
            forwarding = asyncio.create_task(stintara_agent.run_once("test-token-one"))
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
        monkeypatch.setattr(stintara_agent, "STINTARA_WS", f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
        monkeypatch.setattr(stintara_agent, "PITWALL_WS", "ws://127.0.0.1:1")
        await asyncio.wait_for(stintara_agent.run_once("test-token"), timeout=3)


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
        monkeypatch.setattr(stintara_agent, "PITWALL_WS", f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
        task = asyncio.create_task(stintara_agent.bridge(Uplink()))
        try:
            await asyncio.wait_for(received.wait(), timeout=5)
            assert connections >= 2
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
