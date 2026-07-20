from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
import threading
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from mcp import ClientSession

from revitdevtool_pytest.mcp_client import (
    HostIdentityMismatch,
    HostMcpClient,
    PytestClientSession,
    RemotePytestInfrastructureError,
)
from revitdevtool_pytest.models import RunRequest
from revitdevtool_pytest.named_pipe_transport import CaseEvent, _read_messages
from revitdevtool_pytest.pipe_name import HostIdentity


class FakeSession:
    def __init__(self, *, server_name: str = "Revit", server_version: str = "2025") -> None:
        self._request_id = 0
        self.server_name = server_name
        self.server_version = server_version
        self.calls: list[tuple[str, dict[str, Any], float]] = []
        self.cancelled: list[int] = []
        self.initialize_request: Any = None

    async def send_request(self, request: Any, _: Any) -> Any:
        self.initialize_request = request
        return SimpleNamespace(
            protocolVersion="2025-06-18",
            serverInfo=SimpleNamespace(name=self.server_name, version=self.server_version),
        )

    async def initialize(self) -> Any:
        return SimpleNamespace(
            serverInfo=SimpleNamespace(name=self.server_name, version=self.server_version)
        )

    async def call_tool(self, name: str, arguments: dict[str, Any], **kwargs: Any) -> Any:
        self.calls.append((name, arguments, kwargs["read_timeout_seconds"].total_seconds()))
        return SimpleNamespace(
            isError=False,
            structuredContent={
                "exit_code": 0,
                "summary": {"passed": 1},
                "results": [{"nodeid": "test_a", "outcome": "passed"}],
            },
        )

    async def send_notification(self, notification: Any) -> None:
        if notification.root.method == "notifications/cancelled":
            self.cancelled.append(notification.root.params.requestId)


class FakeTransport:
    def __init__(self, *, server_name: str = "Revit") -> None:
        self.session = FakeSession(server_name=server_name)
        self.transport_closed = False
        self.session_closed = False

    @asynccontextmanager
    async def __call__(self, _: str, **__: Any):
        try:
            yield object(), object()
        finally:
            self.transport_closed = True

    @asynccontextmanager
    async def create_session(self, *_: Any):
        try:
            yield self.session
        finally:
            self.session_closed = True


def fake_transport(*, server_name: str = "Revit") -> FakeTransport:
    return FakeTransport(server_name=server_name)


class LegacyFramePipe:
    def __init__(self) -> None:
        payload = b'{"id":"legacy","method":"tests/run","payload":{}}\n'
        self._reads = [len(payload).to_bytes(4, "little") + payload, b""]

    def read(self, _: int) -> bytes:
        return self._reads.pop(0)

    def write(self, _: bytes) -> None:
        raise AssertionError("the MCP client must not write a legacy bridge response")

    def close(self) -> None:
        pass


def test_connect_initializes_and_validates_server_identity() -> None:
    client = HostMcpClient(
        HostIdentity("DevTools_Revit_2025_7", "Revit", "2025", 7),
        transport=fake_transport(),
    )

    client.connect()

    assert client.server_info == ("Revit", "2025")
    client.close()


def test_connect_rejects_identity_mismatch() -> None:
    client = HostMcpClient(
        HostIdentity("DevTools_Revit_2025_7", "Revit", "2025", 7),
        transport=fake_transport(server_name="AutoCad"),
    )

    with pytest.raises(HostIdentityMismatch, match="host_identity_mismatch"):
        client.connect()


@pytest.mark.anyio
async def test_initialize_advertises_nested_case_event_capability() -> None:
    session = FakeSession()
    adapter = PytestClientSession(session)

    await adapter.initialize()

    assert session.initialize_request.root.params.capabilities.experimental == {
        "devtools": {"pytest": {"caseEvents": {"version": "1"}}}
    }


@pytest.mark.anyio
async def test_mcp_transport_rejects_length_prefixed_legacy_bridge_frame() -> None:
    pipe = LegacyFramePipe()
    destination, receive = anyio.create_memory_object_stream(1)

    try:
        with pytest.raises(ValueError, match="Invalid JSON"):
            await _read_messages(pipe, destination, None)
    finally:
        await receive.aclose()



def test_run_tests_uses_pytest_tool_and_parses_structured_result() -> None:
    transport = fake_transport()
    client = HostMcpClient(
        HostIdentity("DevTools_Revit_2025_7", "Revit", "2025", 7), transport=transport
    )
    client.connect()

    result = client.run_tests(RunRequest(nodeids=["test_a"]), timeout_s=12)

    assert result.exit_code == 0
    assert result.summary.passed == 1
    assert transport.session.calls == [
        ("pytest_run", {"workspace_root": "", "test_root": "", "nodeids": ["test_a"], "pytest_args": []}, 12)
    ]
    client.close()


def test_run_tests_raises_stable_infrastructure_status() -> None:
    transport = fake_transport()

    async def failed_call(*_: Any, **__: Any) -> Any:
        return SimpleNamespace(isError=True, structuredContent={"status": "pytest_runner_failed"})

    transport.session.call_tool = failed_call  # type: ignore[method-assign]
    client = HostMcpClient(
        HostIdentity("DevTools_Revit_2025_7", "Revit", "2025", 7), transport=transport
    )
    client.connect()

    with pytest.raises(RemotePytestInfrastructureError, match="pytest_runner_failed"):
        client.run_tests(RunRequest(), timeout_s=1)

    client.close()


