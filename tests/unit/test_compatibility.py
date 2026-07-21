from __future__ import annotations

import pytest

from revitdevtool_pytest.compatibility import (
    MIN_HOST_PROTOCOL_VERSION,
    ProtocolVersionMismatch,
    format_mismatch,
    is_at_least,
    read_host_protocol_version,
    require_host_protocol_version,
)


def test_is_at_least_compares_semver_segments() -> None:
    assert is_at_least("4.0.0", "4.0.0")
    assert is_at_least("4.1.0", "4.0.0")
    assert not is_at_least("3.9.9", "4.0.0")
    assert not is_at_least(None, "4.0.0")


def test_require_host_protocol_version_accepts_current_capability() -> None:
    capabilities = SimpleNamespace(
        experimental={"devtools": {"protocol": {"version": "4.0.0"}}}
    )
    require_host_protocol_version(capabilities)


def test_require_host_protocol_version_rejects_missing_capability() -> None:
    with pytest.raises(ProtocolVersionMismatch, match="protocol_version_mismatch"):
        require_host_protocol_version(SimpleNamespace(experimental={}))


def test_format_mismatch_includes_expected_and_actual() -> None:
    assert (
        format_mismatch("host", "3.0.0", MIN_HOST_PROTOCOL_VERSION)
        == "protocol_version_mismatch: host version 3.0.0 is below required 4.0.0"
    )


def test_read_host_protocol_version_returns_none_when_missing() -> None:
    assert read_host_protocol_version(None) is None


class SimpleNamespace:
    def __init__(self, **values: object) -> None:
        for key, value in values.items():
            setattr(self, key, value)
