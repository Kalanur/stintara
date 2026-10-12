"""Small RFC 9562 UUIDv7 generator using only the Python stdlib."""
from __future__ import annotations

import secrets
import time
import uuid


def uuid7() -> uuid.UUID:
    # 48-bit Unix timestamp in milliseconds, version 7, RFC 4122 variant,
    # plus 74 random bits. Suitable as a sortable database identifier.
    unix_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)
    value = (unix_ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return uuid.UUID(int=value)
