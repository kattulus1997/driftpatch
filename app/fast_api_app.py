# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import contextlib
import json
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path

from a2a.server.tasks import InMemoryTaskStore
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.runners import Runner

from app.app_utils import services
from app.app_utils.a2a import attach_a2a_routes
from app.app_utils.typing import Feedback
from app.public_api import router as public_router

load_dotenv()
logger = logging.getLogger(__name__)
allow_origins = (
    os.getenv("ALLOW_ORIGINS", "").split(",") if os.getenv("ALLOW_ORIGINS") else None
)

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIST = Path(AGENT_DIR) / "frontend" / "dist"


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from app.agent import app as adk_app
    from app.agent import root_agent

    runner = Runner(
        app=adk_app,
        session_service=services.get_session_service(),
        artifact_service=services.get_artifact_service(),
        auto_create_session=True,
    )
    app.state.runner = runner
    app.state.agent_app_name = adk_app.name
    await attach_a2a_routes(
        app,
        agent=root_agent,
        runner=runner,
        task_store=InMemoryTaskStore(),
        rpc_path=f"/a2a/{adk_app.name}",
    )
    yield


app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=os.getenv("ADK_DEV_UI", "false").lower() == "true",
    artifact_service_uri=services.ARTIFACT_SERVICE_URI,
    allow_origins=allow_origins,
    session_service_uri=services.SESSION_SERVICE_URI,
    otel_to_cloud=True,
    lifespan=lifespan,
    trigger_sources=["pubsub"],
)
app.title = "driftpatch"
app.description = "API for interacting with the Agent driftpatch"
app.include_router(public_router)

app.mount(
    "/assets",
    StaticFiles(directory=FRONTEND_DIST / "assets", check_dir=False),
    name="frontend-assets",
)


def _frontend_file(name: str) -> FileResponse:
    path = FRONTEND_DIST / name
    if not path.is_file():
        raise HTTPException(status_code=503, detail="Frontend build is unavailable")
    return FileResponse(path)


@app.get("/", include_in_schema=False)
def demo() -> FileResponse:
    return _frontend_file("index.html")


@app.get("/og-driftpatch.png", include_in_schema=False)
def social_preview() -> FileResponse:
    return _frontend_file("og-driftpatch.png")


@app.get("/favicon.svg", include_in_schema=False)
def favicon() -> FileResponse:
    return _frontend_file("favicon.svg")


@app.post("/feedback")
def collect_feedback(feedback: Feedback) -> dict[str, str]:
    """Collect and log feedback.

    Args:
        feedback: The feedback data to log

    Returns:
        Success message
    """
    logger.info(json.dumps({"event": "feedback", **feedback.model_dump()}))
    return {"status": "success"}


# Main execution
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
