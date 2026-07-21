"""Newline-delimited MCP streams over a Windows named pipe."""

from __future__ import annotations

import inspect
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

import anyio
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage

from .constants import (
    DEFAULT_CONNECT_TIMEOUT_MS,
    MCP_CASE_EVENT_CASE,
    MCP_CASE_EVENT_METHOD,
    MCP_CASE_EVENT_PROGRESS_TOKEN,
    MCP_CASE_EVENT_SEQUENCE,
    MCP_JSONRPC_FIELD,
    MCP_JSONRPC_VERSION,
    MCP_METHOD_FIELD,
    MCP_PARAMS_FIELD,
)

_MAX_MESSAGE_BYTES = 4 * 1024 * 1024
_READ_SIZE = 64 * 1024


class _PipeHandle(Protocol):
    def read(self, size: int) -> bytes: ...

    def write(self, data: bytes) -> object: ...

    def close(self) -> object: ...


@dataclass(frozen=True, slots=True)
class CaseEvent:
    """The one negotiated extension notification accepted by this transport."""

    progress_token: int | str
    sequence: int
    case: dict[str, Any]

    @classmethod
    def from_json_line(cls, line: bytes) -> CaseEvent:
        raw = json.loads(line)
        if not isinstance(raw, dict):
            raise ValueError("Invalid pytest case-event notification")
        params = _validated_case_event_params(raw)
        return cls(
            params[MCP_CASE_EVENT_PROGRESS_TOKEN],
            params[MCP_CASE_EVENT_SEQUENCE],
            params[MCP_CASE_EVENT_CASE],
        )


CaseEventHandler = Callable[[CaseEvent], Awaitable[None] | None]


@asynccontextmanager
async def named_pipe_streams(
    pipe_name: str,
    *,
    open_timeout_ms: int = DEFAULT_CONNECT_TIMEOUT_MS,
    open_handle: Callable[[str], Any] | None = None,
    on_case_event: CaseEventHandler | None = None,
) -> AsyncIterator[
    tuple[
        anyio.abc.ObjectReceiveStream[SessionMessage],
        anyio.abc.ObjectSendStream[SessionMessage],
    ]
]:
    """Expose a byte-mode pipe as the MCP SDK's session-message streams."""
    opener = open_handle or (lambda name: _open_win32_pipe(name, timeout_ms=open_timeout_ms))
    handle = await anyio.to_thread.run_sync(opener, pipe_name)
    incoming_send, incoming_receive = anyio.create_memory_object_stream[SessionMessage](0)
    outgoing_send, outgoing_receive = anyio.create_memory_object_stream[SessionMessage](0)
    writer_finished = anyio.Event()

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(_read_messages, handle, incoming_send, on_case_event)
        task_group.start_soon(_write_messages, handle, outgoing_receive, writer_finished)
        try:
            yield incoming_receive, outgoing_send
        finally:
            await outgoing_send.aclose()
            with anyio.move_on_after(2):
                await writer_finished.wait()
            task_group.cancel_scope.cancel()
            await anyio.to_thread.run_sync(_close_handle, handle)


named_pipe_client = named_pipe_streams


async def _read_messages(
    handle: Any,
    destination: anyio.abc.ObjectSendStream[SessionMessage],
    on_case_event: CaseEventHandler | None,
) -> None:
    pending = bytearray()
    try:
        while True:
            chunk = await anyio.to_thread.run_sync(_read_handle, handle, _READ_SIZE)
            if not chunk:
                return
            _append_chunk(pending, chunk)
            for raw_line in _drain_complete_lines(pending):
                await _deliver_line(raw_line, destination, on_case_event)
    finally:
        await destination.aclose()


def _append_chunk(pending: bytearray, chunk: bytes) -> None:
    pending.extend(chunk)
    if len(pending) > _MAX_MESSAGE_BYTES and b"\n" not in pending:
        raise ValueError("MCP message exceeds 4 MiB")


def _drain_complete_lines(pending: bytearray) -> list[bytes]:
    lines: list[bytes] = []
    while b"\n" in pending:
        raw_line, _, remainder = pending.partition(b"\n")
        pending[:] = remainder
        if len(raw_line) > _MAX_MESSAGE_BYTES:
            raise ValueError("MCP message exceeds 4 MiB")
        lines.append(raw_line)
    return lines


