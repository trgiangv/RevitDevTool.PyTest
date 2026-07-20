from __future__ import annotations

from collections import deque
import json
import threading

import pytest
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage

from revitdevtool_pytest.named_pipe_transport import (
    CaseEvent,
    _drain_complete_lines,
    _is_case_event,
    named_pipe_streams,
)


class FakePipeHandle:
    def __init__(self, reads: list[bytes]) -> None:
        self._reads = deque(reads)
        self.writes: list[bytes] = []

    def read(self, _: int) -> bytes:
        return self._reads.popleft() if self._reads else b""

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def close(self) -> None:
        pass


def make_ping_session_message(request_id: int) -> SessionMessage:
    return SessionMessage(
        message=JSONRPCMessage.model_validate_json(
            f'{{"jsonrpc":"2.0","id":{request_id},"method":"ping"}}'
        )
    )


def test_drain_complete_lines_preserves_incomplete_tail() -> None:
    pending = bytearray(b'{"id":1}\n{"id":2}\n{"id":3')

    lines = _drain_complete_lines(pending)

    assert lines == [b'{"id":1}', b'{"id":2}']
    assert pending == b'{"id":3'


def test_case_event_parser_keeps_typed_extension_fields() -> None:
    event = CaseEvent.from_json_line(
        b'{"jsonrpc":"2.0","method":"notifications/devtools/pytest/case",'
        b'"params":{"progressToken":4,"sequence":2,"case":{"nodeid":"test_a"}}}'
    )

    assert event == CaseEvent(4, 2, {"nodeid": "test_a"})


def test_case_event_parser_rejects_boolean_sequence() -> None:
    raw = (
        b'{"jsonrpc":"2.0","method":"notifications/devtools/pytest/case",'
        b'"params":{"progressToken":4,"sequence":true,"case":{}}}'
    )

    with pytest.raises(ValueError, match="Invalid pytest case-event"):
        CaseEvent.from_json_line(raw)


def test_case_event_parser_rejects_boolean_progress_token() -> None:
    raw = (
        b'{"jsonrpc":"2.0","method":"notifications/devtools/pytest/case",'
        b'"params":{"progressToken":true,"sequence":1,"case":{}}}'
    )

    with pytest.raises(ValueError, match="Invalid pytest case-event"):
        CaseEvent.from_json_line(raw)


def test_case_event_parser_rejects_non_object_json() -> None:
    with pytest.raises(ValueError, match="Invalid pytest case-event"):
        CaseEvent.from_json_line(b"[]")

    assert _is_case_event(b"[]") is False


@pytest.mark.anyio
async def test_transport_decodes_newline_delimited_mcp_message() -> None:
    handle = FakePipeHandle([b'{"jsonrpc":"2.0","id":1,"result":{}}\n'])

    async with named_pipe_streams(
        "DevTools_Revit_2025_7", open_handle=lambda _: handle
    ) as (read, _):
        message = await read.receive()

    assert message.message.root.id == 1


@pytest.mark.anyio
async def test_transport_writes_one_sdk_message_per_line() -> None:
    handle = FakePipeHandle([])

    async with named_pipe_streams(
        "DevTools_Revit_2025_7", open_handle=lambda _: handle
    ) as (_, write):
        await write.send(make_ping_session_message(3))

    assert len(handle.writes) == 1
    assert handle.writes[0].endswith(b"\n")
    assert json.loads(handle.writes[0]) == {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "ping",
    }


@pytest.mark.anyio
async def test_transport_opens_pipe_handle_off_the_event_loop_thread() -> None:
    handle = FakePipeHandle([])
    opened_on: list[int] = []
    main_thread = threading.get_ident()

    def open_handle(_: str) -> FakePipeHandle:
        opened_on.append(threading.get_ident())
        return handle

    async with named_pipe_streams("DevTools_Revit_2025_7", open_handle=open_handle):
        pass

    assert opened_on != [main_thread]
