import asyncio
import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import websockets

from tools.replay import common
from tools.replay.capture import capture, file_digest, metadata_path, write_metadata
from tools.replay.format import header, load
from tools.replay.inspect import inspect
from tools.replay.inspector import inspect_stream
from tools.replay.naming import capture_name, clean_label, create_unique
from tools.replay.scenarios import scenario

T0 = datetime(2026, 11, 15, 11, 30, 0, tzinfo=timezone.utc)


def test_name_convention_and_unknown_defaults():
    assert capture_name(T0, "Spa", "daily", "Driver A") == "20261115T113000Z_lmu_pitwall-ws_spa_daily_driver-a_01.jsonl"
    assert capture_name(T0) == "20261115T113000Z_lmu_pitwall-ws_unknown_unknown_unknown_01.jsonl"
    # Non-UTC or naive times are rejected rather than mislabelled.
    with pytest.raises(ValueError):
        capture_name(datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        capture_name(T0.astimezone(timezone(timedelta(hours=2))))


@pytest.mark.parametrize("hostile", ["../../etc", "..\\..\\x", "a/b", "c" + ":\\x", "con", "x\x00y", "Zürich Ring"])
def test_labels_are_filename_safe_and_bounded(hostile):
    label = clean_label(hostile)
    assert label and all(c.isalnum() and c.isascii() or c == "-" for c in label)
    assert len(label) <= 24 and "_" not in label
    assert len(clean_label("x" * 500)) == 24


@pytest.mark.parametrize("bad", ["", "///", "日本語", "  ", 5])
def test_unusable_labels_fail_instead_of_inventing_identity(bad):
    with pytest.raises(ValueError):
        clean_label(bad)


def test_repeated_and_concurrent_starts_never_overwrite(tmp_path):
    first = create_unique(tmp_path, T0, "spa")
    second = create_unique(tmp_path, T0, "spa")
    first[1].close()
    second[1].close()
    assert first[0].name.endswith("_01.jsonl") and second[0].name.endswith("_02.jsonl")
    assert len({p.name for p in tmp_path.iterdir()}) == 4  # two capture/sidecar pairs
    with pytest.raises(FileExistsError):
        create_unique(tmp_path, T0, "spa", limit=2)


@pytest.fixture
def private(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "PRIVATE_ROOT", tmp_path)
    return tmp_path


async def serve(rows, hold=False, junk=None):
    async def handler(socket):
        for row in rows:
            await socket.send(json.dumps(row["message"]))
        if junk:
            await socket.send(junk)
        if hold:
            await asyncio.sleep(30)
    server = await websockets.serve(handler, "127.0.0.1", 0)
    return server, f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"


async def test_automatic_capture_is_valid_replayable_and_checksummed(private, capsys):
    _, rows = scenario()
    server, url = await serve(rows)
    async with server:
        path, status = await capture(url, track="Spa", event="daily", driver_alias="Driver A", interval=.05)
    assert status == "pitwall_closed"
    assert path.parent == private / "captures" / "raw"
    assert path.name.endswith("_lmu_pitwall-ws_spa_daily_driver-a_01.jsonl")
    meta_header, captured = load(path)
    assert meta_header == header("real") and [r["message"] for r in captured] == [r["message"] for r in rows]
    meta = json.loads(metadata_path(path).read_text())
    size, sha = file_digest(path)
    assert (meta["bytes"], meta["sha256"], meta["messages"]) == (size, sha, len(rows))
    assert sha == hashlib.sha256(path.read_bytes()).hexdigest()
    assert meta["status"] == "pitwall_closed" and meta["regular_end"] is True
    assert meta["shared_memory"] is False and "unverified" not in json.dumps(meta) and "ws://" not in json.dumps(meta)
    out = capsys.readouterr().out
    assert "regular end" in out and sha in out and "Driver Alpha" not in out and url not in out
    inspect(path)  # inspector compatibility
    with path.open("rb") as stream:
        assert inspect_stream(stream)["valid_records"] == len(rows)


async def test_two_recordings_in_the_same_second_get_distinct_files(private, monkeypatch):
    from tools.replay import capture as module
    monkeypatch.setattr(module, "utc_now", lambda: T0)
    _, rows = scenario()
    server, url = await serve(rows)
    async with server:
        (a, _), (b, _) = await capture(url), await capture(url)
    assert a != b and a.read_bytes() and b.read_bytes()


async def test_unexpected_ending_is_marked_incomplete_and_prefix_kept(private, capsys):
    _, rows = scenario()
    server, url = await serve(rows[:3], junk="{not json")
    async with server:
        with pytest.raises(ValueError):
            await capture(url, track="spa")
    path = next((private / "captures" / "raw").glob("*.jsonl"))
    assert len(load(path)[1]) == 3
    meta = json.loads(metadata_path(path).read_text())
    assert meta["status"] == "invalid_data" and meta["regular_end"] is False and meta["messages"] == 3
    assert "INCOMPLETE" in capsys.readouterr().out


async def test_ctrl_c_equivalent_cancellation_is_regular_end(private, capsys):
    _, rows = scenario()
    server, url = await serve(rows[:2], hold=True)
    async with server:
        task = asyncio.ensure_future(capture(url, interval=.05))
        for _ in range(100):
            await asyncio.sleep(.05)
            if any((private / "captures" / "raw").glob("*.meta.json")) and "Recording:" in capsys.readouterr().out:
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    meta = json.loads(next((private / "captures" / "raw").glob("*.meta.json")).read_text())
    assert meta["status"] == "stopped_by_user" and meta["regular_end"] is True and meta["messages"] == 2


async def test_connection_failure_leaves_no_file(private):
    with pytest.raises(OSError):
        await capture("ws://127.0.0.1:1")
    assert not list(private.rglob("*.jsonl"))


async def test_boundaries_and_backward_compatible_output(private, tmp_path_factory):
    _, rows = scenario()
    server, url = await serve(rows[:1])
    async with server:
        for kwargs in (dict(directory=tmp_path_factory.mktemp("outside")), dict(directory=private / ".." / "x"),
                       dict(output=private / "a.jsonl", track="spa")):
            with pytest.raises(ValueError):
                await capture(url, **kwargs)
        path, _ = await capture(url, private / "explicit.jsonl")
        assert path.name == "explicit.jsonl" and not metadata_path(path).exists()
        with pytest.raises(FileExistsError):
            await capture(url, private / "explicit.jsonl")
        with pytest.raises(ValueError):
            await capture(url, private.parent / "elsewhere.jsonl")
    assert path.read_bytes().startswith(b'{"record_type":"header"')


async def test_recorder_does_not_touch_a_parallel_normal_client(private):
    _, rows = scenario()
    server, url = await serve(rows)
    async with server:
        async with websockets.connect(url, proxy=None) as agent_like:
            seen = [json.loads(raw) async for raw in agent_like]
        await capture(url)
    assert seen == [r["message"] for r in rows]


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_windows_starter_forwards_arguments_with_spaces(tmp_path):
    fake = tmp_path / "fake python" / "py.cmd"
    fake.parent.mkdir()
    log = tmp_path / "args.txt"
    fake.write_text(f'@echo off\r\n>"{log}" echo %*\r\nexit /b 7\r\n')
    script = Path(__file__).resolve().parents[1] / "tools" / "capture.ps1"
    result = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                             "-Python", str(fake), "-Track", "Spa Francorchamps", "-OutputDir", "my dir"],
                            capture_output=True, text=True)
    assert result.returncode == 7
    args = log.read_text()
    assert "-m tools.replay.capture" in args and '"Spa Francorchamps"' in args and '"my dir"' in args
    assert "--event" not in args


