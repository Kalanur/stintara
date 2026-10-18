from __future__ import annotations

import argparse
import asyncio
import json
from time import monotonic

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

try:
    from .config import load_config
    from .pitwall import normalize
except ImportError:  # python agent/stintara_agent.py
    from config import load_config
    from pitwall import normalize


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


async def bridge(uplink, config):
    while True:
        try:
            async with websockets.connect(config.pitwall_ws, max_size=32 * 1024 * 1024, proxy=None) as pitwall:
                print("Connected to LMU Pitwall")
                await forward(pitwall, uplink)
        except (OSError, WebSocketException, TimeoutError):
            if getattr(uplink, "close_code", None) is not None:
                return  # run_once reports the uplink close, not a Pitwall failure
            print("Pitwall connection unavailable; retrying in 2s")
        await asyncio.sleep(2)  # also back off after a clean close


def report_close(code, reason, outcome):
    # Only server-defined reasons are safe to echo. Never print arbitrary peer
    # strings, exception text, tokens or payloads.
    safe_reasons = {"invalid token", "agent already connected", "message too large",
                    "invalid Stintara message", "too many invalid payloads", "invalid protocol"}
    reason = reason if reason in safe_reasons else ("[omitted]" if reason else "")
    print(f'Stintara connection closed: code={code} reason="{reason}" outcome={outcome}')


async def run_once(config):
    try:
        await connected_run(config)
    except ConnectionClosed as exc:
        report_close(exc.rcvd.code if exc.rcvd else 1006, exc.rcvd.reason if exc.rcvd else "", "exception")
        raise


async def connected_run(config):
    async with websockets.connect(config.server_ws, max_size=1024 * 1024) as uplink:
        await uplink.send(json.dumps({
            "type": "agent_hello", "protocol": "stintara-agent-v1",
            "token": config.token, "simulator": "lmu",
        }))
        welcome = json.loads(await asyncio.wait_for(uplink.recv(), timeout=10))
        if not isinstance(welcome, dict) or welcome.get("type") != "agent_welcome":
            raise ValueError("Stintara handshake rejected")
        print(f"Connected to Stintara as {welcome.get('driver_slug')}")
        forwarding = asyncio.create_task(bridge(uplink, config))
        closed = asyncio.create_task(uplink.wait_closed())
        try:
            done, _ = await asyncio.wait((forwarding, closed), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            if closed in done or uplink.close_code is not None:
                report_close(uplink.close_code, uplink.close_reason,
                             "clean" if uplink.close_code in (1000, 1001) else "abnormal")
        finally:
            forwarding.cancel()
            closed.cancel()
            await asyncio.gather(forwarding, closed, return_exceptions=True)


async def main(config):
    while True:
        try:
            await run_once(config)
        except (OSError, WebSocketException, TimeoutError, ValueError) as exc:
            print(f"Stintara connection failed: error_class={type(exc).__name__}")
        print("Retrying Stintara connection in 3s")
        await asyncio.sleep(3)


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "Invalid arguments; use --help.\n")


def cli(argv=None):
    parser = SafeArgumentParser(description="Stintara test agent; keep your JSON configuration private.")
    parser.add_argument("--config", metavar="PATH", help="JSON config (default: beside the executable or agent script)")
    parser.add_argument("--version", action="version", version="Stintara Agent 0.1.0")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except ValueError as exc:
        parser.exit(2, f"Configuration error: {exc}\n")
    try:
        asyncio.run(main(config))
    except KeyboardInterrupt:
        print("Stintara agent stopped.")


if __name__ == "__main__":
    cli()
