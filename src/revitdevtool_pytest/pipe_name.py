"""Canonical host-pipe names and prefix-filtered Windows enumeration."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import unicodedata
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass

_HOST_PIPE_PREFIX = "DevTools"
_HOST_PIPE_SEARCH_PATTERN = rf"\\.\pipe\{_HOST_PIPE_PREFIX}_*"
_DOTNET_WHITESPACE_CATEGORIES = frozenset({"Zs", "Zl", "Zp"})
_DOTNET_CONTROL_WHITESPACE = frozenset("\u0009\u000a\u000b\u000c\u000d\u0085")


@dataclass(frozen=True, slots=True)
class HostIdentity:
    """The canonical identity encoded by a host's named-pipe name."""

    pipe_name: str
    host_app: str
    host_version: str
    process_id: int


def format_host_pipe(host_app: str, host_version: str, process_id: int) -> str:
    """Format a canonical ``DevTools_{Host}_{Version}_{PID}`` pipe name."""
    if (
        _is_dotnet_blank(host_app)
        or "_" in host_app
        or _is_dotnet_blank(host_version)
        or "_" in host_version
        or isinstance(process_id, bool)
        or not isinstance(process_id, int)
        or process_id <= 0
    ):
        raise ValueError("Not a canonical host pipe component")
    return f"{_HOST_PIPE_PREFIX}_{host_app}_{host_version}_{process_id}"


def parse_host_pipe(name: str) -> HostIdentity:
    """Parse a strict canonical host-pipe name into its value object."""
    parts = name.split("_")
    if len(parts) != 4 or parts[0].lower() != _HOST_PIPE_PREFIX.lower():
        raise ValueError(f"Not a canonical host pipe: {name}")
    host_app, host_version, raw_pid = parts[1:]
    if (
        _is_dotnet_blank(host_app)
        or _is_dotnet_blank(host_version)
        or not raw_pid.isdecimal()
        or int(raw_pid) <= 0
    ):
        raise ValueError(f"Not a canonical host pipe: {name}")
    return HostIdentity(name, host_app, host_version, int(raw_pid))


def _is_dotnet_blank(value: str) -> bool:
    return not value or all(_is_dotnet_whitespace(char) for char in value)


def _is_dotnet_whitespace(char: str) -> bool:
    return (
        char in _DOTNET_CONTROL_WHITESPACE
        or unicodedata.category(char) in _DOTNET_WHITESPACE_CATEGORIES
    )


def iter_host_pipe_names(
    find_names: Callable[[str], Iterable[str]] | None = None,
) -> Iterator[str]:
    """Yield only canonical host-pipe names from the DevTools Win32 prefix."""
    enumerate_names = find_names or _iter_win32_pipe_names
    for name in enumerate_names(_HOST_PIPE_SEARCH_PATTERN):
        try:
            parse_host_pipe(name)
        except ValueError:
            continue
        yield name


class _WIN32_FIND_DATAW(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", ctypes.wintypes.DWORD),
        ("ftCreationTime", ctypes.wintypes.FILETIME),
        ("ftLastAccessTime", ctypes.wintypes.FILETIME),
        ("ftLastWriteTime", ctypes.wintypes.FILETIME),
        ("nFileSizeHigh", ctypes.wintypes.DWORD),
        ("nFileSizeLow", ctypes.wintypes.DWORD),
        ("dwReserved0", ctypes.wintypes.DWORD),
        ("dwReserved1", ctypes.wintypes.DWORD),
        ("cFileName", ctypes.wintypes.WCHAR * 260),
        ("cAlternateFileName", ctypes.wintypes.WCHAR * 14),
    ]


def _iter_win32_pipe_names(pattern: str) -> Iterator[str]:
    """Enumerate base pipe names using ``FindFirstFileW`` and ``FindNextFileW``."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    find_first = kernel32.FindFirstFileW
    find_first.argtypes = [ctypes.wintypes.LPCWSTR, ctypes.POINTER(_WIN32_FIND_DATAW)]
    find_first.restype = ctypes.wintypes.HANDLE
    find_next = kernel32.FindNextFileW
    find_next.argtypes = [ctypes.wintypes.HANDLE, ctypes.POINTER(_WIN32_FIND_DATAW)]
    find_next.restype = ctypes.wintypes.BOOL
    find_close = kernel32.FindClose
    find_close.argtypes = [ctypes.wintypes.HANDLE]
    find_close.restype = ctypes.wintypes.BOOL

    data = _WIN32_FIND_DATAW()
    handle = find_first(pattern, ctypes.byref(data))
    if handle == ctypes.wintypes.HANDLE(-1).value:
        return

    try:
        yield data.cFileName
        while find_next(handle, ctypes.byref(data)):
            yield data.cFileName
    finally:
        find_close(handle)
