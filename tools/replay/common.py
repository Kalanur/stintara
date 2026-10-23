"""Safe CLI boundaries and shared timing; no production agent changes."""
import argparse
import asyncio
import ipaddress
import logging
from pathlib import Path

from .format import finite

PRIVATE_ROOT = Path(__file__).resolve().parents[2] / "data"


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "Invalid arguments; use --help.\n")


def run_cli(operation):
    # Peer exceptions/handshake headers must never leak through library logging.
    logging.getLogger("websockets").disabled = True
    logging.getLogger("websockets.client").disabled = True
    logging.getLogger("websockets.server").disabled = True
    try:
        operation()
    except KeyboardInterrupt:
        print("Stopped.")
    except Exception:
        raise SystemExit("Replay operation failed; check input and configuration.") from None


def output_location(path, source):
    resolved = Path(path).resolve()
    if source != "synthetic" and not resolved.is_relative_to(PRIVATE_ROOT.resolve()):
        raise ValueError("Real-derived output must remain under private data directory")
    return resolved


def timing_options(parser):
    parser.add_argument("--speed", type=float, default=1)
    parser.add_argument("--immediate", action="store_true")


def validate_timing(speed, hold=0):
    if not finite(speed) or speed <= 0 or not finite(hold) or hold < 0:
        raise ValueError("Invalid timing options")


async def send_records(socket, records, speed=1, immediate=False, sleep=asyncio.sleep):
    from .format import encode
    validate_timing(speed)
    previous = 0
    for row in records:
        delay = (row["t"] - previous) / speed
        if not immediate and delay > 0:
            await sleep(delay)
        await socket.send(encode(row["message"]).rstrip("\n"))
        previous = row["t"]


def validate_bind(host, allow_remote=False):
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False  # Require literal loopback; no hostname/DNS ambiguity.
    if not loopback and not allow_remote:
        raise ValueError("Remote bind requires explicit override")
