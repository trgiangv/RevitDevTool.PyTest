from __future__ import annotations

from types import SimpleNamespace

from revitdevtool_pytest.models import CaseResult, RunResponse
from revitdevtool_pytest.reporting import (
    CaseStreamTracker,
    ItemReportLifecycle,
    _emit_streaming_report,
    emit_item_reports,
)


def case(nodeid: str, phase: str = "call") -> CaseResult:
    return CaseResult(nodeid=nodeid, phase=phase, outcome="passed")


def run_response(results: list[CaseResult]) -> RunResponse:
    return RunResponse(results=tuple(results))


def test_case_event_is_not_emitted_again_from_final_batch() -> None:
    emitted: list[str] = []
    stream = CaseStreamTracker(lambda result: emitted.append(result.nodeid))

    stream.on_case(case("a.py::test_a"))
    stream.emit_final(run_response([case("a.py::test_a"), case("b.py::test_b")]))

    assert emitted == ["a.py::test_a", "b.py::test_b"]


def test_final_batch_emits_all_cases_without_capability() -> None:
    emitted: list[str] = []

    CaseStreamTracker(lambda result: emitted.append(result.nodeid)).emit_final(
        run_response([case("a.py::test_a"), case("b.py::test_b")])
    )

    assert emitted == ["a.py::test_a", "b.py::test_b"]


def test_streamed_phases_and_final_batch_have_one_item_lifecycle() -> None:
    events: list[tuple[str, str]] = []

    class Hook:
        def pytest_runtest_logstart(self, *, nodeid: str, location: object) -> None:
            events.append(("start", nodeid))

        def pytest_runtest_logreport(self, *, report: object) -> None:
            events.append(("report", report.when))

        def pytest_runtest_logfinish(self, *, nodeid: str, location: object) -> None:
            events.append(("finish", nodeid))

    item = SimpleNamespace(
        nodeid="a.py::test_a",
        location=("a.py", 0, "test_a"),
        keywords={},
        ihook=Hook(),
    )
    streamed: set[tuple[str, str]] = set()
    lifecycle = ItemReportLifecycle()
    setup, call, teardown = case(item.nodeid, "setup"), case(item.nodeid), case(item.nodeid, "teardown")

    _emit_streaming_report(setup, {item.nodeid: item}, streamed, lifecycle)
    _emit_streaming_report(call, {item.nodeid: item}, streamed, lifecycle)
    reports = emit_item_reports(
        item,
        [setup, call, teardown],
        collection_failed=False,
        collection_error_message=None,
        emitted_cases=streamed,
        lifecycle=lifecycle,
    )

    assert [report.when for report in reports] == ["teardown"]
    assert events == [
        ("start", item.nodeid),
        ("report", "setup"),
        ("report", "call"),
        ("report", "teardown"),
        ("finish", item.nodeid),
    ]


def test_final_duplicate_after_streamed_teardown_does_not_repeat_lifecycle() -> None:
    events: list[str] = []

    class Hook:
        def pytest_runtest_logstart(self, **_: object) -> None:
            events.append("start")

        def pytest_runtest_logreport(self, **_: object) -> None:
            events.append("report")

        def pytest_runtest_logfinish(self, **_: object) -> None:
            events.append("finish")

    item = SimpleNamespace(
        nodeid="a.py::test_a",
        location=("a.py", 0, "test_a"),
        keywords={},
        ihook=Hook(),
    )
    streamed: set[tuple[str, str]] = set()
    lifecycle = ItemReportLifecycle()
    results = [case(item.nodeid, phase) for phase in ("setup", "call", "teardown")]

    for result in results:
        _emit_streaming_report(result, {item.nodeid: item}, streamed, lifecycle)
    reports = emit_item_reports(
        item,
        results,
        collection_failed=False,
        collection_error_message=None,
        emitted_cases=streamed,
        lifecycle=lifecycle,
    )

    assert reports == []
    assert events == ["start", "report", "report", "report", "finish"]
