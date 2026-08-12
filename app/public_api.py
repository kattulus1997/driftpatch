from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from google.genai import types

from .benchmark import inspect_scenario, load_scenarios
from .ledger import list_runs
from .schemas import ValidationResult


router = APIRouter(prefix="/api", tags=["DriftPatch demo"])
_run_lock = asyncio.Lock()
_requests: dict[str, deque[float]] = defaultdict(deque)


def _check_rate_limit(request: Request) -> None:
    client = request.client.host if request.client else "unknown"
    now = time.monotonic()
    history = _requests[client]
    while history and history[0] < now - 3600:
        history.popleft()
    if len(history) >= 20:
        raise HTTPException(status_code=429, detail="Public demo limit reached; retry later.")
    history.append(now)


@router.get("/scenarios")
def scenarios() -> dict:
    items = []
    for scenario in load_scenarios():
        report = inspect_scenario(scenario)
        items.append(
            {
                "id": scenario.id,
                "title": scenario.title,
                "expected_status": scenario.expected_status,
                "report": report.model_dump(mode="json"),
            }
        )
    return {
        "summary": {
            "decisions": 10,
            "repaired": 8,
            "escalated": 2,
            "auto_merges": 0,
        },
        "items": items,
    }


@router.get("/runs")
async def runs() -> dict:
    return {"items": await list_runs()}


@router.post("/scenarios/{scenario_id}/run", response_model=ValidationResult)
async def run_scenario(scenario_id: str, request: Request) -> ValidationResult:
    _check_rate_limit(request)
    if scenario_id not in {scenario.id for scenario in load_scenarios()}:
        raise HTTPException(status_code=404, detail="Unknown incident")

    runner = request.app.state.runner
    session_service = runner.session_service
    user_id = "public-demo"
    event_id = str(uuid4())
    session = await session_service.create_session(
        app_name="app", user_id=user_id, session_id=event_id
    )
    message = types.Content(
        role="user",
        parts=[
            types.Part.from_text(
                text=(
                    '{"data":{"scenario_id":"'
                    + scenario_id
                    + '"},"attributes":{"event_id":"'
                    + event_id
                    + '","trigger":"web-demo"}}'
                )
            )
        ],
    )

    result = None
    try:
        async with _run_lock, asyncio.timeout(90):
            async for event in runner.run_async(
                user_id=user_id,
                session_id=session.id,
                new_message=message,
            ):
                if event.output:
                    result = event.output
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="Agent run timed out") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Agent run failed") from exc
    finally:
        await session_service.delete_session(
            app_name="app", user_id=user_id, session_id=session.id
        )

    if result is None:
        raise HTTPException(status_code=502, detail="Agent returned no validated result")
    return ValidationResult.model_validate(result)
