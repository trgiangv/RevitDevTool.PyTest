"""Domain models for pytest execution requests and responses.

Mirrors the pytest contracts exposed by the host tool surface.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any, TypeVar

_T = TypeVar("_T")


def _deserialize(cls: type[_T], data: dict[str, Any]) -> _T:
    """Deserialize dict → flat dataclass. Like ``JsonSerializer.Deserialize<T>()``.

    Maps dict keys to dataclass fields by name. Missing keys use field defaults.
    Only works for flat dataclasses (no nested types).
    """
    return cls(**{f.name: data[f.name] for f in fields(cls) if f.name in data})  # type: ignore[arg-type]


# -- Request contracts (mirrors PytestContracts.cs requests) -----------------


@dataclass(slots=True)
class RunRequest:
    workspace_root: str = ""
    test_root: str = ""
    nodeids: list[str] = field(default_factory=list)
    pytest_args: list[str] = field(default_factory=list)

    def to_params(self) -> dict[str, Any]:
        return asdict(self)


# -- Response contracts (mirrors PytestContracts.cs responses) ---------------


@dataclass(frozen=True, slots=True)
class CaseResult:
    nodeid: str = ""
    outcome: str = "error"
    phase: str = "call"
    duration_ms: float = 0.0
    stdout: str = ""
    stderr: str = ""
    message: str = ""
    traceback: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CaseResult:
        return _deserialize(cls, data)


@dataclass(frozen=True, slots=True)
class CollectionError:
    nodeid: str = ""
    path: str = ""
    message: str = ""
    traceback: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CollectionError:
        return _deserialize(cls, data)


@dataclass(frozen=True, slots=True)
class RunSummary:
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0
    xfailed: int = 0
    xpassed: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunSummary:
        return _deserialize(cls, data)


@dataclass(frozen=True, slots=True)
class RunResponse:
    exit_code: int = 1
    summary: RunSummary = field(default_factory=RunSummary)
    results: tuple[CaseResult, ...] = ()
    collection_errors: tuple[CollectionError, ...] = ()
    rootdir: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunResponse:
        return cls(
            exit_code=data.get("exit_code", 1),
            summary=RunSummary.from_dict(data.get("summary", {})),
            results=tuple(CaseResult.from_dict(r) for r in data.get("results", [])),
            collection_errors=tuple(
                CollectionError.from_dict(e) for e in data.get("collection_errors", [])
            ),
            rootdir=data.get("rootdir", ""),
        )
