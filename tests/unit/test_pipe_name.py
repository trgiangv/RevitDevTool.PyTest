from __future__ import annotations

import pytest

from revitdevtool_pytest.constants import HOST_REGISTRY
from revitdevtool_pytest.pipe_name import (
    HostIdentity,
    format_host_pipe,
    host_segments_equal,
    identities_equal,
    iter_host_pipe_names,
    parse_host_pipe,
    version_segments_equal,
)

# .NET HostApp.ToString() publish casing (source: DevTools.Logging/IHostAppInfo.cs).
_DOTNET_HOST_APP_PUBLISH_CASING: dict[str, str] = {
    "revit": "Revit",
    "autocad": "AutoCad",
    "civil3d": "Civil3D",
    "plant3d": "Plant3D",
    "acadarch": "AcadArch",
    "acadmech": "AcadMech",
    "acadmep": "AcadMep",
    "acadelec": "AcadElec",
    "acadmap3d": "AcadMap3D",
    "navisworks": "Navisworks",
    "rhino": "Rhino",
    "tekla": "Tekla",
}


def test_parse_host_pipe_accepts_exact_grammar() -> None:
    identity = parse_host_pipe("DevTools_Revit_2025_12345")

    assert identity == HostIdentity("DevTools_Revit_2025_12345", "Revit", "2025", 12345)


@pytest.mark.parametrize(
    "name",
    [
        "DevTools.Mcp.v2.12345",
        "DevTools__2025_12345",
        "DevTools_Revit__12345",
        "DevTools_ _2025_12345",
        "DevTools_\u0085_2025_12345",
        "DevTools_Revit_ _12345",
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
        (" ", "2025", 12345),
        ("\u00a0", "2025", 12345),
        ("Revit_Name", "2025", 12345),
        ("Revit", "", 12345),
        ("Revit", " ", 12345),
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


@pytest.mark.parametrize("control", ["\x1c", "\x1d", "\x1e", "\x1f"])
def test_pipe_components_accept_dotnet_nonwhitespace_controls(control: str) -> None:
    name = format_host_pipe(control, "2025", 12345)

    assert parse_host_pipe(name) == HostIdentity(name, control, "2025", 12345)


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


def test_parse_host_pipe_accepts_case_insensitive_prefix() -> None:
    identity = parse_host_pipe("devtools_Revit_2025_12345")

    assert identity == HostIdentity("devtools_Revit_2025_12345", "Revit", "2025", 12345)


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("Revit", "revit", True),
        ("AutoCad", "AUTOCAD", True),
        ("Revit", "Rhino", False),
    ],
)
def test_host_segments_equal_is_case_insensitive(left: str, right: str, expected: bool) -> None:
    assert host_segments_equal(left, right) is expected


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("2025", "2025", True),
        ("2025", "2025.1", False),
    ],
)
def test_version_segments_equal_is_case_insensitive(left: str, right: str, expected: bool) -> None:
    assert version_segments_equal(left, right) is expected


def test_identities_equal_matches_case_insensitive_host_and_version() -> None:
    left = HostIdentity("DevTools_Revit_2025_42", "Revit", "2025", 42)
    right = HostIdentity("DevTools_revit_2025_42", "revit", "2025", 42)
    other_pid = HostIdentity("DevTools_Revit_2025_43", "Revit", "2025", 43)

    assert identities_equal(left, right)
    assert not identities_equal(left, other_pid)


@pytest.mark.parametrize("host_name", list(HOST_REGISTRY))
def test_host_registry_pipe_prefix_matches_dotnet_publish_casing(host_name: str) -> None:
    cfg = HOST_REGISTRY[host_name]
    expected = _DOTNET_HOST_APP_PUBLISH_CASING[host_name]
    assert cfg.pipe_prefix == expected
    assert format_host_pipe(cfg.pipe_prefix, "2025", 12345) == f"DevTools_{expected}_2025_12345"


def test_format_parse_roundtrip_preserves_publish_casing() -> None:
    for host_name, cfg in HOST_REGISTRY.items():
        name = format_host_pipe(cfg.pipe_prefix, "2025", 12345)
        identity = parse_host_pipe(name)
        assert identity.host_app == cfg.pipe_prefix
        assert identity.host_version == "2025"
        assert identity.process_id == 12345
        assert host_segments_equal(identity.host_app, _DOTNET_HOST_APP_PUBLISH_CASING[host_name])
