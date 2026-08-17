from __future__ import annotations

import json
import logging
import os
from functools import cache
from typing import Literal, Protocol

from google import genai
from google.genai.types import GenerateContentConfig
from opentelemetry import trace
from pydantic import BaseModel

from .repairs import CandidateCatalogue
from .schemas import DriftReport

MODEL = "gemini-3.5-flash-lite"
INSTRUCTION = """You are a conservative safety preflight for a bounded
structural data-pipeline repair. Inspect current_failure and the authorized
candidate summaries. Decide CONTINUE when a single candidate or a multi-step
combination can explain every failure. In particular, format, path, rename and
cast candidates may compose, and one split candidate can explain required,
type and preserve-value failures for multiple outputs. Ignore irrelevant
parsers for fields that are not failing. Decide ESCALATE when one failing field
has equivalent competing parsers or mappings, transform has no matching
candidate, a stable key is duplicated, or protected values changed without a
split candidate. If uncertain, CONTINUE. You cannot approve a mutation. Return
JSON only."""
_tracer = trace.get_tracer("driftpatch.risk_critic")


class RiskVerdict(BaseModel):
    decision: Literal["escalate", "continue"]


class RiskCritic(Protocol):
    async def confirms_escalation(
        self, report: DriftReport, catalogue: CandidateCatalogue
    ) -> bool: ...


class VertexRiskCritic:
    def __init__(self) -> None:
        self._client = genai.Client(
            vertexai=True,
            project=os.environ["GOOGLE_CLOUD_PROJECT"],
            location=os.getenv("GOOGLE_CLOUD_LOCATION", "global"),
        )

    async def confirms_escalation(
        self, report: DriftReport, catalogue: CandidateCatalogue
    ) -> bool:
        with _tracer.start_as_current_span("driftpatch.risk_preflight") as span:
            span.set_attribute("gen_ai.system", "google")
            span.set_attribute("gen_ai.request.model", MODEL)
            sanitized = report.model_copy(deep=True)
            for profile in (sanitized.before, sanitized.after):
                for field in profile.fields:
                    field.example_values = []
            payload = {
                "report": sanitized.model_dump(mode="json"),
                "candidates": [
                    {"id": candidate.id, "summary": candidate.summary}
                    for candidate in catalogue
                ],
            }
            try:
                response = await self._client.aio.models.generate_content(
                    model=MODEL,
                    contents=INSTRUCTION
                    + json.dumps(payload, separators=(",", ":"), sort_keys=True),
                    config=GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema={
                            "type": "object",
                            "properties": {
                                "decision": {
                                    "type": "string",
                                    "enum": ["escalate", "continue"],
                                }
                            },
                            "required": ["decision"],
                            "additionalProperties": False,
                        },
                        temperature=0,
                        max_output_tokens=64,
                    ),
                )
                verdict = RiskVerdict.model_validate_json(response.text or "")
                confirmed = verdict.decision == "escalate"
            except Exception:
                logging.warning("risk preflight unavailable")
                span.set_attribute("driftpatch.risk.available", False)
                span.set_attribute("driftpatch.risk.escalation_confirmed", False)
                return False
            span.set_attribute("driftpatch.risk.available", True)
            span.set_attribute("driftpatch.risk.escalation_confirmed", confirmed)
            return confirmed


@cache
def configured_risk_critic() -> RiskCritic:
    return VertexRiskCritic()
