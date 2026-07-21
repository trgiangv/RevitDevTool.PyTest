from __future__ import annotations

from dataclasses import dataclass

import pytest

from revitdevtool_pytest.connection import (
    LeaseIdentityMismatch,
    auto_launch,
    connect_host,
    ensure_client,
    reconnect_lease,
)
from revitdevtool_pytest.constants import DEFAULT_CONNECT_TIMEOUT_MS
from revitdevtool_pytest.discovery import HostInstance
from revitdevtool_pytest.suite_leasing import SuiteLease


@dataclass
class RecordingClient:
    pipe_name: str
    connected: bool = False

    def connect(self) -> None:
        self.connected = True


class RecordingConnector:
    def __init__(self) -> None:
        self.opened: list[str] = []

    def __call__(self, pipe_name: str) -> RecordingClient:
        self.opened.append(pipe_name)
        return RecordingClient(pipe_name)


def host(process_id: int, version: str) -> HostInstance:
    return HostInstance(
        pipe_name=f"DevTools_Revit_{version}_{process_id}",
        host_name="revit",
        version=version,
        process_id=process_id,
    )


def suite_lease(process_id: int, pipe_name: str) -> SuiteLease:
    return SuiteLease("suite", "C:/suite", pipe_name, process_id, 1.0, 1.0)


def test_discovery_initializes_only_selected_candidate() -> None:
    connector = RecordingConnector()

    result = ensure_client(
        current_client=None,
        candidates=[host(7, "2025"), host(8, "2026")],
        host_name="revit",
        host_version="2025",
        connector=connector,
    )

    assert result.instance == host(7, "2025")
    assert connector.opened == ["DevTools_Revit_2025_7"]


def test_lease_rejects_pid_reuse_with_wrong_identity() -> None:
    lease = suite_lease(7, "DevTools_Revit_2025_7")

    with pytest.raises(LeaseIdentityMismatch):
        reconnect_lease(lease, candidates=[host(7, "2026")])


class FakeMonotonic:
    def __init__(self, values: list[float]) -> None:
        self._values = iter(values)
        self._last = 0.0

    def __call__(self) -> float:
        try:
            self._last = next(self._values)
        except StopIteration:
            pass
        return self._last


class SlowStartMcpClient:
    """Simulates a host whose MCP endpoint is not ready until several seconds after the pipe appears."""

    instances: list["SlowStartMcpClient"] = []
    connect_attempts = 0
    fail_until_attempt = 3

    def __init__(self, identity, **_: object) -> None:
        self.identity = identity
        self.closed = False
        SlowStartMcpClient.instances.append(self)

    def connect(self) -> None:
        SlowStartMcpClient.connect_attempts += 1
        if SlowStartMcpClient.connect_attempts < SlowStartMcpClient.fail_until_attempt:
            raise ConnectionError("host MCP not ready yet")

    def close(self) -> None:
        self.closed = True


def test_connect_host_waits_for_slow_start_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pipe exists but MCP is not ready for >2s; connect polls until ready within the deadline."""
    SlowStartMcpClient.instances = []
    SlowStartMcpClient.connect_attempts = 0
    SlowStartMcpClient.fail_until_attempt = 3
    monkeypatch.setattr("revitdevtool_pytest.connection.HostMcpClient", SlowStartMcpClient)
    monkeypatch.setattr(
        "revitdevtool_pytest.connection.time.monotonic",
        FakeMonotonic([0.0, 0.0, 1.0, 1.0, 2.5, 2.5, 3.5, 3.5]),
    )
    monkeypatch.setattr("revitdevtool_pytest.connection.time.sleep", lambda _: None)

    client = connect_host("DevTools_Revit_2025_7", timeout_ms=30_000)

    assert SlowStartMcpClient.connect_attempts == 3
    assert len(SlowStartMcpClient.instances) == 3
    assert client.closed is False


def test_connect_host_raises_after_connect_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    SlowStartMcpClient.instances = []
    SlowStartMcpClient.connect_attempts = 0
    SlowStartMcpClient.fail_until_attempt = 100
    monkeypatch.setattr("revitdevtool_pytest.connection.HostMcpClient", SlowStartMcpClient)
    monkeypatch.setattr(
        "revitdevtool_pytest.connection.time.monotonic",
        FakeMonotonic([0.0, 0.0, 1.0, 1.0, 31.0, 31.0]),
    )
    monkeypatch.setattr("revitdevtool_pytest.connection.time.sleep", lambda _: None)

    with pytest.raises(ConnectionError, match="host MCP not ready yet"):
        connect_host("DevTools_Revit_2025_7", timeout_ms=DEFAULT_CONNECT_TIMEOUT_MS)


def test_auto_launch_does_not_sleep_after_pipe_appears(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    instance = host(42, "2025")

    monkeypatch.setattr(
        "revitdevtool_pytest.connection.find_host_executable",
        lambda *_: "C:/Revit/Revit.exe",
    )
    monkeypatch.setattr("revitdevtool_pytest.connection.start_host", lambda *_: 42)
    monkeypatch.setattr("revitdevtool_pytest.connection._start_dialog_resolver", lambda *_: None)
    monkeypatch.setattr(
        "revitdevtool_pytest.connection.wait_for_host_pipe",
        lambda *_args, **_kwargs: instance,
    )
    monkeypatch.setattr("revitdevtool_pytest.connection.time.sleep", lambda seconds: sleeps.append(seconds))

    launched = auto_launch("revit", "2025", launch_timeout_s=120.0)

    assert launched.instance == instance
    assert sleeps == []


def test_connect_host_passes_remaining_timeout_to_client(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[int] = []

    class CapturingClient:
        def __init__(self, identity, *, connect_timeout_ms: int = DEFAULT_CONNECT_TIMEOUT_MS, **_: object) -> None:
            captured.append(connect_timeout_ms)
            self._fail = len(captured) == 1
            self.identity = identity

        def connect(self) -> None:
            if self._fail:
                raise ConnectionError("transient")

        def close(self) -> None:
            pass

    monkeypatch.setattr("revitdevtool_pytest.connection.HostMcpClient", CapturingClient)
    monkeypatch.setattr(
        "revitdevtool_pytest.connection.time.monotonic",
        FakeMonotonic([0.0, 0.0, 0.5, 0.5, 1.0, 1.0]),
    )
    monkeypatch.setattr("revitdevtool_pytest.connection.time.sleep", lambda _: None)

    connect_host("DevTools_Revit_2025_7", timeout_ms=10_000)

    assert captured[0] == 10_000
    assert captured[1] == 9_500