async def _deliver_line(
    raw_line: bytes,
    destination: anyio.abc.ObjectSendStream[SessionMessage],
    on_case_event: CaseEventHandler | None,
) -> None:
    if not raw_line:
        return
    if _is_case_event(raw_line):
        await _deliver_case_event(raw_line, on_case_event)
        return
    await destination.send(SessionMessage(message=JSONRPCMessage.model_validate_json(raw_line)))


async def _deliver_case_event(raw_line: bytes, on_case_event: CaseEventHandler | None) -> None:
    if on_case_event is None:
        return
    result = on_case_event(CaseEvent.from_json_line(raw_line))
    if inspect.isawaitable(result):
        await result


async def _write_messages(
    handle: Any,
    source: anyio.abc.ObjectReceiveStream[SessionMessage],
    finished: anyio.Event,
) -> None:
    try:
        async with source:
            async for session_message in source:
                encoded = session_message.message.model_dump_json(
                    by_alias=True, exclude_none=True
                ).encode("utf-8") + b"\n"
                await anyio.to_thread.run_sync(_write_handle, handle, encoded)
    finally:
        finished.set()


def _is_case_event(raw_line: bytes) -> bool:
    try:
        raw = json.loads(raw_line)
        return isinstance(raw, dict) and raw.get(MCP_METHOD_FIELD) == MCP_CASE_EVENT_METHOD
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False


def _validated_case_event_params(raw: dict[str, Any]) -> dict[str, Any]:
    params = raw.get(MCP_PARAMS_FIELD)
    if not _is_valid_case_event(raw, params):
        raise ValueError("Invalid pytest case-event notification")
    return params


def _is_valid_case_event(raw: dict[str, Any], params: Any) -> bool:
    return (
        raw.get(MCP_JSONRPC_FIELD) == MCP_JSONRPC_VERSION
        and raw.get(MCP_METHOD_FIELD) == MCP_CASE_EVENT_METHOD
        and isinstance(params, dict)
        and _is_case_event_progress_token(params.get(MCP_CASE_EVENT_PROGRESS_TOKEN))
        and _is_non_boolean_int(params.get(MCP_CASE_EVENT_SEQUENCE))
        and isinstance(params.get(MCP_CASE_EVENT_CASE), dict)
    )


def _is_case_event_progress_token(value: Any) -> bool:
    return isinstance(value, (int, str)) and not isinstance(value, bool)


def _is_non_boolean_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _open_win32_pipe(pipe_name: str, *, timeout_ms: int = DEFAULT_CONNECT_TIMEOUT_MS) -> Any:
    import pywintypes  # type: ignore[import-untyped]
    import win32file  # type: ignore[import-untyped]
    import win32pipe  # type: ignore[import-untyped]

    deadline = time.monotonic() + (timeout_ms / 1000.0)
    path = rf"\\.\pipe\{pipe_name}"
    last_error: BaseException | None = None
    while True:
        try:
            handle = win32file.CreateFile(
                path,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0,
                None,
                win32file.OPEN_EXISTING,
                0,
                None,
            )
            win32pipe.SetNamedPipeHandleState(handle, win32pipe.PIPE_READMODE_BYTE, None, None)
            return handle
        except pywintypes.error as exc:
            last_error = exc
            if exc.winerror not in (2, 231):  # ERROR_FILE_NOT_FOUND, ERROR_PIPE_BUSY
                raise
            remaining_s = deadline - time.monotonic()
            if remaining_s <= 0:
                break
            time.sleep(min(0.1, remaining_s))
    if last_error is not None:
        raise last_error
    raise TimeoutError(f"Timed out opening pipe {pipe_name} after {timeout_ms}ms")


def _read_handle(handle: Any, size: int) -> bytes:
    if hasattr(handle, "read"):
        return bytes(handle.read(size))
    import win32file  # type: ignore[import-untyped]

    _, data = win32file.ReadFile(handle, size)
    return bytes(data)


def _write_handle(handle: Any, data: bytes) -> None:
    if hasattr(handle, "write"):
        handle.write(data)
        return
    import win32file  # type: ignore[import-untyped]

    win32file.WriteFile(handle, data)


def _close_handle(handle: Any) -> None:
    if hasattr(handle, "close"):
        handle.close()
        return
    import win32file  # type: ignore[import-untyped]

    win32file.CloseHandle(handle)
