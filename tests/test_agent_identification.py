"""Agent identification over the real ingest transport: declared vs. received source_epoch, mixed versions."""
import asyncio
import json
from contextlib import suppress
from unittest.mock import AsyncMock

import pytest
import websockets
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agent import stintara_agent
from agent.config import AgentConfig
from server.app import main

SHA40 = "0123456789abcdef0123456789abcdef01234567"
EPOCH = "pitwall-gen_0001"


@pytest.fixture
def ingest_client(monkeypatch):
    main.active_agents.clear()
    monkeypatch.setattr(main, "authenticate_agent", AsyncMock(
        side_effect=lambda token: {"id": "test-agent", "slug": "driver"} if token == "test-secret" else None))
    handler = AsyncMock()
    monkeypatch.setattr(main, "handle_message", handler)
    yield TestClient(main.app), handler
    main.active_agents.clear()


def legacy_hello(ws, token="test-secret"):
    ws.send_json({"type": "agent_hello", "protocol": "stintara-agent-v1", "token": token, "simulator": "lmu"})


def identified_hello(ws, agent=None, capabilities=("source_epoch",), token="test-secret"):
    message = {"type": "agent_hello", "protocol": "stintara-agent-v1", "token": token, "simulator": "lmu"}
    if agent is not False:
        message["agent"] = agent if agent is not None else {"version": "0.3.0", "channel": "release", "revision": SHA40}
    if capabilities is not None:
        message["capabilities"] = capabilities if isinstance(capabilities, str) else list(capabilities)
    ws.send_json(message)


def events(caplog, name):
    parsed = []
    for record in caplog.records:
        text = record.getMessage()
        if record.name == "stintara.agent" and text.startswith("{"):
            data = json.loads(text)
            if data.get("event") == name:
                parsed.append(data)
    return parsed


def levels(caplog, name):
    return [r.levelname for r in caplog.records if r.name == "stintara.agent" and f'"{name}"' in r.getMessage()]


def test_identified_agent_gets_an_unverified_compat_answer(ingest_client, caplog):
    caplog.set_level("INFO", logger="stintara.agent")
    client, _ = ingest_client
    with client.websocket_connect("/ws/ingest") as ws:
        identified_hello(ws)
        welcome = ws.receive_json()
    assert welcome["compat"] == {"status": "compatible", "limitations": [], "required": ["source_epoch"],
                                 "verified": False}
    (connected,) = events(caplog, "agent_connected")
    assert connected["agent_version"] == "0.3.0" and connected["capabilities"] == ["source_epoch"]
    assert connected["compat_status"] == "compatible" and connected["agent_revision"] == SHA40
    assert levels(caplog, "agent_connected") == ["INFO"]
    assert "test-secret" not in caplog.text


def test_legacy_agent_keeps_the_unchanged_welcome_and_is_flagged_for_the_operator(ingest_client, caplog):
    caplog.set_level("INFO", logger="stintara.agent")
    client, _ = ingest_client
    with client.websocket_connect("/ws/ingest") as ws:
        legacy_hello(ws)
        assert ws.receive_json() == {"type": "agent_welcome", "driver_slug": "driver", "version": "0.1.0"}
    (connected,) = events(caplog, "agent_connected")
    assert connected["declared"] is False and connected["compat_status"] == "unknown_legacy"
    assert levels(caplog, "agent_connected") == ["WARNING"]


def test_identified_agent_without_epoch_capability_is_reported_as_limited(ingest_client, caplog):
    caplog.set_level("INFO", logger="stintara.agent")
    client, _ = ingest_client
    with client.websocket_connect("/ws/ingest") as ws:
        identified_hello(ws, capabilities=None)
        answer = ws.receive_json()["compat"]
    assert answer["status"] == "limited" and answer["limitations"] == ["source_epoch_not_declared"]
    assert events(caplog, "agent_connected")[0]["limitations"] == ["source_epoch_not_declared"]
    assert levels(caplog, "agent_connected") == ["WARNING"]


@pytest.mark.parametrize("agent,capabilities", [
    ("0.3.0", ["source_epoch"]), ({"version": {"x": 1}}, ["source_epoch"]),
    ({"version": "0.3.0"}, "source_epoch"), (False, ["source_epoch"])])
def test_malformed_identification_never_breaks_the_connection_or_leaks(ingest_client, caplog, agent, capabilities):
    caplog.set_level("INFO", logger="stintara.agent")
    client, handler = ingest_client
    with client.websocket_connect("/ws/ingest") as ws:
        identified_hello(ws, agent=agent, capabilities=capabilities)
        answer = ws.receive_json().get("compat")
        for _ in range(2):
            ws.send_json({"protocol": "stintara-agent-v1", "message_type": "telemetry", "payload": {"fuel": 1}})
    assert answer["status"] == "limited" and "agent_info_invalid" in answer["limitations"]
    assert handler.await_count == 2
    assert "{'x': 1}" not in caplog.text and "'x': 1" not in caplog.text


def test_oversized_hello_is_refused(ingest_client):
    client, handler = ingest_client
    with client.websocket_connect("/ws/ingest") as ws:
        identified_hello(ws, capabilities=["source_epoch"] + ["x" * 31] * 700)
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == 1009 and not handler.await_count


def run_stream(client, hello_args, epochs):
    with client.websocket_connect("/ws/ingest") as ws:
        identified_hello(ws, **hello_args)
        ws.receive_json()
        for epoch in epochs:
            message = {"protocol": "stintara-agent-v1", "message_type": "telemetry", "payload": {"fuel": 42}}
            if epoch is not None:
                message["source_epoch"] = epoch
            ws.send_json(message)


