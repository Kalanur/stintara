import asyncio
import io
import json
from unittest.mock import AsyncMock

import pytest
import websockets

from agent.config import AgentConfig
from tools.replay import common
from tools.replay.capture import capture, record
from tools.replay.common import run_cli, send_records, validate_bind, validate_timing
from tools.replay.compile import compile_capture
from tools.replay.format import dumps, header, load
from tools.replay.pitwall_server import handler
from tools.replay.scenarios import scenario
from tools.replay.stintara_replay import replay


@pytest.mark.parametrize("speed,immediate,delays", [(1, False, [2]*8), (5, False, [.4]*8), (1, True, [])])
async def test_timing_order_without_wall_clock_sleeps(speed, immediate, delays):
    _, rows = scenario()
    socket, sleep = AsyncMock(), AsyncMock()
    await send_records(socket, rows, speed, immediate, sleep)
    assert [c.args[0] for c in sleep.await_args_list] == delays
    assert [json.loads(c.args[0]) for c in socket.send.await_args_list] == [r["message"] for r in rows]


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_invalid_speed(value):
    with pytest.raises(ValueError):
        validate_timing(value)


def test_bind_safety():
    validate_bind("127.0.0.1")
    validate_bind("::1")
    for host in ("0.0.0.0", "localhost", "example.invalid"):
        with pytest.raises(ValueError):
            validate_bind(host)
        validate_bind(host, True)


async def test_loopback_restarts_for_each_client_and_recorder_roundtrip(tmp_path, monkeypatch, capsys):
    metadata, rows = scenario()
    monkeypatch.setattr(common, "PRIVATE_ROOT", tmp_path)
    async with websockets.serve(handler(rows, immediate=True), "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        for _ in range(2):
            async with websockets.connect(url, proxy=None) as client:
                received = [json.loads(raw) async for raw in client]
                assert received == [r["message"] for r in rows]
        await capture(url, tmp_path/"raw.jsonl")
    captured_header, captured = load(tmp_path/"raw.jsonl")
    assert captured_header == header("real")
    assert [r["message"] for r in captured] == received
    assert [r["t"] for r in captured] == sorted(r["t"] for r in captured)
    output = capsys.readouterr().out
    assert "Driver Alpha" not in output and url not in output
    assert "Messages: 29" in output
    assert len(dumps(metadata, rows)) > 0


async def test_recorder_clock_binary_frames_and_safe_counters(capsys):
    async def frames():
        yield b"ignored"
        yield '{"type":"Unknown Private Marker","driver_name":"Private Driver One"}'
        yield '{"type":"ConnectionStatus","game_connected":true}'
    clock = iter([10, 10.5, 11, 12])
    stream = io.StringIO()
    await record(frames(), stream, lambda: next(clock))
    assert [json.loads(line)["t"] for line in stream.getvalue().splitlines()] == [.5, 1]
    out = capsys.readouterr().out
    assert "Private" not in out and "duration: 2.000s" in out


@pytest.mark.parametrize("speed,immediate,expected", [(1, False, 8), (10, False, 8), (1, True, 0)])
async def test_protocol_hello_timing_hold_and_no_payload_output(speed, immediate, expected, capsys):
    _, rows, _ = compile_capture(*scenario())
    socket, sleep = AsyncMock(), AsyncMock()
    socket.recv.return_value = '{"type":"agent_welcome","driver_slug":"Private Driver Marker"}'
    config = AgentConfig("ws://localhost", "test-only-private-token")
    await replay(socket, config, rows, speed, immediate, hold_open=2, sleep=sleep)
    sent = [json.loads(c.args[0]) for c in socket.send.await_args_list]
    assert sent[0] == dict(type="agent_hello", protocol="stintara-agent-v1", token=config.token, simulator="lmu")
    assert sent[1:] == [r["message"] for r in rows]
    assert sleep.await_count == expected+1
    assert [c.args[0] for c in sleep.await_args_list] == ([] if immediate else [2/speed]*8)+[2]
    assert not capsys.readouterr().out
    assert config.token not in dumps(header(format="stintara-protocol-replay"), rows)


@pytest.mark.parametrize("welcome", ['{"type":"private-marker","token":"private-marker"}', 'private-marker', '[]'])
async def test_invalid_welcome_never_sends_replay(welcome):
    socket = AsyncMock()
    socket.recv.return_value = welcome
    with pytest.raises(ValueError) as exc:
        await replay(socket, AgentConfig("ws://localhost", "test-only-token"), [])
    assert "private-marker" not in str(exc.value)
    assert socket.send.await_count == 1


def test_cli_errors_do_not_echo_exception_values(capsys):
    def failed():
        raise RuntimeError("private-token-payload-url")
    with pytest.raises(SystemExit) as exc:
        run_cli(failed)
    assert "private-token" not in str(exc.value)+str(capsys.readouterr())


async def test_compiled_replay_real_ingest_database_and_fuel(db, unused_tcp_port):
    import httpx
    import uvicorn

    from server.app import main
    from tests.conftest import make_agent

    await make_agent(db, display_name="Synthetic Driver")
    server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=unused_tcp_port,
                                          lifespan="off", log_level="error"))
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(.01)
        config = AgentConfig(f"ws://127.0.0.1:{unused_tcp_port}/ws/ingest", "test-token-one")
        # Exercise handshake + actual compiled messages; no production-clock bypass.
        _, rows, _ = compile_capture(*scenario())
        async with websockets.connect(config.server_ws, proxy=None) as socket:
            await replay(socket, config, rows, immediate=True)
            async with asyncio.timeout(10):
                while True:
                    async with db.acquire() as conn:
                        if await conn.fetchval("SELECT count(*) FROM laps") == 9:
                            break
                    await asyncio.sleep(.01)
            async with db.acquire() as conn:
                session = await conn.fetchval("SELECT id FROM sessions")
                participant = await conn.fetchval("SELECT id FROM session_participants WHERE driver_id IS NOT NULL")
                assert await conn.fetchval("SELECT count(*) FROM session_participants") == 6
                # Immediate replay still obeys server-side 1 Hz persistence.
                assert await conn.fetchval("SELECT count(*) FROM strategy_samples") >= 1
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{unused_tcp_port}", trust_env=False) as client:
                data = (await client.get(f"/api/sessions/{session}/live")).json()
                assert len(data["participants"]) == 6
                fuel = (await client.get(f"/api/sessions/{session}/participants/{participant}/fuel")).json()
                assert fuel["estimate"]["consumption_l_per_lap"] == 3
                assert fuel["estimate"]["sample_count"] == 5
                assert fuel["current"]["fuel_l"] is not None
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)


async def test_protocol_file_client_loopback(tmp_path, capsys):
    from tools.replay.format import write
    from tools.replay.stintara_replay import replay_file

    metadata, rows, _ = compile_capture(*scenario())
    path = tmp_path / "compiled.jsonl"
    write(path, metadata, rows)
    received, done = [], asyncio.Event()
    async def ingest(socket):
        received.append(json.loads(await socket.recv()))
        await socket.send('{"type":"agent_welcome"}')
        async for raw in socket:
            received.append(json.loads(raw))
        done.set()
    async with websockets.serve(ingest, "127.0.0.1", 0) as server:
        config = AgentConfig(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", "test-only-replay-token")
        await replay_file(path, config, immediate=True)
        await asyncio.wait_for(done.wait(), 3)
    assert received[0]["token"] == config.token
    assert received[1:] == [r["message"] for r in rows]
    assert config.token not in path.read_text()+capsys.readouterr().out
