from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas import StoredBundle, TaskRequest

ATTEMPT_ID = "11111111-1111-4111-8111-111111111111"
ATTEMPT_TOKEN = "22222222-2222-4222-8222-222222222222"
BUNDLE = StoredBundle(
    object_name="custom/custom_0123456789abcdef0123456789abcdef.json",
    generation=7,
    sha256="a" * 64,
    size_bytes=321,
)


def _task(**changes) -> dict:
    value = {
        "case_kind": "custom",
        "case_id": "custom_0123456789abcdef0123456789abcdef",
        "event_id": "custom_0123456789abcdef0123456789abcdef",
        "issued_day": "2026-08-13",
        "attempt_id": ATTEMPT_ID,
        "attempt_token": ATTEMPT_TOKEN,
        "bundle": BUNDLE,
    }
    value.update(changes)
    return value


def test_custom_task_requires_an_exact_bundle_reference() -> None:
    task = TaskRequest.model_validate(_task())

    assert task.case_kind == "custom"
    assert task.bundle == BUNDLE
    with pytest.raises(ValidationError, match="bundle"):
        TaskRequest.model_validate(_task(bundle=None))


def test_fixture_task_cannot_smuggle_a_bundle_reference() -> None:
    with pytest.raises(ValidationError, match="bundle"):
        TaskRequest.model_validate(
            _task(
                case_kind="fixture",
                case_id="column-rename",
                event_id="9462c403-6afe-5220-8a07-967191220d3a",
            )
        )


def test_custom_bundle_object_must_be_namespaced_to_its_run() -> None:
    with pytest.raises(ValidationError, match="must match"):
        TaskRequest.model_validate(
            _task(
                bundle=BUNDLE.model_copy(
                    update={"object_name": "custom/custom_other.json"}
                )
            )
        )