def test_received_epoch_is_audited_separately_from_the_declaration(ingest_client, caplog):
    """Declaring source_epoch but sending none is flagged; a legacy hello that does send it is still verified."""
    caplog.set_level("INFO", logger="stintara.agent")
    client, handler = ingest_client
    run_stream(client, {}, [None] * 25)                                           # declares, never sends
    run_stream(client, {}, [EPOCH] * 25)                                          # declares and sends
    run_stream(client, {"agent": False, "capabilities": None}, [EPOCH] * 25)      # hello without identification
    run_stream(client, {}, ["bad"] + [EPOCH] * 24)                                # sometimes malformed
    assert [(e["verdict"], e["declared_source_epoch"]) for e in events(caplog, "agent_epoch_check")] == [
        ("not_received", True), ("verified", True), ("verified", False), ("inconsistent", True)]
    assert levels(caplog, "agent_epoch_check") == ["WARNING", "INFO", "INFO", "WARNING"]
    assert levels(caplog, "agent_disconnected") == ["INFO", "INFO", "INFO", "WARNING"]
    last = events(caplog, "agent_disconnected")[-1]
    assert (last["epoch_valid"], last["epoch_invalid"], last["epoch_missing"]) == (24, 1, 0)
    assert handler.await_count == 100        # the audit never drops or delays a message


def test_few_messages_produce_no_epoch_verdict(ingest_client, caplog):
    caplog.set_level("INFO", logger="stintara.agent")
    client, _ = ingest_client
    run_stream(client, {}, [None] * 5)
    assert not events(caplog, "agent_epoch_check")
    assert events(caplog, "agent_disconnected")[0]["epoch_missing"] == 5


def test_mixed_agent_versions_are_handled_per_connection_with_unchanged_message_handling(monkeypatch):
    main.active_agents.clear()
    tokens = {"old-token": {"id": "old-agent", "slug": "old"}, "new-token": {"id": "new-agent", "slug": "new"}}
    monkeypatch.setattr(main, "authenticate_agent", AsyncMock(side_effect=tokens.get))
    handler = AsyncMock()
    monkeypatch.setattr(main, "handle_message", handler)
    client = TestClient(main.app)
    with client.websocket_connect("/ws/ingest") as old, client.websocket_connect("/ws/ingest") as new:
        legacy_hello(old, "old-token")
        identified_hello(new, token="new-token")
        assert "compat" not in old.receive_json()
        assert new.receive_json()["compat"]["status"] == "compatible"
        for _ in range(2):
            old.send_json({"protocol": "stintara-agent-v1", "message_type": "telemetry", "payload": {"fuel": 42}})
            new.send_json({"protocol": "stintara-agent-v1", "message_type": "telemetry",
                           "payload": {"fuel": 42}, "source_epoch": EPOCH})
    calls = {}
    for call in handler.await_args_list:
        calls.setdefault(call.args[0]["id"], []).append((call.args[1], call.args[2], call.args[3]["fuel"], call.args[4]))
    assert calls == {"old-agent": [("lmu", "telemetry", 42, None)] * 2,
                     "new-agent": [("lmu", "telemetry", 42, EPOCH)] * 2}
    main.active_agents.clear()


async def test_agent_sends_identification_and_prints_the_server_answer(capsys):
    seen = {}

    async def uplink(socket):
        seen["hello"] = json.loads(await socket.recv())
        await socket.send(json.dumps({"type": "agent_welcome", "driver_slug": "driver", "compat": {
            "status": "limited", "limitations": ["source_epoch_not_declared"]}}))
        await socket.close()
    async with websockets.serve(uplink, "127.0.0.1", 0) as server:
        config = AgentConfig(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", "test-secret", "ws://127.0.0.1:1")
        await asyncio.wait_for(stintara_agent.run_once(config), 3)
    assert seen["hello"]["agent"] == {"version": "0.3.1", "channel": "dev", "revision": "unknown"}
    assert seen["hello"]["capabilities"] == ["source_epoch"] and seen["hello"]["token"] == "test-secret"
    output = capsys.readouterr().out
    assert "Server compatibility: limited" in output and "no source_epoch support declared" in output
    assert "test-secret" not in output


async def test_agent_reports_unknown_compatibility_for_an_older_server(capsys):
    async def uplink(socket):
        await socket.recv()
        await socket.send(json.dumps({"type": "agent_welcome", "driver_slug": "driver", "version": "0.1.0"}))
        await socket.close()
    async with websockets.serve(uplink, "127.0.0.1", 0) as server:
        config = AgentConfig(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", "test-secret", "ws://127.0.0.1:1")
        await asyncio.wait_for(stintara_agent.run_once(config), 3)
    assert "not reported by this server (unknown)" in capsys.readouterr().out


async def test_each_uplink_lifetime_gets_its_own_pitwall_epoch():
    """source_epoch follows the Pitwall socket, never the uplink: a new bridge opens a new Pitwall socket."""
    sent = []

    async def source(socket):
        await socket.send(json.dumps({"type": "ScoringUpdate", "session_type": "Race", "vehicles": []}))
        await asyncio.sleep(.5)

    class Uplink:
        async def send(self, raw):
            sent.append(json.loads(raw)["source_epoch"])
    async with websockets.serve(source, "127.0.0.1", 0) as server:
        config = AgentConfig("ws://localhost", "test-secret", f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
        for _ in range(2):
            task = asyncio.create_task(stintara_agent.bridge(Uplink(), config))
            await asyncio.sleep(.25)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
    assert len(sent) == 2 and sent[0] != sent[1]
