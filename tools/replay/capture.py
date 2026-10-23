"""Independent, private Pitwall recorder. No Stintara connection."""
import asyncio
from collections import Counter
from pathlib import Path
from time import monotonic

import websockets

from agent.config import DEFAULT_PITWALL_WS, validate_ws_url
from agent.pitwall import MESSAGE_TYPES

from .common import Parser, output_location, run_cli
from .format import decode, encode, header, validate_record


async def record(socket, stream, clock=monotonic):
    started, previous, count = clock(), 0, 0
    types = Counter()
    try:
        async for raw in socket:
            if not isinstance(raw, str):
                continue
            message = decode(raw)
            row = validate_record(dict(record_type="message", t=clock()-started, message=message), previous)
            stream.write(encode(row))
            stream.flush()
            previous = row["t"]
            count += 1
            kind = message.get("type")
            if isinstance(kind, str) and kind in MESSAGE_TYPES:
                types[kind] += 1
    finally:
        print(f"Messages: {count}; duration: {max(0, clock()-started):.3f}s; known types: {dict(types)}")


async def capture(url, output):
    validate_ws_url(url, "pitwall_ws")
    path = output_location(output, "real")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(encode(header("real")))
        stream.flush()
        async with websockets.connect(url, max_size=32*1024*1024, proxy=None) as socket:
            await record(socket, stream)


def cli():
    parser = Parser(description="Record private Pitwall JSON; output must be under data/.")
    parser.add_argument("--output", required=True)
    parser.add_argument("--pitwall-ws", default=DEFAULT_PITWALL_WS)
    args = parser.parse_args()
    run_cli(lambda: asyncio.run(capture(args.pitwall_ws, args.output)))


if __name__ == "__main__":
    cli()
