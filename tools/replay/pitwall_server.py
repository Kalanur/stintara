"""Each connection replays from the beginning, then closes cleanly."""
import asyncio

import websockets

from .common import Parser, run_cli, send_records, timing_options, validate_bind, validate_timing
from .format import WIRE, load


def handler(records, speed=1, immediate=False, sleep=asyncio.sleep):
    async def replay(socket):
        try:
            await send_records(socket, records, speed, immediate, sleep)
            await socket.close()
        except websockets.exceptions.ConnectionClosed:
            pass
    return replay


async def serve(path, host="127.0.0.1", port=9100, speed=1, immediate=False, allow_remote=False):
    validate_bind(host, allow_remote)
    validate_timing(speed)
    _, records = load(path, WIRE)
    async with websockets.serve(handler(records, speed, immediate), host, port):
        print("Replay server ready.")
        await asyncio.Future()


def cli():
    parser = Parser(description="Local fake Pitwall; use speed 1 for agent sampling fidelity.")
    parser.add_argument("capture")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument("--allow-remote-bind", action="store_true")
    timing_options(parser)
    args = parser.parse_args()
    run_cli(lambda: asyncio.run(serve(args.capture, args.host, args.port, args.speed,
                                    args.immediate, args.allow_remote_bind)))


if __name__ == "__main__":
    cli()
