"""Synchronous pytest MCP client backed by one AnyIO portal thread."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from datetime import timedelta
from typing import Any

import anyio
from anyio.from_thread import BlockingPortal, start_blocking_portal
from mcp import ClientSession, types
from mcp.shared.exceptions import McpError
from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS

from .compatibility import require_host_protocol_version
from .constants import (
    DEFAULT_CONNECT_TIMEOUT_MS,
    MCP_CANCEL_REASON,
    MCP_CLIENT_NAME,
    PACKAGE_VERSION,
    PYTEST_INFRASTRUCTURE_ERROR_STATUS,
    PYTEST_INVALID_RESPONSE_STATUS,
    PYTEST_TOOL_NAME,
    case_event_capabilities,
)
from .models import CaseResult, RunRequest, RunResponse
from .named_pipe_transport import CaseEvent, named_pipe_streams
from .pipe_name import HostIdentity

class HostIdentityMismatch(RuntimeError):
    pass


class RemotePytestInfrastructureError(RuntimeError):
    def __init__(self, status: str) -> None:
        self.status = status
        super().__init__(status)


class PytestClientSession:
    """The only adapter that relies on MCP 1.x private request sequencing."""

    def __init__(self, session: Any) -> None:
        self._session = session
        self._active_request_id: int | None = None
        self._cancelled_active_call = False

    @property
    def next_request_id(self) -> int:
        request_id = getattr(self._session, "_request_id", None)
        if isinstance(request_id, bool) or not isinstance(request_id, int):
            raise RuntimeError("MCP 1.x ClientSession request counter is unavailable")
        return request_id

    @property
    def active_request_id(self) -> int | None:
        return self._active_request_id

    @property
    def cancelled_active_call(self) -> bool:
        return self._cancelled_active_call

    async def initialize(self) -> Any:
        """Initialize with the host's negotiated pytest case-event capability."""
        result = await self._session.send_request(
            types.ClientRequest(
                types.InitializeRequest(
                    params=types.InitializeRequestParams(
                        protocolVersion=types.LATEST_PROTOCOL_VERSION,
                        capabilities=types.ClientCapabilities(
                            experimental=case_event_capabilities()
                        ),
                        clientInfo=types.Implementation(
                            name=MCP_CLIENT_NAME, version=PACKAGE_VERSION
                        ),
                    )
                )
            ),
            types.InitializeResult,
        )
        if result.protocolVersion not in SUPPORTED_PROTOCOL_VERSIONS:
            raise RuntimeError(f"Unsupported protocol version from the server: {result.protocolVersion}")
        await self._session.send_notification(
            types.ClientNotification(types.InitializedNotification())
        )
        return result

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        read_timeout_seconds: timedelta,
        progress_callback: Callable[..., Any] | None,
    ) -> Any:
        if self._active_request_id is not None:
            raise RuntimeError("A pytest MCP session permits only one active request")
        self._active_request_id = self.next_request_id
        self._cancelled_active_call = False
        result_send, result_receive = anyio.create_memory_object_stream[tuple[bool, Any]](1)
        completed = anyio.Event()
        call_scope = anyio.CancelScope(shield=True)

        async def invoke() -> None:
            with call_scope:
                try:
                    result = await self._session.call_tool(
                        name,
                        arguments,
                        read_timeout_seconds=read_timeout_seconds,
                        progress_callback=progress_callback,
                    )
                    await result_send.send((True, result))
                except BaseException as error:
                    await result_send.send((False, error))
                finally:
                    completed.set()
                    await result_send.aclose()

        try:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(invoke)
                try:
                    succeeded, result = await result_receive.receive()
                    if succeeded:
                        return result
                    raise result
                except BaseException as error:
                    if _should_cancel(error):
                        self._cancelled_active_call = True
                        await self.cancel_active()
                        with anyio.CancelScope(shield=True):
                            with anyio.move_on_after(2):
                                await completed.wait()
                    raise
                finally:
                    call_scope.cancel()
                    task_group.cancel_scope.cancel()
        except BaseException as error:
            raise
        finally:
            self._active_request_id = None
            await result_receive.aclose()

    async def cancel_active(self) -> None:
        request_id = self._active_request_id
        if request_id is None:
            return
        with anyio.CancelScope(shield=True):
            with anyio.move_on_after(2):
                await self._session.send_notification(
                    types.ClientNotification(
                        types.CancelledNotification(
                            params=types.CancelledNotificationParams(
                                requestId=request_id,
                                reason=MCP_CANCEL_REASON,
                            )
                        )
                    )
                )


