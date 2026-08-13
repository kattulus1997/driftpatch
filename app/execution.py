from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from .result_delivery import ResultPublisher


@dataclass
class ExecutionBinding:
    event_id: str
    issued_day: str
    attempt_id: str
    execution_token: str
    publisher: ResultPublisher
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
