"""Map remote host test results to pytest reports.

Pure functions — no module state, no globals. Every function receives
what it needs as explicit parameters.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Literal

import pytest

from .constants import (
    OUTCOME_ERROR,
    OUTCOME_FAILED,
    OUTCOME_PASSED,
    OUTCOME_SKIPPED,
    OUTCOME_XFAILED,
    OUTCOME_XPASSED,
    PHASE_CALL,
    PLUGIN_NAME,
)
from .mcp_client import HostMcpClient
from .models import CaseResult, RunRequest, RunResponse

log = logging.getLogger(PLUGIN_NAME)

PytestOutcome = Literal["passed", "failed", "skipped"]

_VALID_REPORT_OUTCOMES = frozenset({OUTCOME_PASSED, OUTCOME_FAILED, OUTCOME_SKIPPED})


class CaseStreamTracker:
    """Emit each streamed node/phase once and retain that identity for the batch."""

    def __init__(self, emit: Callable[[CaseResult], None]) -> None:
        self._emit = emit
        self.emitted: set[tuple[str, str]] = set()

    def on_case(self, result: CaseResult) -> None:
        key = (result.nodeid, result.phase)
        if key in self.emitted:
            return
        self.emitted.add(key)
        self._emit(result)

    def emit_final(self, response: RunResponse) -> None:
        for result in response.results:
            self.on_case(result)


# ---------------------------------------------------------------------------
# Remote session execution
# ---------------------------------------------------------------------------


def run_remote_session(
    session: pytest.Session,
    client: HostMcpClient,
    timeout_per_test: float,
) -> tuple[dict[str, list[CaseResult]], set[tuple[str, str]], bool, str | None]:
    """Run tests remotely, streaming progress as it arrives.

    Returns ``(results_by_nodeid, streamed_nodeids, collection_failed, error_msg)``.
    When an IDE adapter is active, streaming is disabled to avoid
    duplicate reports in the test tree.
    """
    workspace_root = str(session.config.rootdir)
    total_timeout = timeout_per_test * max(len(session.items), 1)
    nodeids = [item.nodeid for item in session.items]
    stream = CaseStreamTracker(
        lambda result: _emit_streaming_report(
            result, {item.nodeid: item for item in session.items}, stream.emitted
        )
    )
    on_case = None if _is_ide_adapter_active(session) else stream.on_case
    response = _request_remote_run(
        client,
        workspace_root,
        nodeids,
        total_timeout,
        on_case,
        _build_progress_callback(session),
    )
    if response is None:
        fail_all(session, f"{PLUGIN_NAME}: Remote execution failed.")
        return {}, stream.emitted, False, None

    collection_error_message = _report_collection_errors(session, response)
    if _is_global_collection_failure(response):
        return {}, stream.emitted, True, collection_error_message

    results_by_nodeid: dict[str, list[CaseResult]] = {}
    for r in response.results:
        results_by_nodeid.setdefault(r.nodeid, []).append(r)

    return results_by_nodeid, stream.emitted, False, None


def _build_progress_callback(session: pytest.Session) -> Callable[..., None] | None:
    if _is_ide_adapter_active(session):
        return None

    reporter = session.config.pluginmanager.getplugin("terminalreporter")
    if reporter is None:
        return None

    def on_progress(*values: Any) -> None:
        current = values[1] if len(values) > 1 else values[0] if values else "?"
        total = values[2] if len(values) > 2 else None
        suffix = f"/{total}" if total is not None else ""
        reporter.write_line(f"{PLUGIN_NAME}: host progress {current}{suffix}")

    return on_progress


def _is_ide_adapter_active(session: pytest.Session) -> bool:
    """Detect if an IDE test adapter plugin is loaded."""
    pm = session.config.pluginmanager
    return pm.hasplugin("vscode_pytest") or bool(os.environ.get("TEST_RUN_PIPE"))


def _request_remote_run(
    client: HostMcpClient,
    workspace_root: str,
    nodeids: list[str],
    timeout_s: float,
    on_case: Callable[[CaseResult], None] | None,
    on_progress: Callable[..., None] | None,
) -> RunResponse | None:
    try:
        return client.run_tests(
            RunRequest(workspace_root=workspace_root, test_root=workspace_root, nodeids=nodeids),
            timeout_s=timeout_s,
            progress_callback=on_progress,
            on_case=on_case,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("Remote request failed: %s", exc)
        return None


def _report_collection_errors(
    session: pytest.Session,
    response: RunResponse,
) -> str | None:
    first_message: str | None = None
    for err in response.collection_errors:
        report_collection_error(session, err.nodeid, err.message, err.traceback)
        if first_message is None:
            first_message = err.message or err.traceback or "Remote collection failed."
    return first_message


# ---------------------------------------------------------------------------
# Streaming progress (real-time per-test reports)
# ---------------------------------------------------------------------------


def _emit_streaming_report(
    result: CaseResult,
    items_by_nodeid: dict[str, pytest.Item],
    streamed: set[tuple[str, str]],
) -> None:
    """Emit a live TestReport from a progress notification (CLI only).

    Emits ``logstart`` / ``logreport`` / ``logfinish`` so the terminal
    reporter can display results as they arrive. Items emitted here are
    tracked in *streamed* so the batch phase can skip them.
    """
    try:
        item = items_by_nodeid.get(result.nodeid)
        if item is None:
            return

        ihook = item.ihook
        key = (result.nodeid, result.phase)
        is_first = not any(nodeid == result.nodeid for nodeid, _ in streamed)
        if is_first:
            ihook.pytest_runtest_logstart(nodeid=item.nodeid, location=item.location)

        report = make_report(item, result)
        ihook.pytest_runtest_logreport(report=report)
        streamed.add(key)

        if result.phase in {"call", "teardown"}:
            ihook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)
    except Exception:  # noqa: BLE001
        log.debug("Failed to emit streaming report", exc_info=True)


# ---------------------------------------------------------------------------
# Batch report emission (after full response)
# ---------------------------------------------------------------------------


def emit_item_reports(
    item: pytest.Item,
    results: list[CaseResult],
    *,
    collection_failed: bool,
    collection_error_message: str | None,
    emitted_cases: set[tuple[str, str]] | None = None,
) -> list[pytest.TestReport]:
    if collection_failed:
        message = collection_error_message or "Remote collection failed before test execution."
        pending = [make_error_report(item, message)]
    elif not results:
        pending = [make_error_report(item, "No result received from host for this test.")]
    else:
        already_emitted = emitted_cases or set()
        pending = [make_report(item, result) for result in results if (result.nodeid, result.phase) not in already_emitted]

    if not pending:
        return []

    ihook = item.ihook
    ihook.pytest_runtest_logstart(nodeid=item.nodeid, location=item.location)
    reports = [_emit_single(ihook, report) for report in pending]

    ihook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)
    return reports


def _emit_single(ihook: Any, report: pytest.TestReport) -> pytest.TestReport:
    ihook.pytest_runtest_logreport(report=report)
    return report


# ---------------------------------------------------------------------------
# Report builders
# ---------------------------------------------------------------------------


def make_report(item: pytest.Item, result: CaseResult) -> pytest.TestReport:
    outcome, wasxfail = _normalize_outcome(result)
    longrepr = _build_longrepr(outcome, result)

    sections: list[tuple[str, str]] = []
    if result.stdout:
        sections.append(("Captured stdout", result.stdout))
    if result.stderr:
        sections.append(("Captured stderr", result.stderr))

    report = pytest.TestReport(
        nodeid=item.nodeid,
        location=item.location,
        keywords=dict(item.keywords),
        outcome=outcome,
        longrepr=longrepr,  # type: ignore[arg-type]
        when=result.phase,  # type: ignore[arg-type]
        duration=result.duration_ms / 1000.0,
        sections=sections,
    )
    if wasxfail:
        report.wasxfail = wasxfail  # type: ignore[attr-defined]
    return report


def make_error_report(item: pytest.Item, message: str) -> pytest.TestReport:
    return pytest.TestReport(
        nodeid=item.nodeid,
        location=item.location,
        keywords=dict(item.keywords),
        outcome=OUTCOME_FAILED,  # type: ignore[arg-type]
        longrepr=message,  # type: ignore[arg-type]
        when=PHASE_CALL,  # type: ignore[arg-type]
    )


def _normalize_outcome(result: CaseResult) -> tuple[PytestOutcome, str]:
    outcome = result.outcome
    if outcome == OUTCOME_XFAILED:
        return OUTCOME_SKIPPED, result.message or "expected failure"  # type: ignore[return-value]
    if outcome == OUTCOME_XPASSED:
        return OUTCOME_FAILED, result.message or "expected failure but passed"  # type: ignore[return-value]
    if outcome == OUTCOME_ERROR or outcome not in _VALID_REPORT_OUTCOMES:
        return OUTCOME_FAILED, ""  # type: ignore[return-value]
    return outcome, ""  # type: ignore[return-value]


def _build_longrepr(
    outcome: str,
    result: CaseResult,
) -> str | tuple[str, int, str] | None:
    if outcome == OUTCOME_FAILED and (result.message or result.traceback):
        return result.traceback if result.traceback else result.message
    if outcome == OUTCOME_SKIPPED:
        return "", -1, f"Skipped: {result.message}" if result.message else "Skipped"
    return None


# ---------------------------------------------------------------------------
# Bulk status helpers
# ---------------------------------------------------------------------------


def skip_all(session: pytest.Session, reason: str) -> None:
    for item in session.items:
        ihook = item.ihook
        ihook.pytest_runtest_logstart(nodeid=item.nodeid, location=item.location)
        report = pytest.TestReport(
            nodeid=item.nodeid,
            location=item.location,
            keywords=dict(item.keywords),
            outcome=OUTCOME_SKIPPED,  # type: ignore[arg-type]
            longrepr=("", -1, f"Skipped: {reason}"),  # type: ignore[arg-type]
            when=PHASE_CALL,  # type: ignore[arg-type]
        )
        ihook.pytest_runtest_logreport(report=report)
        ihook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)


def fail_all(session: pytest.Session, message: str) -> None:
    for item in session.items:
        ihook = item.ihook
        ihook.pytest_runtest_logstart(nodeid=item.nodeid, location=item.location)
        ihook.pytest_runtest_logreport(report=make_error_report(item, message))
        ihook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)


def report_collection_error(
    session: pytest.Session, nodeid: str, message: str, tb: str,
) -> None:
    full = f"{message}\n{tb}" if tb else message
    session.config.hook.pytest_collectreport(
        report=pytest.CollectReport(
            nodeid=nodeid or "<collection>",
            outcome=OUTCOME_FAILED,  # type: ignore[arg-type]
            longrepr=full,  # type: ignore[arg-type]
            result=[],
        ),
    )


def _is_global_collection_failure(response: RunResponse) -> bool:
    return bool(response.collection_errors) and not response.results
