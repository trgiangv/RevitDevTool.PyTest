"""pytest plugin — redirect test execution to a running host instance.

Thin hook orchestrator. Delegates to:
- ``connection`` — bridge lifecycle, discovery, lease, launch, retry
- ``reporting``  — remote result <-> pytest report mapping
- ``suite_lock`` — Windows Mutex + suite context resolution
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from .connection import ensure_host_client
from .constants import (
    DEFAULT_HOST,
    DEFAULT_LAUNCH_TIMEOUT_S,
    DEFAULT_TEST_TIMEOUT_S,
    EXIT_CODE_CONFIG_ERROR,
    HOST_REGISTRY,
    OPT_HOST,
    OPT_LAUNCH,
    OPT_LAUNCH_TIMEOUT,
    OPT_PIPE,
    OPT_TIMEOUT,
    OPT_VERSION,
    PHASE_CALL,
    PLUGIN_NAME,
)
from .models import CaseResult
from .reporting import ItemReportLifecycle, emit_item_reports, run_remote_session, skip_all
from .suite_leasing import SuiteLeaseStore
from .suite_lock import SuiteMutex, resolve_suite_context

if TYPE_CHECKING:
    from .dialog_resolver import StartupDialogResolver
    from .mcp_client import HostMcpClient

# ---------------------------------------------------------------------------
# Session state — only this file owns mutable globals
# ---------------------------------------------------------------------------

_client: HostMcpClient | None = None
_dialog_resolver: StartupDialogResolver | None = None
_lease_store: SuiteLeaseStore | None = None
_suite_mutex = SuiteMutex()

# Stash keys for cross-hook communication
_collect_only_key = pytest.StashKey[bool]()
_remote_results_key = pytest.StashKey[dict[str, list[CaseResult]]]()
_streamed_nodeids_key = pytest.StashKey[set[tuple[str, str]]]()
_report_lifecycle_key = pytest.StashKey[ItemReportLifecycle]()
_remote_collection_failed_key = pytest.StashKey[bool]()
_remote_collection_error_key = pytest.StashKey[str | None]()

_SUPPORTED_HOSTS = ", ".join(sorted(HOST_REGISTRY))


# ---------------------------------------------------------------------------
# Hooks — option registration
# ---------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    grp = parser.getgroup("host", "Host application testing (Revit, AutoCAD, Civil3D, etc.)")
    grp.addoption(
        "--host", dest=OPT_HOST, default=None,
        help=f"Host application name. Supported: {_SUPPORTED_HOSTS} (default: {DEFAULT_HOST}).",
    )
    grp.addoption(
        "--host-version", dest=OPT_VERSION, default=None,
        help="Host version (e.g. 2025, 8.0). Required when --host-launch is set.",
    )
    grp.addoption(
        "--host-timeout", dest=OPT_TIMEOUT, default=None, type=float,
        help=f"Per-test execution timeout in seconds (default: {DEFAULT_TEST_TIMEOUT_S}).",
    )
    grp.addoption(
        "--host-pipe", dest=OPT_PIPE, default=None,
        help="Explicit pipe name (bypasses auto-discovery).",
    )
    grp.addoption(
        "--host-launch", dest=OPT_LAUNCH, action="store_true", default=False,
        help="Force-launch a new host instance (skip reusing existing). Requires --host-version.",
    )
    grp.addoption(
        "--host-launch-timeout", dest=OPT_LAUNCH_TIMEOUT, default=None, type=float,
        help=f"Seconds to wait for host to start (default: {DEFAULT_LAUNCH_TIMEOUT_S}).",
    )

    parser.addini(OPT_HOST, "Host application name", type="string", default=DEFAULT_HOST)
    parser.addini(OPT_VERSION, "Host version (e.g. 2025, 8.0)", type="string", default=None)
    parser.addini(OPT_TIMEOUT, "Per-test timeout (seconds)", type="string", default=str(DEFAULT_TEST_TIMEOUT_S))
    parser.addini(OPT_PIPE, "Explicit pipe name", type="string", default=None)
    parser.addini(OPT_LAUNCH, "Force-launch a new host instance (skip reusing existing)", type="bool", default=False)
    parser.addini(OPT_LAUNCH_TIMEOUT, "Launch timeout (seconds)", type="string", default=str(DEFAULT_LAUNCH_TIMEOUT_S))


# ---------------------------------------------------------------------------
# Hooks — session lifecycle
# ---------------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "revit: mark test to run inside host process")

    reportchars = getattr(config.option, "reportchars", "") or ""
    if "P" not in reportchars:
        config.option.reportchars = reportchars + "P"

    global _lease_store  # noqa: PLW0603
    _lease_store = SuiteLeaseStore()

    config.stash[_collect_only_key] = _is_collect_only(config)
    config.stash[_remote_collection_failed_key] = False
    config.stash[_remote_collection_error_key] = None


@pytest.hookimpl(tryfirst=True)
def pytest_runtestloop(session: pytest.Session) -> bool | None:
    if session.config.stash.get(_collect_only_key, False) or _is_local_unit_session(session):
        return None

    host_name = _resolve_host_name(session.config)
    if not _ensure_client(session, host_name):
        skip_all(session, f"Not connected to {host_name}")
    elif session.items:
        _dispatch_remote_run(session)
    return True


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None) -> bool | None:  # noqa
    results_by_nodeid = item.session.stash.get(_remote_results_key, None)
    if results_by_nodeid is None:
        return None

    streamed = item.session.stash.get(_streamed_nodeids_key, set())
    lifecycle = item.session.stash.get(_report_lifecycle_key, None)
    results = results_by_nodeid.get(item.nodeid, [])

    reports = emit_item_reports(
        item, results,
        collection_failed=item.session.stash.get(_remote_collection_failed_key, False),
        collection_error_message=item.session.stash.get(_remote_collection_error_key, None),
        emitted_cases=streamed,
        lifecycle=lifecycle,
    )
    for report in reports:
        if report.when == PHASE_CALL and report.failed:
            item.session.testsfailed += 1
    return True


def pytest_unconfigure(config: pytest.Config) -> None:  # noqa
    global _client, _dialog_resolver, _lease_store
    _suite_mutex.release()
    if _dialog_resolver is not None:
        _dialog_resolver.stop()
        _dialog_resolver = None
    if _client is not None:
        _client.close()
        _client = None
    _lease_store = None


# ---------------------------------------------------------------------------
# Internal — orchestration
# ---------------------------------------------------------------------------


def _dispatch_remote_run(session: pytest.Session) -> None:
    assert _client is not None
    per_test_timeout = _opt_float(session.config, OPT_TIMEOUT, OPT_TIMEOUT) or DEFAULT_TEST_TIMEOUT_S

    results_by_nodeid, streamed_nodeids, lifecycle, collection_failed, collection_error = run_remote_session(
        session, _client, per_test_timeout,
    )
    session.stash[_remote_results_key] = results_by_nodeid
    session.stash[_streamed_nodeids_key] = streamed_nodeids
    session.stash[_report_lifecycle_key] = lifecycle
    session.stash[_remote_collection_failed_key] = collection_failed
    session.stash[_remote_collection_error_key] = collection_error

    for index, item in enumerate(session.items):
        nextitem = session.items[index + 1] if index + 1 < len(session.items) else None
        session.config.hook.pytest_runtest_protocol(item=item, nextitem=nextitem)


def _ensure_client(session: pytest.Session, host_name: str) -> bool:
    global _client, _dialog_resolver  # noqa: PLW0603

    config = session.config
    suite_key, suite_path = resolve_suite_context(session)

    explicit_pipe = _opt(config, OPT_PIPE, OPT_PIPE)
    if not explicit_pipe and not _suite_mutex.acquire(suite_key):
        pytest.exit(
            f"{PLUGIN_NAME}: Suite is already running in another pytest process (suite={suite_key}).",
            returncode=EXIT_CODE_CONFIG_ERROR,
        )

    force_launch = _opt_bool(config, OPT_LAUNCH, OPT_LAUNCH)
    if force_launch and _client is not None:
        _client.close()
        _client = None

    result = ensure_host_client(
        current_client=_client,
        lease_store=_lease_store,
        launch_timeout_s=_opt_float(config, OPT_LAUNCH_TIMEOUT, OPT_LAUNCH_TIMEOUT) or DEFAULT_LAUNCH_TIMEOUT_S,
        host_name=host_name,
        version=_opt(config, OPT_VERSION, OPT_VERSION),
        explicit_pipe=explicit_pipe,
        suite_key=suite_key,
        suite_path=suite_path,
        force_launch=force_launch,
    )
    if result.dialog_resolver is not None:
        _dialog_resolver = result.dialog_resolver
    if result.error is not None and result.client is None:
        pytest.exit(
            f"{PLUGIN_NAME}: Could not connect to {host_name}: {result.error}",
            returncode=EXIT_CODE_CONFIG_ERROR,
        )
    _client = result.client
    return result.ok


# ---------------------------------------------------------------------------
# Internal — config helpers
# ---------------------------------------------------------------------------


def _resolve_host_name(config: pytest.Config) -> str:
    raw = _opt(config, OPT_HOST, OPT_HOST)
    return raw.strip().lower() if raw else DEFAULT_HOST


def _opt(config: pytest.Config, cli: str, ini: str) -> str | None:
    val = config.getoption(cli, default=None)
    if val is not None:
        return str(val)
    ini_val = config.getini(ini)
    return str(ini_val) if ini_val not in (None, "") else None


def _opt_float(config: pytest.Config, cli: str, ini: str) -> float | None:
    raw = _opt(config, cli, ini)
    return float(raw) if raw else None


def _opt_bool(config: pytest.Config, cli: str, ini: str) -> bool:
    cli_val = config.getoption(cli, default=None)
    if cli_val:
        return True
    ini_val = config.getini(ini)
    return bool(ini_val)


def _is_collect_only(config: pytest.Config) -> bool:
    option = getattr(config, "option", None)
    return bool(getattr(option, "collectonly", False))


def _is_local_unit_session(session: pytest.Session) -> bool:
    """Keep this repository's offline contract tests out of the host runner."""
    repository_root = Path(__file__).resolve().parents[2]
    if Path(session.config.rootpath).resolve() != repository_root:
        return False
    return bool(session.items) and all(item.nodeid.startswith("tests/unit/") for item in session.items)
