from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.admission import (
    AdmissionClient,
    AdmissionError,
    AdmissionRejected,
    AdmissionService,
    DirectAdmissionClient,
    configured_admission_client,
)
from app.benchmark import load_scenario, scenario_case
from app.bundle_store import BundleStore, configured_bundle_store
from app.case_data import MAX_REQUEST_BYTES, parse_submission
from app.event_delivery import EventPublisher, configured_publisher
from app.execution import ExecutionBinding, bind_execution
from app.http_limits import RequestBodyLimitMiddleware
from app.ledger import EventStore, configured_event_store
from app.live_source import (
    LiveSourceError,
    LiveSourceWatcher,
    SourceReader,
    configured_source_reader,
)
from app.public_api import create_public_router
from app.result_delivery import ResultPublisher, configured_result_publisher
from app.result_service import DirectResultPublisher, ProposalRejected, ResultService
from app.schemas import CustomRunSubmission, TaskRequest, WorkerProposal
from app.task_handler import run_task
from app.telemetry import configure_cloud_tracing, shutdown_cloud_tracing

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"
PRIVATE_BODY_LIMIT = 64 * 1024
_local_logger = logging.getLogger("driftpatch.local")


def _frontend_file(name: str) -> FileResponse:
    path = FRONTEND_DIST / name
    if not path.is_file():
        raise HTTPException(status_code=503, detail="Frontend build is unavailable")
    return FileResponse(path)


