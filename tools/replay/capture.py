"""Independent, private Pitwall recorder. No Stintara connection.

RAW here means Pitwall WebSocket JSON before Stintara normalisation, not LMU shared memory.
"""
import asyncio
import hashlib
import json
import os
import tempfile
from collections import Counter
from pathlib import Path
from time import monotonic

import websockets

from agent.config import DEFAULT_PITWALL_WS, validate_ws_url
from agent.pitwall import MESSAGE_TYPES

from . import common
from .common import Parser, output_location, run_cli
from .format import decode, encode, header, validate_record
from .naming import DEFAULT_DIRECTORY, create_unique, metadata_path, utc_now

STATUS_INTERVAL = 10.0
# Regular ends vs. captures that may be truncated or damaged.
REGULAR = {"stopped_by_user": "stopped by Ctrl+C (regular end)",
           "pitwall_closed": "Pitwall closed the connection (regular end)"}
INCOMPLETE = {"connection_lost": "connection to Pitwall was lost; capture may be truncated",
              "invalid_data": "a message could not be recorded; capture is truncated",
              "error": "recording failed; capture may be truncated"}


async def record(socket, stream, clock=monotonic, stats=None):
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
            if stats is not None:
                stats["messages"] = count
            kind = message.get("type")
            if isinstance(kind, str) and kind in MESSAGE_TYPES:
                types[kind] += 1
    finally:
        print(f"Messages: {count}; duration: {max(0, clock()-started):.3f}s; known types: {dict(types)}")


def file_digest(path):
    """Size and SHA-256 computed in fixed-size chunks; the file is never loaded whole."""
    digest, size = hashlib.sha256(), 0
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def write_metadata(path, document):
    """Replace the sidecar atomically via a unique, exclusively created temporary file.

    mkstemp uses a random name with O_EXCL, so a pre-planted file or symlink is never followed or written.
    os.replace swaps the directory entry itself (a symlink at the target is replaced, not followed).
    """
    target = metadata_path(path)
    descriptor, name = tempfile.mkstemp(dir=target.parent, prefix=target.name + ".", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, allow_nan=False, indent=2)
            stream.write("\n")
        os.replace(name, target)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def classify(error):
    if isinstance(error, asyncio.CancelledError | KeyboardInterrupt):
        return "stopped_by_user"
    if isinstance(error, websockets.ConnectionClosed):
        return "connection_lost"
    if isinstance(error, ValueError):  # FormatError: malformed JSON or record; payload is never printed
        return "invalid_data"
    return "error"


async def ticker(stats, path, interval):
    started = monotonic()
    while True:
        await asyncio.sleep(interval)
        print(f"Recording: {monotonic()-started:.0f}s, {stats['messages']} messages -> {path}", flush=True)


def finish(path, stream, meta, stats, duration, status, automatic):
    stream.flush()
    size, sha = file_digest(path)
    regular = status in REGULAR
    meta.update(status=status, regular_end=regular, messages=stats["messages"],
                duration_s=round(duration, 3), bytes=size, sha256=sha)
    if automatic:
        write_metadata(path, meta)
    print(f"Capture {'finished' if regular else 'INCOMPLETE'}: {(REGULAR | INCOMPLETE)[status]}\n"
          f"  File: {path}\n  Messages: {stats['messages']}; duration: {duration:.1f}s; size: {size} bytes\n"
          f"  SHA-256: {sha}", flush=True)


async def capture(url, output=None, *, directory=None, track=None, event=None, driver_alias=None,
                  interval=STATUS_INTERVAL):
    """Record to `output`, or to an automatically named file under `directory` (default data/captures/raw)."""
    validate_ws_url(url, "pitwall_ws")
    automatic = output is None
    if automatic:
        target = output_location(directory or common.PRIVATE_ROOT / DEFAULT_DIRECTORY, "real")
    else:
        if any(value is not None for value in (directory, track, event, driver_alias)):
            raise ValueError("Labels and directory apply only to automatic naming")
        path = output_location(output, "real")
    # Connect before creating the file: a failed connection must not leave an empty capture behind.
    async with websockets.connect(url, max_size=32*1024*1024, proxy=None) as socket:
        started = utc_now()
        if automatic:
            path, stream = create_unique(target, started, track, event, driver_alias)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            stream = path.open("x", encoding="utf-8", newline="\n")
        stats = dict(messages=0)
        meta = dict(format="capture-meta-v1", file=path.name, status="recording",
                    started_utc=started.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    capture_kind="pitwall-websocket-json", shared_memory=False,
                    labels=dict(track=track, event=event, driver_alias=driver_alias,
                                note="user-supplied organisational labels, not verified LMU metadata"))
        begin, status = monotonic(), "error"
        with stream:
            watcher = None
            try:
                stream.write(encode(header("real")))
                stream.flush()
                if automatic:
                    write_metadata(path, meta)
                print(f"Connected to Pitwall. Recording to {path}\nStop with Ctrl+C.", flush=True)
                watcher = asyncio.ensure_future(ticker(stats, path, interval))
                await record(socket, stream, stats=stats)
                status = "pitwall_closed"
            except BaseException as error:
                status = classify(error)
                raise
            finally:
                if watcher:
                    watcher.cancel()
                finish(path, stream, meta, stats, monotonic() - begin, status, automatic)
    return path, status


def cli():
    parser = Parser(description="Record private Pitwall WebSocket JSON (RAW wire capture, not shared memory); "
                                "output must be under data/. Without --output a unique name is chosen.")
    parser.add_argument("--output", help="explicit new file under data/ (exclusive create)")
    parser.add_argument("--output-dir", help="directory under data/ for automatic names")
    parser.add_argument("--track", help="optional label, e.g. spa")
    parser.add_argument("--event", help="optional label, e.g. daily")
    parser.add_argument("--driver-alias", help="optional private alias, not a driver identity")
    parser.add_argument("--pitwall-ws", default=DEFAULT_PITWALL_WS)
    args = parser.parse_args()
    run_cli(lambda: asyncio.run(capture(args.pitwall_ws, args.output, directory=args.output_dir, track=args.track,
                                        event=args.event, driver_alias=args.driver_alias)))


if __name__ == "__main__":
    cli()
