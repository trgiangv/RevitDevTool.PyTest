from __future__ import annotations

from revitdevtool_pytest.models import CaseResult, RunResponse
from revitdevtool_pytest.reporting import CaseStreamTracker


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
