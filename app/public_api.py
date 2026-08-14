from __future__ import annotations

import json
import re
from functools import cache

from fastapi import APIRouter, HTTPException, Request, Response, status

from .admission import AdmissionError, AdmissionRejected
from .benchmark import (
    inspect_scenario,
    load_scenario,
    load_scenarios,
    scenario_source,
    source_document,
)
from .schemas import CustomRunReceipt, CustomRunSubmission, RunReceipt

_CUSTOM_RUN_ID = re.compile(r"^custom_[0-9a-f]{32}$")


@cache
def _scenario_response() -> dict:
    return {
        "items": [
            {
                "id": scenario.id,
                "title": scenario.title,
                "report": inspect_scenario(scenario).model_dump(mode="json"),
            }
            for scenario in load_scenarios()
        ]
    }


@cache
def _scenario_ids() -> frozenset[str]:
    return frozenset(scenario.id for scenario in load_scenarios())


def create_public_router() -> APIRouter:
    router = APIRouter(prefix="/api", tags=["DriftPatch demo"])

    @router.get("/scenarios")
    def scenarios() -> dict:
        return _scenario_response()

    @router.get("/scenarios/{scenario_id}/run")
    async def run(scenario_id: str, request: Request, response: Response) -> dict:
        if scenario_id not in _scenario_ids():
            raise HTTPException(status_code=404, detail="Unknown incident")
        try:
            result = await request.app.state.admission.get(scenario_id)
        except AdmissionError as exc:
            raise HTTPException(
                status_code=503,
                detail="Run status is temporarily unavailable.",
            ) from exc
        if result.get("status") == "queued":
            response.status_code = status.HTTP_202_ACCEPTED
        return result

    @router.post(
        "/scenarios/{scenario_id}/run",
        response_model=RunReceipt,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def run_scenario(scenario_id: str, request: Request) -> RunReceipt:
        if scenario_id not in _scenario_ids():
            raise HTTPException(status_code=404, detail="Unknown incident")

        try:
            receipt = await request.app.state.admission.start(scenario_id)
        except AdmissionError as exc:
            raise HTTPException(
                status_code=503,
                detail="Run admission is temporarily unavailable.",
            ) from exc
        return RunReceipt.model_validate(receipt)

    @router.post(
        "/runs",
        response_model=CustomRunReceipt,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def start_custom_run(
        submission: CustomRunSubmission, request: Request
    ) -> CustomRunReceipt:
        try:
            receipt = await request.app.state.admission.start_custom(submission)
        except AdmissionRejected as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": exc.detail},
            ) from exc
        except AdmissionError as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "admission_unavailable",
                    "message": "Custom-run admission is temporarily unavailable.",
                },
            ) from exc
        return CustomRunReceipt.model_validate(receipt)

    @router.get("/runs/{run_id}")
    async def custom_run(
        run_id: str, request: Request, response: Response
    ) -> dict:
        if not _CUSTOM_RUN_ID.fullmatch(run_id):
            raise HTTPException(status_code=404, detail="Unknown custom run")
        try:
            result = await request.app.state.admission.get_custom(run_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Unknown custom run") from exc
        except AdmissionError as exc:
            raise HTTPException(
                status_code=503,
                detail="Run status is temporarily unavailable.",
            ) from exc
        if result.get("status") == "queued":
            response.status_code = status.HTTP_202_ACCEPTED
        return result

    @router.get("/examples/{scenario_id}")
    def example(scenario_id: str) -> dict:
        if scenario_id not in _scenario_ids():
            raise HTTPException(status_code=404, detail="Unknown incident")
        scenario = load_scenario(scenario_id)
        submission = CustomRunSubmission(
            label=scenario.title,
            before=source_document(scenario_source(scenario, "before")),
            after=source_document(scenario_source(scenario, "after")),
            pipeline_json=json.dumps(
                scenario.pipeline.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ),
            contract_json=json.dumps(
                scenario.contract.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        return submission.model_dump(mode="json")

    return router