# --- #75 review: sidecar reservation and temporary-file safety ---------------------------------------------

def sidecar_of(path):
    return metadata_path(path)


def test_planted_predictable_tmp_symlink_is_never_followed(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    path, stream = create_unique(tmp_path / "raw", T0)
    stream.close()
    for planted in (sidecar_of(path).with_name(sidecar_of(path).name + ".tmp"),):
        try:
            planted.symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable")
    write_metadata(path, dict(status="recording"))
    write_metadata(path, dict(status="done", sha256="x"))  # repeated update still works
    assert outside.read_text() == "keep"
    assert json.loads(sidecar_of(path).read_text())["status"] == "done"
    assert sorted(p.name for p in path.parent.iterdir() if p.name.endswith(".tmp")) == [
        sidecar_of(path).name + ".tmp"]  # only the planted link remains; ours are cleaned up


def test_sidecar_symlink_target_is_replaced_not_followed(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    path, stream = create_unique(tmp_path / "raw", T0)
    stream.close()
    sidecar_of(path).unlink()
    try:
        sidecar_of(path).symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    write_metadata(path, dict(status="x"))
    assert outside.read_text() == "keep" and not sidecar_of(path).is_symlink()


def test_failed_sidecar_update_leaves_external_files_and_no_temporary(tmp_path, monkeypatch):
    path, stream = create_unique(tmp_path, T0)
    stream.close()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")

    def refuse(*args):
        raise PermissionError

    monkeypatch.setattr("tools.replay.capture.os.replace", refuse)
    with pytest.raises(PermissionError):
        write_metadata(path, dict(status="x"))
    assert outside.read_text() == "keep"
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert sidecar_of(path).read_bytes() == b""  # reservation placeholder untouched


def test_existing_sidecar_without_capture_forces_next_suffix(tmp_path):
    first = tmp_path / capture_name(T0)
    sidecar_of(first).write_text("precious")
    path, stream = create_unique(tmp_path, T0)
    stream.close()
    assert path.name.endswith("_02.jsonl")
    assert sidecar_of(first).read_text() == "precious" and not first.exists()
    assert sidecar_of(path).exists()


def test_existing_capture_without_sidecar_is_untouched_and_leaves_no_orphan(tmp_path):
    first = tmp_path / capture_name(T0)
    first.write_text("precious")
    path, stream = create_unique(tmp_path, T0)
    stream.close()
    assert path.name.endswith("_02.jsonl") and first.read_text() == "precious"
    assert not sidecar_of(first).exists()


def test_failed_reservation_removes_only_its_own_placeholder(tmp_path, monkeypatch):
    foreign = tmp_path / capture_name(T0)
    sidecar_of(foreign).write_text("precious")
    real_open = Path.open

    def failing(self, *args, **kwargs):
        if self.name.endswith(".jsonl"):
            raise PermissionError
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing)
    with pytest.raises(PermissionError):
        create_unique(tmp_path, T0)
    monkeypatch.undo()
    assert [p.name for p in tmp_path.iterdir()] == [sidecar_of(foreign).name]
    assert sidecar_of(foreign).read_text() == "precious"


def test_parallel_reservations_are_distinct_pairs(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: create_unique(tmp_path, T0, "spa"), range(16)))
    for _, stream in results:
        stream.close()
    names = {path.name for path, _ in results}
    assert len(names) == 16
    assert all(sidecar_of(path).exists() for path, _ in results)
    assert len(list(tmp_path.iterdir())) == 32


