"""Coordinated minimum versions for the unified MCP 4.0 release set."""

from __future__ import annotations

from typing import Final

# Keep aligned with RevitDevTool `source/DevTools.Ipc/ProtocolCompatibility.cs`
# and `docs/MCP/compatibility.md`.
HOST_PROTOCOL_VERSION: Final = "4.0.0"
MIN_HOST_PROTOCOL_VERSION: Final = "4.0.0"
MIN_DAEMON_VERSION: Final = "4.0.0"
MIN_PYTEST_PLUGIN_VERSION: Final = "0.4.0"
MIN_GATEWAY_VERSION: Final = "2.0.0"


class ProtocolVersionMismatch(RuntimeError):
    def __init__(self, component: str, actual: str | None, required: str) -> None:
        self.component = component
        self.actual = actual
        self.required = required
        super().__init__(format_mismatch(component, actual, required))


def format_mismatch(component: str, actual: str | None, required: str) -> str:
    shown = actual if actual else "<missing>"
    return f"protocol_version_mismatch: {component} version {shown} is below required {required}"


def _parse_version(value: str) -> tuple[int, ...]:
    normalized = value.strip().split("+", 1)[0]
    parts: list[int] = []
    for segment in normalized.split("."):
        if not segment.isdigit():
            raise ValueError(f"invalid version segment: {segment}")
        parts.append(int(segment))
    return tuple(parts)


def is_at_least(actual: str | None, minimum: str) -> bool:
    if not actual:
        return False
    try:
        return _parse_version(actual) >= _parse_version(minimum)
    except ValueError:
        return False


def read_host_protocol_version(capabilities: object | None) -> str | None:
    if capabilities is None:
        return None
    experimental = getattr(capabilities, "experimental", None)
    if not isinstance(experimental, dict):
        return None
    devtools = experimental.get("devtools")
    if not isinstance(devtools, dict):
        return None
    protocol = devtools.get("protocol")
    if not isinstance(protocol, dict):
        return None
    version = protocol.get("version")
    return version if isinstance(version, str) and version.strip() else None


def require_host_protocol_version(capabilities: object | None) -> None:
    actual = read_host_protocol_version(capabilities)
    if not is_at_least(actual, MIN_HOST_PROTOCOL_VERSION):
        raise ProtocolVersionMismatch("host", actual, MIN_HOST_PROTOCOL_VERSION)
