from __future__ import annotations

import asyncio
import json
import os
from time import monotonic

import websockets
from websockets.exceptions import WebSocketException

try:
    from .pitwall import normalize
except ImportError:  # python agent/stintara_agent.py
    from pitwall import normalize

PITWALL_WS = os.environ.get("PITWALL_WS", "ws://127.0.0.1:9000?format=json")
STINTARA_WS = os.environ.get("STINTARA_WS", "ws://127.0.0.1:8080/ws/ingest")


async def forward(pitwall, uplink):
    last_sent = {}
    async for raw in pitwall:
        if not isinstance(raw, str):
            continue
        try:
            normalized = normalize(json.loads(raw))
        except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
            continue
        if normalized is None:
            continue
        kind, payload = normalized
        now = monotonic()
        interval = {"scoring": 0.2, "telemetry": 1.0}.get(kind, 0.0)
        if kind in last_sent and now - last_sent[kind] < interval:
            continue
        await uplink.send(json.dumps({
            "protocol": "stintara-agent-v1", "message_type": kind,
            "payload": payload,
        }, allow_nan=False, separators=(",", ":")))
        last_sent[kind] = now


async def bridge(uplink):
    while True:
        try:
            async with websockets.connect(PITWALL_WS, max_size=32 * 1024 * 1024, proxy=None) as pitwall:
                print("Connected to LMU Pitwall")
                await forward(pitwall, uplink)
        except (OSError, WebSocketException, TimeoutError):
            print("Pitwall connection unavailable; retrying in 2s")
        await asyncio.sleep(2)  # also back off after a clean close


async def run_once(token):
    async with websockets.connect(STINTARA_WS, max_size=1024 * 1024) as uplink:
        await uplink.send(json.dumps({
            "type": "agent_hello", "protocol": "stintara-agent-v1",
            "token": token, "simulator": "lmu",
        }))
        welcome = json.loads(await asyncio.wait_for(uplink.recv(), timeout=10))
        if not isinstance(welcome, dict) or welcome.get("type") != "agent_welcome":
            raise ValueError("Stintara handshake rejected")
        print(f"Connected to Stintara as {welcome.get('driver_slug')}")
        forwarding = asyncio.create_task(bridge(uplink))
        closed = asyncio.create_task(uplink.wait_closed())
        try:
            done, _ = await asyncio.wait((forwarding, closed), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            forwarding.cancel()
            closed.cancel()
            await asyncio.gather(forwarding, closed, return_exceptions=True)


async def main():
    token = os.environ.get("STINTARA_AGENT_TOKEN", "").strip()
    if not token or token.startswith(("change-", "replace-")):
        raise SystemExit("Set STINTARA_AGENT_TOKEN to the server's configured agent token")
    while True:
        try:
            await run_once(token)
        except (OSError, WebSocketException, TimeoutError, ValueError):
            print("Stintara connection unavailable or handshake rejected; retrying in 3s")
        await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(main())
