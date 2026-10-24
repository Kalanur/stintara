"""Replay creates real target database rows; use a dedicated synthetic test agent."""
import asyncio
import json

import websockets

from agent.config import load_config

from .common import Parser, run_cli, send_records, timing_options, validate_timing
from .format import PROTOCOL, decode, load


async def replay(socket, config, records, speed=1, immediate=False, hold_open=0, sleep=asyncio.sleep):
    validate_timing(speed, hold_open)
    await socket.send(json.dumps(dict(type="agent_hello", protocol="stintara-agent-v1",
                                     token=config.token, simulator="lmu")))
    welcome = decode(await asyncio.wait_for(socket.recv(), 10))
    if not isinstance(welcome, dict) or welcome.get("type") != "agent_welcome":
        raise ValueError("Handshake rejected")
    await send_records(socket, records, speed, immediate, sleep)
    if hold_open:
        await sleep(hold_open)


async def replay_file(path, config, speed=1, immediate=False, hold_open=0):
    validate_timing(speed, hold_open)
    _, records = load(path, PROTOCOL)
    async with websockets.connect(config.server_ws, max_size=1024*1024, proxy=None) as socket:
        await replay(socket, config, records, speed, immediate, hold_open)
    print(f"Sent messages: {len(records)}")


def cli():
    parser = Parser(description="Write replay data into a test/dev Stintara database using a dedicated synthetic agent.")
    parser.add_argument("file")
    parser.add_argument("--config")
    parser.add_argument("--hold-open", type=float, default=0)
    timing_options(parser)
    args = parser.parse_args()
    run_cli(lambda: asyncio.run(replay_file(args.file, load_config(args.config), args.speed,
                                          args.immediate, args.hold_open)))


if __name__ == "__main__":
    cli()