async def test_header_failure_leaves_visible_incomplete_state(private, monkeypatch):
    from tools.replay import capture as module
    _, rows = scenario()
    server, url = await serve(rows)

    def broken(*args):
        raise OSError

    monkeypatch.setattr(module, "header", broken)
    async with server:
        with pytest.raises(OSError):
            await capture(url, track="spa")
    path = next((private / "captures" / "raw").glob("*.jsonl"))
    meta = json.loads(metadata_path(path).read_text())
    assert meta["status"] == "error" and meta["regular_end"] is False and meta["messages"] == 0
    assert not [p for p in path.parent.iterdir() if p.name.endswith(".tmp")]


async def test_concurrent_recorders_get_distinct_pairs(private, monkeypatch):
    from tools.replay import capture as module
    monkeypatch.setattr(module, "utc_now", lambda: T0)
    _, rows = scenario()
    server, url = await serve(rows)
    async with server:
        results = await asyncio.gather(capture(url), capture(url), capture(url))
    paths = {path for path, _ in results}
    assert len(paths) == 3
    for path in paths:
        assert json.loads(metadata_path(path).read_text())["regular_end"] is True


def test_planted_regular_tmp_file_is_not_written(tmp_path):
    """Runs without symlink privileges (Windows): the old predictable name must be left alone."""
    path, stream = create_unique(tmp_path, T0)
    stream.close()
    planted = sidecar_of(path).with_name(sidecar_of(path).name + ".tmp")
    planted.write_text("keep")
    write_metadata(path, dict(status="x"))
    assert planted.read_text() == "keep"
    assert json.loads(sidecar_of(path).read_text()) == dict(status="x")
