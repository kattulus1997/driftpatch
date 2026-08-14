from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from .case_data import RepairCase
from .result_delivery import ResultPublisher
from .schemas import StoredBundle


@dataclass
class ExecutionBinding:
    event_id: str
    issued_day: str
    attempt_id: str
    execution_token: str
    publisher: ResultPublisher
    case_kind: str = "fixture"
    case_id: str = ""
    bundle: StoredBundle | None = None
    case: RepairCase | None = None
    published: bool = False


_ACTIVE_EXECUTION: ContextVar[ExecutionBinding | None] = ContextVar(
    "driftpatch_execution",
    default=None,
)


@contextmanager
def bind_execution(binding: ExecutionBinding) -> Iterator[ExecutionBinding]:
    token = _ACTIVE_EXECUTION.set(binding)
    try:
        yield binding
    finally:
        _ACTIVE_EXECUTION.reset(token)


def current_execution() -> ExecutionBinding | None:
    return _ACTIVE_EXECUTION.get()
