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

import json
import os
from uuid import uuid4

import pytest
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agent import root_agent
from app.schemas import ValidationResult


@pytest.mark.live
@pytest.mark.skipif(
    os.getenv("DRIFTPATCH_LIVE_MODEL_TESTS", "").lower() != "true",
    reason="Set DRIFTPATCH_LIVE_MODEL_TESTS=true to run live model tests",
)
@pytest.mark.asyncio
async def test_agent_stream() -> None:
    session_service = InMemorySessionService()
    session = await session_service.create_session(user_id="test_user", app_name="test")
    runner = Runner(agent=root_agent, session_service=session_service, app_name="test")

    message = types.Content(
        role="user",
        parts=[
            types.Part.from_text(
                text=json.dumps(
                    {
                        "scenario_id": "column-rename",
                        "attributes": {
                            "event_id": str(uuid4()),
                            "trigger": "integration",
                        },
                    }
                )
            )
        ],
    )

    try:
        events = [
            event
            async for event in runner.run_async(
            new_message=message,
            user_id="test_user",
            session_id=session.id,
            run_config=RunConfig(streaming_mode=StreamingMode.SSE),
        )
        ]
    finally:
        await runner.close()

    terminal = [event.output for event in events if isinstance(event.output, ValidationResult)]
    assert terminal
    assert all(result == terminal[0] for result in terminal)
    assert terminal[-1].status == "repaired"
    assert terminal[-1].plan.operation == "update_field_sources"
    assert terminal[-1].checks and all(check.passed for check in terminal[-1].checks)
