from __future__ import annotations

import pytest

from revitdevtool_pytest.pipe_name import (
    HostIdentity,
    format_host_pipe,
    iter_host_pipe_names,
    parse_host_pipe,
)


def test_parse_host_pipe_accepts_exact_grammar() -> None:
    identity = parse_host_pipe("DevTools_Revit_2025_12345")

    assert identity == HostIdentity("DevTools_Revit_2025_12345", "Revit", "2025", 12345)


@pytest.mark.parametrize(
    "name",
    [
        "DevTools.Mcp.v2.12345",
        "DevTools__2025_12345",
        "DevTools_Revit__12345",
        "DevTools_Revit_2025_0",
        "DevTools_Revit_LT_2025_12345",
    ],
)
def test_parse_host_pipe_rejects_noncanonical_names(name: str) -> None:
    with pytest.raises(ValueError, match="canonical host pipe"):
        parse_host_pipe(name)


def test_format_host_pipe_returns_canonical_name() -> None:
    assert format_host_pipe("Revit", "2025", 12345) == "DevTools_Revit_2025_12345"


@pytest.mark.parametrize(
    ("host_app", "host_version", "process_id"),
    [
        ("", "2025", 12345),
        ("Revit_Name", "2025", 12345),
        ("Revit", "", 12345),
        ("Revit", "2025_1", 12345),
        ("Revit", "2025", 0),
    ],
)
def test_format_host_pipe_rejects_noncanonical_components(
    host_app: str,
    host_version: str,
    process_id: int,
) -> None:
    with pytest.raises(ValueError, match="canonical host pipe"):
        format_host_pipe(host_app, host_version, process_id)


def test_enumerator_passes_prefix_to_win32_find() -> None:
    seen: list[str] = []

    names = list(
        iter_host_pipe_names(
            lambda pattern: seen.append(pattern)
            or ["DevTools_Revit_2025_7", "DevToolsDaemon_Control"]
        )
    )

    assert seen == [r"\\.\pipe\DevTools_*"]
    assert names == ["DevTools_Revit_2025_7"]
