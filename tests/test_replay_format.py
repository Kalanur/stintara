import json
import traceback

import pytest

from tools.replay.format import PROTOCOL, FormatError, dumps, header, load, loads, validate_header, write
from tools.replay.scenarios import record


@pytest.mark.parametrize("text", ["", "{}", "private-marker-not-json",
    '{"record_type":"header","version":2}',
    json.dumps(dict(header(), source="unknown")),
    json.dumps(dict(header(), token="private-marker")),
    json.dumps(dict(header(), version=True)),
    json.dumps(dict(header("sanitized_real"), public_review_required=False))])
def test_invalid_headers_are_payload_free(text):
    with pytest.raises(FormatError) as exc:
        loads(text)
    assert "private-marker" not in "".join(traceback.format_exception(exc.value))


@pytest.mark.parametrize("row", [dict(record_type="message", t=-1, message={}),
    dict(record_type="message", t=float("inf"), message={}),
    dict(record_type="message", t=True, message={}),
    dict(record_type="message", t=0, message=[]),
    dict(record_type="message", t=0, message={"secret": float("nan")})])
def test_invalid_records(row):
    with pytest.raises(FormatError):
        loads(json.dumps(header())+"\n"+json.dumps(row))


def test_roundtrip_decreasing_time_and_exclusive_write(tmp_path):
    rows = [record(0, "ConnectionStatus", game_connected=True), record(1, "Unknown", value=None)]
    text = dumps(header(), rows)
    assert loads(text) == (header(), rows)
    path = tmp_path / "capture.jsonl"
    write(path, header(), rows)
    assert load(path) == (header(), rows)
    with pytest.raises(FileExistsError):
        write(path, header(), [])
    assert path.read_text() == text
    with pytest.raises(FormatError):
        dumps(header(), list(reversed(rows)))
    with pytest.raises(FormatError):
        loads(text, PROTOCOL)
    with pytest.raises(FormatError):
        dumps(header(format=PROTOCOL), [record(0, "agent_hello", token="private-marker")])


@pytest.mark.parametrize("source", ["synthetic", "real", "sanitized_real"])
def test_source_roundtrip(source):
    assert loads(dumps(header(source), []))[0]["source"] == source
    validate_header(header(source))
