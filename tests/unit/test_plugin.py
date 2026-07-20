from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from revitdevtool_pytest.plugin import _is_local_unit_session


def test_local_unit_session_skips_host_execution() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    session = SimpleNamespace(
        config=SimpleNamespace(rootpath=repository_root),
        items=[SimpleNamespace(nodeid="tests/unit/test_plugin.py::test_local_unit_session_skips_host_execution")],
    )

    assert _is_local_unit_session(session) is True


def test_non_unit_session_keeps_host_execution_enabled() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    session = SimpleNamespace(
        config=SimpleNamespace(rootpath=repository_root),
        items=[SimpleNamespace(nodeid="tests/Revit/test_smoke.py::test_smoke")],
    )

    assert _is_local_unit_session(session) is False
