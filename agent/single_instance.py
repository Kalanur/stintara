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
ERROR_FILE_NOT_FOUND = 2
PRESENT, DENIED, MISSING = "present", "denied", "missing"
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
    """ctypes access to kernel32/advapi32; the only platform-specific part.

    The mutex is only an existence marker, so every handle is requested with SYNCHRONIZE alone (CreateMutexW would
    ask for MUTEX_ALL_ACCESS, which an existing object created by an elevated process refuses to a normal one).
    A new object gets an explicit DACL: full control for SYSTEM, Administrators and the owner, and SYNCHRONIZE for
    everyone, so a process in a different security context (elevated vs. normal) can still detect it. SYNCHRONIZE
    allows neither modifying nor closing the object for others.
    """

    SYNCHRONIZE = 0x00100000
    SDDL = "D:(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;OW)(A;;0x00100000;;;WD)"

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self._ctypes, self._wintypes = ctypes, wintypes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        k = self._kernel32
        k.CreateMutexExW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD)
        k.CreateMutexExW.restype = wintypes.HANDLE
        k.OpenMutexW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
        k.OpenMutexW.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = (wintypes.HANDLE,)
        k.CloseHandle.restype = wintypes.BOOL
        k.LocalFree.argtypes = (wintypes.HLOCAL,)
        k.LocalFree.restype = wintypes.HLOCAL
        conv = self._advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
        conv.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID), wintypes.LPVOID)
        conv.restype = wintypes.BOOL

    def _attributes(self):
        ctypes, wintypes = self._ctypes, self._wintypes

        class SECURITY_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", wintypes.LPVOID),
                        ("bInheritHandle", wintypes.BOOL)]
        descriptor = wintypes.LPVOID()
        if not self._advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                self.SDDL, 1, ctypes.byref(descriptor), None):
            return None, None      # fall back to the default DACL rather than not protecting at all
        return SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), descriptor, False), descriptor

    def create(self, name: str) -> CreateResult:
        # Not inheritable, not owned (no initial owner), SYNCHRONIZE only.
        attributes, descriptor = self._attributes()
        try:
            self._ctypes.set_last_error(0)
            handle = self._kernel32.CreateMutexExW(
                self._ctypes.byref(attributes) if attributes is not None else None, name, 0, self.SYNCHRONIZE)
            error = self._ctypes.get_last_error()
        finally:
            if descriptor:
                self._kernel32.LocalFree(descriptor)
        if not handle:
            return CreateResult(None, False, error or 1)
        return CreateResult(handle, error == ERROR_ALREADY_EXISTS)

    def probe(self, name: str):
        """Does an object with this name exist? Returns PRESENT, DENIED (exists, cannot be opened), MISSING or an
        unexpected Win32 error code."""
        self._ctypes.set_last_error(0)
        handle = self._kernel32.OpenMutexW(self.SYNCHRONIZE, False, name)
        error = self._ctypes.get_last_error()
        if handle:
            self._kernel32.CloseHandle(handle)
            return PRESENT
        return {ERROR_ACCESS_DENIED: DENIED, ERROR_FILE_NOT_FOUND: MISSING}.get(error, error or 1)

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
    running. One deliberate exception: access denied on Global\\ while the object demonstrably exists (probe) blocks
    the start, because the name derives from the secret identity and an independent Local\\ object would be a
    bypass. Access denied while nothing exists falls back to Local\\ (per logon session only).
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
        name = mutex_name(server_ws, token, scope)
        try:
            result = backend.create(name)
            if result.handle is None and result.error == ERROR_ACCESS_DENIED and scope == "Global":
                # Access denied proves neither a running agent nor its absence. Ask the kernel whether an object
                # with this secret-derived name exists: if it exists but cannot even be opened with SYNCHRONIZE,
                # another security context holds it (never fall back to an independent Local\ object then, that
                # would be a bypass). If it does not exist, creation in Global\ is simply not permitted here.
                found = backend.probe(name)
        except Exception:
            return InstanceLock(degraded="Single-instance protection is unavailable (lock creation failed).")
        if result.handle is None and result.error == ERROR_ACCESS_DENIED and scope == "Global":
            if found in (PRESENT, DENIED):
                raise AlreadyRunning
            if found == MISSING:
                scope_note = "Single-instance protection is limited to this Windows logon session."
                continue
            return InstanceLock(degraded=f"Single-instance protection is unavailable (Windows error {found}).")
        break
    if result.handle is None:
        return InstanceLock(degraded=f"Single-instance protection is unavailable (Windows error {result.error}).")
    if result.already_exists:
        backend.close(result.handle)   # our extra handle only; the first instance's object is untouched
        raise AlreadyRunning
    lock = InstanceLock(mutex_name(server_ws, token, scope), result.handle, backend, scope_note)
    _held.append(lock)
    return lock
