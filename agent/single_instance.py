"""Windows single-instance protection for the agent (one running agent per server URL and token).

Mechanism: a named kernel mutex used purely as an existence marker. Nothing waits on it or owns it logically; the
kernel removes it when the last handle closes, i.e. also after a crash or a hard kill, so there is no stale lock.
PyInstaller onefile: the bootloader parent never runs this code, only the Python child does, so parent and child
are not duplicates of each other. Detection is never based on process names or paths.

The mutex name contains only a truncated, domain-separated SHA-256 of the identity. The token and URL never appear
in the name, in messages or in logs. This module performs no network access.
"""
from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from urllib.parse import urlsplit

EXIT_ALREADY_RUNNING = 3
ERROR_ACCESS_DENIED = 5
ERROR_ALREADY_EXISTS = 183
NAME_PREFIX = "StintaraAgent-"
_DOMAIN = b"stintara-agent-single-instance-v1"

ALREADY_RUNNING_MESSAGE = (
    "A Stintara agent with this configuration is already running on this computer. "
    "This copy exits now; the running instance is not affected.\n"
    "Note: a running StintaraAgent.exe appears twice in Task Manager (normal for the onefile build)."
)


class AlreadyRunning(Exception):
    """Another process on this computer holds the lock for the same identity."""


def _normalized_server(server_ws: str) -> str:
    # Scheme and host are case-insensitive; path and query are kept as configured.
    url = urlsplit(server_ws)
    host = (url.hostname or "").lower()
    netloc = f"[{host}]" if ":" in host else host
    if url.port is not None:
        netloc += f":{url.port}"
    return f"{url.scheme.lower()}://{netloc}{url.path}{'?' + url.query if url.query else ''}"


def instance_id(server_ws: str, token: str) -> str:
    """Stable 128-bit hex identity of (server_ws, token); length-prefixed so fields cannot run into each other."""
    digest = hashlib.sha256()
    digest.update(_DOMAIN)
    for part in (_normalized_server(server_ws), token):
        data = part.encode("utf-8")
        digest.update(len(data).to_bytes(4, "big") + data)
    return digest.hexdigest()[:32]


def mutex_name(server_ws: str, token: str, scope: str = "Global") -> str:
    return f"{scope}\\{NAME_PREFIX}{instance_id(server_ws, token)}"


@dataclass(frozen=True)
class CreateResult:
    handle: object            # opaque; None when the object could not be created
    already_exists: bool
    error: int = 0            # Win32 error when handle is None


class WindowsMutexBackend:
    """ctypes access to kernel32; the only platform-specific part."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._ctypes = ctypes
        create = self._kernel32.CreateMutexW
        create.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        create.restype = wintypes.HANDLE
        close = self._kernel32.CloseHandle
        close.argtypes = (wintypes.HANDLE,)
        close.restype = wintypes.BOOL

    def create(self, name: str) -> CreateResult:
        # lpMutexAttributes=NULL: default security, handle is not inheritable. bInitialOwner=FALSE: no ownership.
        self._ctypes.set_last_error(0)
        handle = self._kernel32.CreateMutexW(None, False, name)
        error = self._ctypes.get_last_error()
        if not handle:
            return CreateResult(None, False, error or 1)
        return CreateResult(handle, error == ERROR_ALREADY_EXISTS)

    def close(self, handle) -> None:
        self._kernel32.CloseHandle(handle)


class InstanceLock:
    """Holds the handle for the process lifetime. Not releasing it is intentional (the kernel does it at exit)."""

    def __init__(self, name=None, handle=None, backend=None, degraded=None):
        self.name = name
        self._handle = handle
        self._backend = backend
        self.degraded = degraded      # fixed, secret-free text when protection is reduced or unavailable

    @property
    def active(self) -> bool:
        return self._handle is not None

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None and self._backend is not None:
            self._backend.close(handle)


_held: list[InstanceLock] = []   # strong module-level reference: the handle must outlive every local scope


def acquire(server_ws: str, token: str, backend=None, platform: str | None = None) -> InstanceLock:
    """Claim the lock for this identity or raise AlreadyRunning.

    Anything other than a clear "already exists" answer fails open: a lock problem must never stop the agent from
    running. A Global\\ name that is denied falls back to Local\\ (per logon session only).
    """
    if backend is None:
        if (platform or sys.platform) != "win32":
            return InstanceLock(degraded="Single-instance protection is only available on Windows.")
        try:
            backend = WindowsMutexBackend()
        except Exception:  # ctypes/kernel32 unavailable (e.g. restricted environment)
            return InstanceLock(degraded="Single-instance protection is unavailable (kernel32 not usable).")
    result = None
    scope_note = None
    for scope in ("Global", "Local"):
        try:
            result = backend.create(mutex_name(server_ws, token, scope))
        except Exception:
            return InstanceLock(degraded="Single-instance protection is unavailable (lock creation failed).")
        if result.handle is None and result.error == ERROR_ACCESS_DENIED and scope == "Global":
            scope_note = "Single-instance protection is limited to this Windows logon session."
            continue
        break
    if result.handle is None:
        return InstanceLock(degraded=f"Single-instance protection is unavailable (Windows error {result.error}).")
    if result.already_exists:
        backend.close(result.handle)   # our extra handle only; the first instance's object is untouched
        raise AlreadyRunning
    lock = InstanceLock(mutex_name(server_ws, token, scope), result.handle, backend, scope_note)
    _held.append(lock)
    return lock
