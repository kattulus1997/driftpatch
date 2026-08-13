from __future__ import annotations

from functools import cache

from fastapi import APIRouter, HTTPException, Request, Response, status

from .admission import AdmissionError
from .benchmark import inspect_scenario, load_scenarios
from .schemas import RunReceipt


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

    return router
