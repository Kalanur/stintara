"""Automatic, collision-free file names for private Pitwall wire captures.

Labels are organisational, user-supplied text. They are never verified LMU metadata, and a capture may span
several sessions, so the name deliberately makes no session claim.
"""
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

LABEL_MAX = 24
UNKNOWN = "unknown"
SOURCE_TAG = "lmu_pitwall-ws"
DEFAULT_DIRECTORY = Path("captures") / "raw"


def clean_label(value, field="label"):
    """Return a filename-safe label, `unknown` when absent. Reject labels with no usable characters."""
    if value is None:
        return UNKNOWN
    if not isinstance(value, str):
        raise ValueError(f"Invalid {field}")
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").lower()
    label = re.sub(r"[^a-z0-9]+", "-", folded).strip("-")[:LABEL_MAX].strip("-")
    if not label:
        raise ValueError(f"Invalid {field}: use letters or digits")
    return label


def capture_name(started, track=None, event=None, driver_alias=None, index=1):
    """`<UTC start>_lmu_pitwall-ws_<track>_<event>_<driver-alias>_<NN>.jsonl`."""
    if started.tzinfo is None or started.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError("Start time must be UTC")
    if type(index) is not int or index < 1:
        raise ValueError("Invalid index")
    parts = [started.strftime("%Y%m%dT%H%M%SZ"), SOURCE_TAG, clean_label(track, "track"),
             clean_label(event, "event"), clean_label(driver_alias, "driver alias"), f"{index:02d}"]
    return "_".join(parts) + ".jsonl"


def utc_now():
    return datetime.now(timezone.utc)


def create_unique(directory, started, track=None, event=None, driver_alias=None, limit=999):
    """Exclusively create the next free capture file; returns (path, open text stream).

    Collisions (same second, concurrent recorders) advance the index; existing files are never opened for write.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(1, limit + 1):
        path = directory / capture_name(started, track, event, driver_alias, index)
        try:
            return path, path.open("x", encoding="utf-8", newline="\n")
        except FileExistsError:
            continue
    raise FileExistsError("No free capture file name")
