"""Host MCP client lifecycle: discovery, lease reuse, and launch."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from .constants import DEFAULT_CONNECT_TIMEOUT_MS, EXIT_CODE_CONFIG_ERROR, PLUGIN_NAME, get_host_config
from .discovery import HostInstance, find_host_executable, find_host_pipes, start_host, wait_for_host_pipe
from .mcp_client import HostMcpClient
from .pipe_name import HostIdentity, format_host_pipe, parse_host_pipe

if TYPE_CHECKING:
    from .dialog_resolver import StartupDialogResolver
    from .suite_leasing import SuiteLease, SuiteLeaseStore

log = logging.getLogger(PLUGIN_NAME)
CONNECT_RETRY_DELAY_S = 1.0


class LeaseIdentityMismatch(RuntimeError):
    """A persisted lease PID now represents another canonical host identity."""


@dataclass
class ConnectionResult:
    client: HostMcpClient | None = None
    instance: HostInstance | None = None
    launched: bool = False
    dialog_resolver: StartupDialogResolver | None = None
    error: Exception | None = None

    @property
    def ok(self) -> bool:
        return self.client is not None


def ensure_client(
    *,
    current_client: HostMcpClient | None,
    candidates: list[HostInstance],
    host_name: str,
    host_version: str | None,
    connector: Callable[[str], HostMcpClient] = lambda name: connect_host(name),
) -> ConnectionResult:
    """Connect only the selected candidate, never probe every discovered pipe."""
    if current_client is not None:
        return ConnectionResult(client=current_client)

    target = _select_candidate(candidates, host_name, host_version)
    if target is None:
        return ConnectionResult()
    try:
        client = connector(target.pipe_name)
    except Exception as exc:  # noqa: BLE001 - connection diagnostics reach pytest
        return ConnectionResult(instance=target, error=exc)
    return ConnectionResult(client=client, instance=target)


def ensure_host_client(
    *,
    current_client: HostMcpClient | None,
    lease_store: SuiteLeaseStore | None,
    launch_timeout_s: float,
    host_name: str,
    version: str | None,
    explicit_pipe: str | None,
    suite_key: str,
    suite_path: str,
    force_launch: bool = False,
) -> ConnectionResult:
    """Return one identity-validated MCP client, reusing only a matching lease."""
    if not force_launch and current_client is not None:
        return ConnectionResult(client=current_client)

    if explicit_pipe and not force_launch:
        return _connect_explicit_pipe_or_exit(explicit_pipe)

    instances = instances_for_version(host_name, version)
    if not force_launch:
        if lease_store is not None:
            leased = _try_reconnect_leased(host_name, lease_store, suite_key, suite_path, instances)
            if leased is not None:
                return leased
            instances = instances_for_version(host_name, version)

        free = lease_store.find_free(suite_key, instances) if lease_store else instances
        connected = ensure_client(
            current_client=None,
            candidates=free,
            host_name=host_name,
            host_version=version,
        )
        if connected.ok:
            _assign_lease(lease_store, suite_key, suite_path, connected.instance)
            _log_assignment("Assigned free instance", suite_key, connected.instance)
            return connected
        if connected.error is not None:
            return connected

    launch_version = _resolve_launch_version(host_name, version, instances)
    launched = auto_launch(host_name, launch_version, launch_timeout_s)
    connected = ensure_client(
        current_client=None,
        candidates=[launched.instance],
        host_name=host_name,
        host_version=launch_version,
    )
    connected.launched = connected.ok
    connected.dialog_resolver = launched.dialog_resolver
    if connected.ok:
        _assign_lease(lease_store, suite_key, suite_path, connected.instance)
        _log_assignment("Spawned and leased", suite_key, connected.instance)
    return connected


@dataclass
class LaunchResult:
    instance: HostInstance
    dialog_resolver: StartupDialogResolver | None = None


def auto_launch(host_name: str, version: str, launch_timeout_s: float) -> LaunchResult:
    if find_host_executable(host_name, version) is None:
        pytest.exit(f"{PLUGIN_NAME}: {host_name} {version} is not installed on this machine.", returncode=EXIT_CODE_CONFIG_ERROR)

    process_id = start_host(host_name, version)
    resolver = _start_dialog_resolver(process_id)
    expected_pipe = format_host_pipe(get_host_config(host_name).pipe_prefix, version, process_id)
    instance = wait_for_host_pipe(host_name, version, timeout_s=launch_timeout_s, process_id=process_id)
    if instance is None or instance.pipe_name != expected_pipe:
        pytest.exit(
            f"{PLUGIN_NAME}: {host_name} {version} launched but pipe {expected_pipe} did not appear within {launch_timeout_s}s.",
            returncode=EXIT_CODE_CONFIG_ERROR,
        )
    return LaunchResult(instance, resolver)


def _start_dialog_resolver(process_id: int) -> StartupDialogResolver | None:
    try:
        from .dialog_resolver import StartupDialogResolver

        resolver = StartupDialogResolver(process_id)
        resolver.start()
        return resolver
    except ImportError:
        return None


def instances_for_version(host_name: str, version: str | None) -> list[HostInstance]:
    instances = find_host_pipes(host_name)
    if version is not None:
        return [instance for instance in instances if instance.version == version]
    return sorted(instances, key=lambda instance: (instance.version, instance.process_id), reverse=True)


def reconnect_lease(lease: SuiteLease, *, candidates: list[HostInstance]) -> HostInstance:
    """Resolve a lease only when its full canonical identity still exists."""
    identity = parse_host_pipe(lease.pipe_name)
    if identity.process_id != lease.process_id:
        raise LeaseIdentityMismatch("lease PID does not match its pipe identity")
    instance = next((item for item in candidates if item.process_id == lease.process_id), None)
    if instance is None:
        raise LeaseIdentityMismatch("leased PID is no longer discoverable")
    current = parse_host_pipe(instance.pipe_name)
    if current != identity:
        raise LeaseIdentityMismatch("leased PID was reused by another host identity")
    return instance


def _try_reconnect_leased(
    host_name: str,
    store: SuiteLeaseStore,
    suite_key: str,
    suite_path: str,
    instances: list[HostInstance],
) -> ConnectionResult | None:
    lease = store.get_suite_lease(suite_key)
    if lease is None:
        return None
    if not is_process_alive(lease.process_id, host_name):
        store.clear_suite(suite_key)
        return None
    try:
        instance = reconnect_lease(lease, candidates=instances)
    except (LeaseIdentityMismatch, ValueError):
        store.clear_suite(suite_key)
        return None
    connected = ensure_client(
        current_client=None,
        candidates=[instance],
        host_name=host_name,
        host_version=instance.version,
    )
    if not connected.ok:
        store.clear_suite(suite_key)
        return None
    _assign_lease(store, suite_key, suite_path, instance)
    _log_assignment("Reusing lease", suite_key, instance)
    return connected


def connect_host(
    pipe_name: str,
    *,
    timeout_ms: int = DEFAULT_CONNECT_TIMEOUT_MS,
) -> HostMcpClient:
    """Parse identity before opening the pipe and validate it during MCP initialize."""
    identity: HostIdentity = parse_host_pipe(pipe_name)
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    last_error: Exception | None = None
    while True:
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            break
        client = HostMcpClient(identity, connect_timeout_ms=remaining_ms)
        try:
            client.connect()
            return client
        except Exception as exc:  # noqa: BLE001 - retry transient pipe startup failures
            client.close()
            last_error = exc
            remaining_s = deadline - time.monotonic()
            if remaining_s <= 0:
                break
            time.sleep(min(CONNECT_RETRY_DELAY_S, remaining_s))
    assert last_error is not None
    raise last_error


def _connect_explicit_pipe_or_exit(pipe_name: str) -> ConnectionResult:
    try:
        identity = parse_host_pipe(pipe_name)
        return ensure_client(
            current_client=None,
            candidates=[HostInstance(pipe_name, identity.host_app, identity.host_version, identity.process_id)],
            host_name=identity.host_app,
            host_version=identity.host_version,
        )
    except ValueError as exc:
        pytest.exit(f"{PLUGIN_NAME}: Invalid host pipe: {exc}", returncode=EXIT_CODE_CONFIG_ERROR)


def _select_candidate(candidates: list[HostInstance], host_name: str, host_version: str | None) -> HostInstance | None:
    cfg = get_host_config(host_name)
    matches = [
        candidate for candidate in candidates
        if candidate.host_name.lower() == host_name.lower()
        and parse_host_pipe(candidate.pipe_name).host_app.lower() == cfg.pipe_prefix.lower()
        and (host_version is None or candidate.version == host_version)
    ]
    return max(matches, key=lambda candidate: candidate.process_id) if matches else None


def _assign_lease(store: SuiteLeaseStore | None, suite_key: str, suite_path: str, instance: HostInstance | None) -> None:
    if store is not None and instance is not None:
        store.assign(suite_key, suite_path, instance)


def _log_assignment(label: str, suite_key: str, instance: HostInstance | None) -> None:
    if instance is not None:
        log.info("%s suite=%s pid=%d pipe=%s", label, suite_key, instance.process_id, instance.pipe_name)


def is_process_alive(process_id: int, host_name: str = "revit") -> bool:
    cfg = get_host_config(host_name)
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(process_id))  # type: ignore[attr-defined]
    if handle == 0:
        return False
    try:
        if cfg.exe_name is None:
            return True
        path = ctypes.create_unicode_buffer(260)
        size = ctypes.wintypes.DWORD(260)
        if not ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):  # type: ignore[attr-defined]
            return True
        return path.value.lower().endswith(f"\\{cfg.exe_name.lower()}")
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]


def _resolve_launch_version(host_name: str, version: str | None, instances: list[HostInstance]) -> str:
    if version is not None:
        return version
    if instances:
        return max(instances, key=lambda instance: instance.version).version
    pytest.exit(f"{PLUGIN_NAME}: --host-version is required when no existing {host_name} instances are available.", returncode=EXIT_CODE_CONFIG_ERROR)