def create_public_app(
    *,
    admission: AdmissionClient | None = None,
    lifespan: Callable[[FastAPI], contextlib.AbstractAsyncContextManager[None]]
    | None = None,
) -> FastAPI:
    application = FastAPI(
        title="driftpatch",
        description="Public proof interface for DriftPatch",
        lifespan=lifespan,
    )
    application.state.admission = admission or configured_admission_client()
    application.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=MAX_REQUEST_BYTES,
        strict_json_paths=("/api/runs",),
    )
    application.include_router(create_public_router())

    @application.exception_handler(RequestValidationError)
    async def invalid_public_request(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        status_code = (
            400
            if any(error.get("type") == "json_invalid" for error in exc.errors())
            else 422
        )
        return JSONResponse(
            status_code=status_code,
            content={
                "detail": {
                    "code": "invalid_json" if status_code == 400 else "invalid_request",
                    "message": "The custom-run request is malformed.",
                }
            },
        )
    application.mount(
        "/assets",
        StaticFiles(directory=FRONTEND_DIST / "assets", check_dir=False),
        name="frontend-assets",
    )

    @application.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok", "role": "public"}

    @application.get("/", include_in_schema=False)
    def demo() -> FileResponse:
        return _frontend_file("index.html")

    @application.get("/og-driftpatch.png", include_in_schema=False)
    def social_preview() -> FileResponse:
        return _frontend_file("og-driftpatch.png")

    @application.get("/favicon.svg", include_in_schema=False)
    def favicon() -> FileResponse:
        return _frontend_file("favicon.svg")

    @application.get("/favicon.ico", include_in_schema=False)
    def legacy_favicon() -> FileResponse:
        return _frontend_file("favicon.svg")

    return application


def create_admission_app(
    *,
    publisher: EventPublisher | None = None,
    store: EventStore | None = None,
    source_reader: SourceReader | None = None,
    bundles: BundleStore | None = None,
) -> FastAPI:
    application = FastAPI(
        title="driftpatch-admission",
        description="Private bounded admission service for DriftPatch",
    )
    service = AdmissionService(
        store or configured_event_store(),
        publisher or configured_publisher(),
        bundles or configured_bundle_store(),
        daily_custom_limit=int(os.getenv("CUSTOM_DAILY_LIMIT", "24")),
        daily_total_limit=int(os.getenv("CUSTOM_TOTAL_DAILY_LIMIT", "48")),
    )
    application.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=MAX_REQUEST_BYTES,
    )

    @application.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok", "role": "admission"}

    @application.post("/internal/runs", status_code=status.HTTP_202_ACCEPTED)
    async def start_custom(submission: CustomRunSubmission) -> dict:
        try:
            return await service.start_custom(submission)
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
                    "message": "Custom-run admission is unavailable.",
                },
            ) from exc

    @application.get("/internal/runs/{run_id}")
    async def get_custom(run_id: str, response: Response) -> dict:
        try:
            result = await service.get_custom(run_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Unknown custom run") from exc
        if result.get("status") == "queued":
            response.status_code = status.HTTP_202_ACCEPTED
        return result

    @application.post("/internal/scenarios/{scenario_id}/run")
    async def start(scenario_id: str) -> dict:
        try:
            return await service.start(scenario_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Unknown incident") from exc
        except AdmissionError as exc:
            raise HTTPException(status_code=503, detail="Admission unavailable") from exc

    @application.get("/internal/scenarios/{scenario_id}/run")
    async def get(scenario_id: str) -> dict:
        try:
            return await service.get(scenario_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Unknown incident") from exc

    @application.post("/internal/watch")
    async def watch() -> dict:
        try:
            reader = source_reader or configured_source_reader()
            return await LiveSourceWatcher(reader, service).watch()
        except LiveSourceError as exc:
            raise HTTPException(status_code=503, detail="Live source unavailable") from exc
        except AdmissionError as exc:
            raise HTTPException(status_code=503, detail="Admission unavailable") from exc

    return application


def create_result_app(
    *,
    store: EventStore | None = None,
    bundles: BundleStore | None = None,
) -> FastAPI:
    application = FastAPI(
        title="driftpatch-result",
        description="Private deterministic result service for DriftPatch",
    )
    service = ResultService(
        store or configured_event_store(),
        bundles or configured_bundle_store(),
    )
    application.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=PRIVATE_BODY_LIMIT,
    )

    @application.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok", "role": "result"}

    @application.post("/internal/attempts/preflight")
    async def preflight(task: TaskRequest) -> dict:
        try:
            return (await service.preflight(task)).model_dump(mode="json")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid attempt identity") from exc

    @application.post("/internal/results")
    async def complete(proposal: WorkerProposal) -> dict:
        try:
            return await service.complete(proposal)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid proposal identity") from exc
        except ProposalRejected as exc:
            raise HTTPException(status_code=422, detail="Proposal rejected") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail="Proposal was not admitted") from exc

    @application.post("/internal/reconcile")
    async def reconcile() -> dict[str, int]:
        terminals = await service.reconcile_stale()
        return {"terminalized": len(terminals)}

    return application


class _LocalTaskPublisher:
    def __init__(self, application: FastAPI, result_publisher: ResultPublisher) -> None:
        self._application = application
        self._result_publisher = result_publisher

    async def publish(self, request: TaskRequest) -> None:
        task = asyncio.create_task(
            _run_local_incident(
                self._application,
                self._result_publisher,
                request=request,
            )
        )
        self._application.state.local_tasks.add(task)

        def finish(completed: asyncio.Task[None]) -> None:
            self._application.state.local_tasks.discard(completed)
            if completed.cancelled():
                return
            error = completed.exception()
            if error is not None:
                _local_logger.error(
                    "Local worker failed for %s",
                    request.case_id,
                    exc_info=(type(error), error, error.__traceback__),
                )

        task.add_done_callback(finish)


async def _run_local_incident(
    application: FastAPI,
    result_publisher: ResultPublisher,
    *,
    request: TaskRequest,
) -> None:
    from google.genai import types

    message = types.Content(
        role="user",
        parts=[
            types.Part.from_text(
                text=json.dumps(
                    {
                        "case_kind": request.case_kind,
                        "case_id": request.case_id,
                        "attributes": {
                            "event_id": request.event_id,
                            "issued_day": request.issued_day,
                            "trigger": "local-demo",
                        },
                    }
                )
            )
        ],
    )
    lease = await result_publisher.preflight(request)
    if lease.disposition != "run" or lease.execution_token is None:
        return
    if request.case_kind == "fixture":
        case = scenario_case(load_scenario(request.case_id))
    else:
        if request.bundle is None:
            raise RuntimeError("custom local task has no bundle")
        submission = await configured_bundle_store().get(request.bundle)
        case = parse_submission(submission, case_id=request.case_id)
    execution = ExecutionBinding(
        event_id=request.event_id,
        issued_day=request.issued_day,
        attempt_id=request.attempt_id,
        execution_token=lease.execution_token,
        publisher=result_publisher,
        case_kind=request.case_kind,
        case_id=request.case_id,
        bundle=request.bundle,
        case=case,
    )
    with bind_execution(execution):
        async for _ in application.state.local_runner.run_async(
            user_id="local-demo",
            session_id=request.event_id,
            new_message=message,
        ):
            pass
    if not execution.published:
        raise RuntimeError("local worker completed without publishing a proposal")


@contextlib.asynccontextmanager
async def _runner_lifespan(
    application: FastAPI,
    *,
    state_name: str,
) -> AsyncIterator[None]:
    from google.adk.artifacts import InMemoryArtifactService
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService

    from app.agent import app as adk_app

    runner = Runner(
        app=adk_app,
        session_service=InMemorySessionService(),
        artifact_service=InMemoryArtifactService(),
        auto_create_session=True,
    )
    setattr(application.state, state_name, runner)
    try:
        yield
    finally:
        await runner.close()


@contextlib.asynccontextmanager
async def _development_lifespan(application: FastAPI) -> AsyncIterator[None]:
    application.state.local_tasks = set()
    async with _runner_lifespan(application, state_name="local_runner"):
        try:
            yield
        finally:
            tasks = list(application.state.local_tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def create_development_app() -> FastAPI:
    store = configured_event_store()
    result_publisher = DirectResultPublisher(ResultService(store))
    application = create_public_app(
        admission=None,
        lifespan=_development_lifespan,
    )
    publisher = _LocalTaskPublisher(application, result_publisher)
    application.state.admission = DirectAdmissionClient(
        AdmissionService(store, publisher)
    )
    return application


@contextlib.asynccontextmanager
async def _worker_lifespan(application: FastAPI) -> AsyncIterator[None]:
    configure_cloud_tracing()
    try:
        async with _runner_lifespan(application, state_name="runner"):
            yield
    finally:
        shutdown_cloud_tracing()


def create_worker_app(
    *,
    result_publisher: ResultPublisher | None = None,
    bundles: BundleStore | None = None,
) -> FastAPI:
    application = FastAPI(
        title="driftpatch-worker",
        description="Private task worker for DriftPatch",
        lifespan=_worker_lifespan,
    )
    application.state.result_publisher = (
        result_publisher or configured_result_publisher()
    )
    application.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=PRIVATE_BODY_LIMIT,
    )
    application.state.task_queue = os.getenv("CLOUD_TASKS_QUEUE", "driftpatch-worker")
    application.state.bundle_store = bundles or configured_bundle_store()

    @application.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok", "role": "worker"}

    @application.post("/tasks/run", status_code=204)
    async def task(request: Request, payload: TaskRequest):
        return await run_task(request, payload)

    return application


def create_app(role: str | None = None) -> FastAPI:
    selected = (role or os.getenv("SERVICE_ROLE", "public")).strip().lower()
    if selected == "public":
        return create_public_app()
    if selected == "admission":
        return create_admission_app()
    if selected == "worker":
        return create_worker_app()
    if selected == "result":
        return create_result_app()
    if selected == "development":
        return create_development_app()
    raise RuntimeError(
        "SERVICE_ROLE must be 'public', 'admission', 'worker', 'result' or 'development'"
    )


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