class HostMcpClient:
    """Blocking facade over a dedicated MCP session and named-pipe transport."""

    def __init__(
        self,
        identity: HostIdentity,
        *,
        transport: Callable[..., AbstractAsyncContextManager[Any]] = named_pipe_streams,
        connect_timeout_ms: int = DEFAULT_CONNECT_TIMEOUT_MS,
    ) -> None:
        self._identity = identity
        self._transport = transport
        self._connect_timeout_ms = connect_timeout_ms
        self._portal_context: Any = None
        self._portal: BlockingPortal | None = None
        self._transport_context: AbstractAsyncContextManager[Any] | None = None
        self._session_context: AbstractAsyncContextManager[Any] | None = None
        self._session: PytestClientSession | None = None
        self._server_info: tuple[str, str] | None = None
        self._case_callback: Callable[[CaseResult], None] | None = None
        self._case_progress_token: int | str | None = None
        self._last_case_sequence = -1
        self._run_active = False

    @property
    def server_info(self) -> tuple[str, str]:
        if self._server_info is None:
            raise RuntimeError("MCP session is not connected")
        return self._server_info

    def connect(self) -> None:
        if self._portal is not None:
            return
        self._portal_context = start_blocking_portal(name="revitdevtool-pytest-mcp")
        self._portal = self._portal_context.__enter__()
        try:
            self._portal.call(self._connect_async)
        except BaseException:
            self.close()
            raise

    def run_tests(
        self,
        request: RunRequest,
        *,
        timeout_s: float,
        progress_callback: Callable[..., Any] | None = None,
        on_case: Callable[[CaseResult], None] | None = None,
    ) -> RunResponse:
        if self._portal is None:
            raise RuntimeError("MCP session is not connected")
        return self._portal.call(
            self._run_tests_async, request, timeout_s, progress_callback, on_case
        )

    def close(self) -> None:
        portal, portal_context = self._portal, self._portal_context
        self._portal = None
        self._portal_context = None
        if portal is None or portal_context is None:
            return
        try:
            portal.call(self._close_async)
        finally:
            portal_context.__exit__(None, None, None)

    async def _connect_async(self) -> None:
        self._transport_context = self._transport(
            self._identity.pipe_name,
            open_timeout_ms=self._connect_timeout_ms,
            on_case_event=self._receive_case_event,
        )
        streams = await self._transport_context.__aenter__()
        if hasattr(self._transport, "create_session"):
            self._session_context = self._transport.create_session(*streams)
        else:
            self._session_context = ClientSession(*streams)
        raw_session = await self._session_context.__aenter__()
        self._session = PytestClientSession(raw_session)
        initialized = await self._session.initialize()
        require_host_protocol_version(initialized.capabilities)
        server_name = initialized.serverInfo.name
        server_version = initialized.serverInfo.version
        if (server_name, server_version) != (
            self._identity.host_app,
            self._identity.host_version,
        ):
            raise HostIdentityMismatch(
                "host_identity_mismatch: "
                f"expected {self._identity.host_app} {self._identity.host_version}, "
                f"got {server_name} {server_version}"
            )
        self._server_info = (server_name, server_version)

    async def _run_tests_async(
        self,
        request: RunRequest,
        timeout_s: float,
        progress_callback: Callable[..., Any] | None,
        on_case: Callable[[CaseResult], None] | None,
    ) -> RunResponse:
        if self._session is None:
            raise RuntimeError("MCP session is not connected")
        if self._run_active:
            raise RuntimeError("A pytest MCP session permits only one active request")
        self._run_active = True
        self._case_callback = on_case
        self._case_progress_token = self._session.next_request_id
        self._last_case_sequence = -1
        try:
            result = await self._session.call_tool(
                PYTEST_TOOL_NAME,
                request.to_params(),
                read_timeout_seconds=timedelta(seconds=timeout_s),
                progress_callback=progress_callback,
            )
            structured = result.structuredContent
            if not isinstance(structured, Mapping):
                raise RemotePytestInfrastructureError(PYTEST_INVALID_RESPONSE_STATUS)
            if result.isError:
                status = structured.get("status")
                raise RemotePytestInfrastructureError(
                    status if isinstance(status, str) else PYTEST_INFRASTRUCTURE_ERROR_STATUS
                )
            return RunResponse.from_dict(dict(structured))
        except BaseException:
            if self._session.cancelled_active_call:
                await self._close_async()
            raise
        finally:
            self._case_callback = None
            self._case_progress_token = None
            self._run_active = False

    async def _close_async(self) -> None:
        if self._session_context is not None:
            await self._session_context.__aexit__(None, None, None)
            self._session_context = None
        if self._transport_context is not None:
            await self._transport_context.__aexit__(None, None, None)
            self._transport_context = None
        self._session = None
        self._server_info = None

    def _receive_case_event(self, event: CaseEvent) -> None:
        callback = self._case_callback
        if callback is None:
            return
        if event.progress_token != self._case_progress_token:
            raise ValueError("pytest case event progress token does not match active request")
        if event.sequence <= self._last_case_sequence:
            raise ValueError("pytest case event sequence is not strictly increasing")
        self._last_case_sequence = event.sequence
        callback(CaseResult.from_dict(event.case))


def _should_cancel(error: BaseException) -> bool:
    if isinstance(error, (TimeoutError, KeyboardInterrupt)):
        return True
    if isinstance(error, McpError):
        return error.error.code == 408
    return isinstance(error, anyio.get_cancelled_exc_class())