@pytest.mark.anyio
async def test_private_request_counter_is_captured_and_cancelled() -> None:
    session = FakeSession()
    adapter = PytestClientSession(session)

    assert adapter.next_request_id == 0
    assert adapter.active_request_id is None
    adapter._active_request_id = 0

    await adapter.cancel_active()

    assert session.cancelled == [0]


@pytest.mark.anyio
async def test_client_session_rejects_a_second_active_call() -> None:
    class BlockingSession(FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.started = anyio.Event()

        async def call_tool(self, *_: Any, **__: Any) -> Any:
            self.started.set()
            await anyio.sleep_forever()

    session = BlockingSession()
    adapter = PytestClientSession(session)

    async def first_call() -> None:
        await adapter.call_tool("pytest_run", {}, read_timeout_seconds=timedelta(seconds=1), progress_callback=None)

    async with anyio.create_task_group() as group:
        group.start_soon(first_call)
        await session.started.wait()
        with pytest.raises(RuntimeError, match="only one active request"):
            await adapter.call_tool("pytest_run", {}, read_timeout_seconds=timedelta(seconds=1), progress_callback=None)
        group.cancel_scope.cancel()

    assert session.cancelled == [0]


@pytest.mark.anyio
async def test_adapter_uses_installed_mcp_session_counter_and_single_flight() -> None:
    incoming_send, incoming_receive = anyio.create_memory_object_stream(1)
    outgoing_send, _ = anyio.create_memory_object_stream(1)
    sdk_session = ClientSession(incoming_receive, outgoing_send)
    started = anyio.Event()

    async def blocking_call(*_: Any, **__: Any) -> Any:
        started.set()
        await anyio.sleep_forever()

    sdk_session.call_tool = blocking_call  # type: ignore[method-assign]
    adapter = PytestClientSession(sdk_session)

    async def first_call() -> None:
        await adapter.call_tool("pytest_run", {}, read_timeout_seconds=timedelta(seconds=1), progress_callback=None)

    assert adapter.next_request_id == 0
    async with anyio.create_task_group() as group:
        group.start_soon(first_call)
        await started.wait()
        with pytest.raises(RuntimeError, match="only one active request"):
            await adapter.call_tool("pytest_run", {}, read_timeout_seconds=timedelta(seconds=1), progress_callback=None)
        group.cancel_scope.cancel()

    await incoming_send.aclose()
    await outgoing_send.aclose()


@pytest.mark.anyio
async def test_cancellation_waits_for_cooperative_completion_before_close() -> None:
    class CooperatingSession(FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.started = anyio.Event()
            self.cancel_received = anyio.Event()
            self.completed = anyio.Event()
            self.order: list[str] = []

        async def call_tool(self, *_: Any, **__: Any) -> Any:
            self.started.set()
            await self.cancel_received.wait()
            self.order.append("completed")
            self.completed.set()
            return SimpleNamespace(isError=False, structuredContent={})

        async def send_notification(self, notification: Any) -> None:
            await super().send_notification(notification)
            if notification.root.method == "notifications/cancelled":
                self.order.append("cancelled")
                self.cancel_received.set()

    session = CooperatingSession()
    adapter = PytestClientSession(session)
    scope: anyio.CancelScope | None = None

    async def invoke() -> None:
        nonlocal scope
        with anyio.CancelScope() as scope:
            with pytest.raises(anyio.get_cancelled_exc_class()):
                await adapter.call_tool("pytest_run", {}, read_timeout_seconds=timedelta(seconds=1), progress_callback=None)

    async with anyio.create_task_group() as group:
        group.start_soon(invoke)
        await session.started.wait()
        assert scope is not None
        scope.cancel()

    assert session.order == ["cancelled", "completed"]


def test_case_event_rejects_mismatched_token_and_sequence_without_overwriting_active_callback() -> None:
    client = HostMcpClient(
        HostIdentity("DevTools_Revit_2025_7", "Revit", "2025", 7), transport=fake_transport()
    )
    received: list[str] = []
    client._case_callback = lambda case: received.append(case.nodeid)
    client._case_progress_token = 4

    client._receive_case_event(CaseEvent(4, 1, {"nodeid": "test_a"}))
    with pytest.raises(ValueError, match="progress token"):
        client._receive_case_event(CaseEvent(5, 2, {"nodeid": "wrong"}))
    with pytest.raises(ValueError, match="strictly increasing"):
        client._receive_case_event(CaseEvent(4, 1, {"nodeid": "duplicate"}))

    assert received == ["test_a"]


@pytest.mark.anyio
async def test_second_host_run_rejects_before_mutating_active_case_routing() -> None:
    client = HostMcpClient(
        HostIdentity("DevTools_Revit_2025_7", "Revit", "2025", 7), transport=fake_transport()
    )
    session = PytestClientSession(FakeSession())
    session._active_request_id = 3
    client._session = session
    client._run_active = True
    active_callback = lambda _: None
    client._case_callback = active_callback
    client._case_progress_token = 3

    with pytest.raises(RuntimeError, match="only one active request"):
        await client._run_tests_async(RunRequest(), 1, None, lambda _: None)

    assert client._case_callback is active_callback
    assert client._case_progress_token == 3


def test_close_joins_portal_thread_and_closes_only_client_resources() -> None:
    transport = fake_transport()
    client = HostMcpClient(
        HostIdentity("DevTools_Revit_2025_7", "Revit", "2025", 7), transport=transport
    )
    client.connect()
    portal_threads = [thread for thread in threading.enumerate() if thread.name == "revitdevtool-pytest-mcp"]

    client.close()

    assert portal_threads
    assert not any(thread.is_alive() for thread in portal_threads)
    assert transport.session_closed
    assert transport.transport_closed
