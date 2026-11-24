"""Windows integration tests with real kernel mutexes in real processes (skipped on other platforms).

The EXE test additionally needs STINTARA_AGENT_EXE (the built onefile executable); it uses a loopback fake Stintara
server and fake Pitwall with synthetic, test-only data and never touches real infrastructure.
"""
import asyncio
import contextlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import websockets

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows named mutex")

ROOT = Path(__file__).resolve().parents[1]
WS = "ws://127.0.0.1:1/ws/ingest"
TOKEN = "test-only-integration-token"
HOLDER = """
import pathlib, sys, time
sys.path.insert(0, sys.argv[1])
from agent import single_instance as si
ws, token, ctl, tag = sys.argv[2:6]
ctl = pathlib.Path(ctl)
while not (ctl / "go").exists():
    time.sleep(0.002)
try:
    si.acquire(ws, token)
except si.AlreadyRunning:
    (ctl / f"status-{tag}").write_text("DENIED")
    sys.exit(3)
(ctl / f"status-{tag}").write_text("HELD")
while not (ctl / f"stop-{tag}").exists():
    (ctl / f"hb-{tag}").write_text(str(time.time_ns()))
    time.sleep(0.02)
"""
PROCS = []


def wait_for(predicate, timeout=20.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    pytest.fail(f"timed out waiting for {what}")


def hard_kill(proc):
    """Kill the whole process tree without any chance to clean up (taskkill /F)."""
    # Exit code 255 can occur when a tree member (e.g. the venv launcher) vanishes mid-kill; the wait is the check.
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    proc.wait(timeout=20)


@pytest.fixture(autouse=True)
def reap_processes():
    yield
    for proc in PROCS:
        if proc.poll() is None:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    PROCS.clear()


@pytest.fixture
def ctl(tmp_path):
    directory = tmp_path / "ctl"
    directory.mkdir()
    return directory


def status(ctl, tag):
    path = ctl / f"status-{tag}"
    return path.read_text() if path.exists() else None


def heartbeat(ctl, tag):
    path = ctl / f"hb-{tag}"
    return path.read_text() if path.exists() else None


def start_holder(ctl, tag, ws=WS, token=TOKEN, go=True):
    proc = subprocess.Popen([sys.executable, "-c", HOLDER, str(ROOT), ws, token, str(ctl), tag],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    PROCS.append(proc)
    if go:
        (ctl / "go").write_text("1")
    return proc


def is_elevated():
    import ctypes
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


@contextlib.contextmanager
def restricted_thread():
    """Run the current thread under a LUA-restricted copy of this process's token (Administrators deny-only, medium
    integrity): the access checks of the kernel then see what a normal, non-elevated start would present. This is an
    impersonation inside one process, not a second process."""
    import ctypes
    from ctypes import wintypes as w
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", w.LPVOID), ("Attributes", w.DWORD)]

    kernel.GetCurrentProcess.restype = w.HANDLE
    adv.OpenProcessToken.argtypes = (w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE))
    adv.CreateRestrictedToken.argtypes = (w.HANDLE, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.LPVOID, w.DWORD,
                                          w.LPVOID, ctypes.POINTER(w.HANDLE))
    adv.DuplicateTokenEx.argtypes = (w.HANDLE, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, ctypes.POINTER(w.HANDLE))
    adv.ConvertStringSidToSidW.argtypes = (w.LPCWSTR, ctypes.POINTER(w.LPVOID))
    adv.SetTokenInformation.argtypes = (w.HANDLE, w.DWORD, w.LPVOID, w.DWORD)
    adv.SetThreadToken.argtypes = (w.LPVOID, w.HANDLE)

    def check(ok, what):
        if not ok:
            raise OSError(ctypes.get_last_error(), what)

    current, restricted, impersonation, sid = w.HANDLE(), w.HANDLE(), w.HANDLE(), w.LPVOID()
    check(adv.OpenProcessToken(kernel.GetCurrentProcess(), 0xF01FF, ctypes.byref(current)), "OpenProcessToken")
    check(adv.CreateRestrictedToken(current, 4, 0, None, 0, None, 0, None, ctypes.byref(restricted)),
          "CreateRestrictedToken")  # 4 = LUA_TOKEN
    check(adv.DuplicateTokenEx(restricted, 0xF01FF, None, 2, 2, ctypes.byref(impersonation)),
          "DuplicateTokenEx")  # SecurityImpersonation, TokenImpersonation
    check(adv.ConvertStringSidToSidW("S-1-16-8192", ctypes.byref(sid)), "ConvertStringSidToSidW")  # medium
    label = SID_AND_ATTRIBUTES(sid, 0x20)  # SE_GROUP_INTEGRITY
    check(adv.SetTokenInformation(impersonation, 25, ctypes.byref(label), ctypes.sizeof(label)), "SetTokenInformation")
    check(adv.SetThreadToken(None, impersonation), "SetThreadToken")
    try:
        yield
    finally:
        adv.SetThreadToken(None, None)
        for handle in (impersonation, restricted, current):
            kernel.CloseHandle(handle)


def agent_config(tmp_path, server_ws=WS, token=TOKEN, pitwall_ws="ws://127.0.0.1:1"):
    path = tmp_path / "stintara-agent.json"
    path.write_text(json.dumps({"server_ws": server_ws, "token": token, "pitwall_ws": pitwall_ws}), encoding="utf-8")
    return path


def test_second_start_exits_3_and_first_keeps_running(ctl, tmp_path):
    first = start_holder(ctl, "a")
    wait_for(lambda: status(ctl, "a") == "HELD", what="first instance")
    before = wait_for(lambda: heartbeat(ctl, "a"), what="heartbeat")
    started = time.monotonic()
    second = subprocess.run([sys.executable, str(ROOT / "agent" / "stintara_agent.py"), "--config",
                             str(agent_config(tmp_path))], capture_output=True, text=True, timeout=30)
    assert second.returncode == 3
    assert "already running" in second.stderr and TOKEN not in second.stdout + second.stderr
    assert "Connected" not in second.stdout and time.monotonic() - started < 15
    wait_for(lambda: heartbeat(ctl, "a") != before, what="first instance still alive")
    assert first.poll() is None


def test_regular_exit_releases_the_lock(ctl):
    first = start_holder(ctl, "a")
    wait_for(lambda: status(ctl, "a") == "HELD")
    (ctl / "stop-a").write_text("1")
    assert first.wait(timeout=20) == 0
    start_holder(ctl, "b")
    wait_for(lambda: status(ctl, "b") == "HELD", what="new instance after regular exit")


def test_hard_kill_leaves_no_stale_lock(ctl):
    first = start_holder(ctl, "a")
    wait_for(lambda: status(ctl, "a") == "HELD")
    hard_kill(first)
    start_holder(ctl, "b")
    wait_for(lambda: status(ctl, "b") == "HELD", what="new instance after hard kill")


def test_simultaneous_start_has_exactly_one_winner(ctl):
    tags = [f"p{i}" for i in range(8)]
    for tag in tags:
        start_holder(ctl, tag, go=False)
    time.sleep(1.0)  # let every process reach the barrier
    (ctl / "go").write_text("1")
    wait_for(lambda: all(status(ctl, t) for t in tags), 30, "all decisions")
    assert sorted(status(ctl, t) for t in tags) == ["DENIED"] * 7 + ["HELD"]


def test_different_token_or_server_run_in_parallel(ctl):
    start_holder(ctl, "a")
    start_holder(ctl, "b", token=TOKEN + "-other")
    start_holder(ctl, "c", ws="ws://127.0.0.1:2/ws/ingest")
    wait_for(lambda: all(status(ctl, t) == "HELD" for t in "abc"), what="three distinct identities")


def test_restricted_token_still_detects_an_existing_object():
    """The kernel access check for a normal (restricted, medium) token against an object created by this process.

    With an elevated runner this is the P2 scenario (elevated first instance, normal second start). It is a
    same-process impersonation of a real restricted token against a real kernel object, not two processes.
    """
    from agent import single_instance as si
    backend = si.WindowsMutexBackend()
    identity = TOKEN + "-impersonation"
    first = backend.create(si.mutex_name(WS, identity))
    assert first.handle and not first.already_exists
    try:
        with restricted_thread():
            second = backend.create(si.mutex_name(WS, identity))
            assert second.handle and second.already_exists, f"object not detected (error {second.error})"
            backend.close(second.handle)
            with pytest.raises(si.AlreadyRunning):
                si.acquire(WS, identity)
    finally:
        backend.close(first.handle)


@pytest.mark.skipif(sys.platform == "win32" and not is_elevated(), reason="premise exists only for elevated tokens")
def test_premise_default_createmutexw_is_denied_for_a_restricted_token():
    """Why the fix is needed: the previous call (CreateMutexW, MUTEX_ALL_ACCESS, default DACL) is refused."""
    import ctypes
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = (w.LPVOID, w.BOOL, w.LPCWSTR)
    kernel.CreateMutexW.restype = w.HANDLE
    name = "Global\StintaraAgent-premise-" + os.urandom(8).hex()
    first = kernel.CreateMutexW(None, False, name)
    assert first
    try:
        with restricted_thread():
            ctypes.set_last_error(0)
            second = kernel.CreateMutexW(None, False, name)
            assert not second and ctypes.get_last_error() == 5
    finally:
        kernel.CloseHandle(first)


# --- real onefile EXE -------------------------------------------------------------------------------------------

class Fakes:
    """Loopback fake Stintara ingest and fake Pitwall running in a background event loop."""

    def __init__(self):
        self.hellos, self.connections, self.messages = [], 0, 0
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    async def _ingest(self, ws):
        self.connections += 1
        hello = json.loads(await ws.recv())
        self.hellos.append(hello.get("token"))
        await ws.send(json.dumps({"type": "agent_welcome", "driver_slug": "test-driver"}))
        async for _ in ws:
            self.messages += 1

    async def _pitwall(self, ws):
        try:
            while True:
                await ws.send(json.dumps({"type": "ScoringUpdate", "vehicles": []}))
                await asyncio.sleep(0.05)
        except websockets.ConnectionClosed:
            pass

    def _run(self):
        asyncio.set_event_loop(self.loop)

        async def serve():
            async with websockets.serve(self._ingest, "127.0.0.1", 0) as ingest, \
                    websockets.serve(self._pitwall, "127.0.0.1", 0) as pitwall:
                self.ingest_port = ingest.sockets[0].getsockname()[1]
                self.pitwall_port = pitwall.sockets[0].getsockname()[1]
                self.ready.set()
                await asyncio.Future()
        try:
            self.loop.run_until_complete(serve())
        except asyncio.CancelledError:
            pass

    def start(self):
        self.thread.start()
        assert self.ready.wait(10)

    def stop(self):
        self.loop.call_soon_threadsafe(lambda: [t.cancel() for t in asyncio.all_tasks(self.loop)])
        self.thread.join(5)


@pytest.fixture
def fakes():
    server = Fakes()
    server.start()
    yield server
    server.stop()


@pytest.fixture
def exe():
    path = os.environ.get("STINTARA_AGENT_EXE")
    if not path or not Path(path).is_file():
        pytest.skip("Set STINTARA_AGENT_EXE to the built onefile StintaraAgent.exe")
    return path


def start_exe(exe, config, log):
    with open(log, "wb") as out:
        proc = subprocess.Popen([exe, "--config", str(config)], stdout=out, stderr=subprocess.STDOUT)
    PROCS.append(proc)
    return proc


def test_onefile_exe_double_start_hard_kill_and_restart(exe, fakes, tmp_path):
    config = agent_config(tmp_path, f"ws://127.0.0.1:{fakes.ingest_port}/ws/ingest", TOKEN,
                          f"ws://127.0.0.1:{fakes.pitwall_port}")
    first = start_exe(exe, config, tmp_path / "one.log")
    wait_for(lambda: fakes.hellos and fakes.messages >= 3, 60, "first EXE connected and forwarding")
    # Bootloader parent + Python child must not count as a duplicate: exactly one connection so far.
    assert fakes.connections == 1 and fakes.hellos == [TOKEN]
    seen = fakes.messages

    second = start_exe(exe, config, tmp_path / "two.log")
    assert second.wait(timeout=60) == 3
    text = (tmp_path / "two.log").read_text(errors="replace")
    assert "already running" in text and TOKEN not in text and "Connected" not in text
    assert fakes.connections == 1, "the rejected copy must not touch the network"
    wait_for(lambda: fakes.messages > seen + 3, 20, "first EXE still forwarding after the rejected start")
    assert first.poll() is None

    hard_kill(first)
    third = start_exe(exe, config, tmp_path / "three.log")
    wait_for(lambda: len(fakes.hellos) == 2, 60, "third EXE connected after a hard kill")
    assert third.poll() is None
    assert TOKEN not in (tmp_path / "one.log").read_text(errors="replace")
    hard_kill(third)
