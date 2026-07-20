from __future__ import annotations

from dataclasses import dataclass

import pytest

from revitdevtool_pytest.connection import (
    LeaseIdentityMismatch,
    ensure_client,
    reconnect_lease,
)
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
