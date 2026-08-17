from __future__ import annotations

import asyncio
import logging
import math
import os
from functools import cache
from typing import Protocol

from google import genai
from google.genai.types import EmbedContentConfig
from opentelemetry import trace

from .case_data import RepairCase
from .repairs import CandidateCatalogue

MODEL = "gemini-embedding-001"
MAX_CANDIDATES = 16
OUTPUT_DIMENSIONS = 256
MIN_SIMILARITY = 0.8
MIN_MARGIN = 0.01
_tracer = trace.get_tracer("driftpatch.lineage")


class TextEmbedder(Protocol):
    async def embed(self, text: str) -> list[float]: ...


class LineageRanker(Protocol):
    async def score(
        self, case: RepairCase, catalogue: CandidateCatalogue
    ) -> dict[str, float]: ...


class VertexTextEmbedder:
    def __init__(self) -> None:
        project = os.environ["GOOGLE_CLOUD_PROJECT"]
        location = os.getenv("GOOGLE_CLOUD_LOCATION", "global")
        self._client = genai.Client(
            vertexai=True,
            project=project,
            location=location,
        )

    async def embed(self, text: str) -> list[float]:
        response = await self._client.aio.models.embed_content(
            model=MODEL,
            contents=text,
            config=EmbedContentConfig(
                task_type="SEMANTIC_SIMILARITY",
                output_dimensionality=OUTPUT_DIMENSIONS,
            ),
        )
        return list(response.embeddings[0].values)


def cosine_similarity(left: list[float], right: list[float]) -> float | None:
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return None
    return numerator / (left_norm * right_norm)


class SemanticLineageRanker:
    def __init__(self, embedder: TextEmbedder) -> None:
        self._embedder = embedder

    async def score(
        self, case: RepairCase, catalogue: CandidateCatalogue
    ) -> dict[str, float]:
        with _tracer.start_as_current_span("driftpatch.semantic_lineage") as span:
            span.set_attribute("gen_ai.system", "google")
            span.set_attribute("gen_ai.request.model", MODEL)
            return await self._score(case, catalogue, span)

    async def _score(self, case, catalogue, span) -> dict[str, float]:
        pairs: list[tuple[str, str, str]] = []
        for candidate in catalogue:
            if candidate.step.operation != "update_field_sources":
                continue
            updates = candidate.step.field_sources
            legacy = "; ".join(
                f"output field {item.output_field}; legacy source field "
                f"{case.pipeline.fields[item.output_field]}"
                for item in updates
            )
            observed = "; ".join(
                f"observed replacement field {item.source_field}" for item in updates
            )
            pairs.append((candidate.id, legacy, observed))
            if len(pairs) == MAX_CANDIDATES:
                break
        if len(pairs) < 2:
            span.set_attribute("driftpatch.lineage.candidates", len(pairs))
            span.set_attribute("driftpatch.lineage.hint_emitted", False)
            return {}
        span.set_attribute("driftpatch.lineage.candidates", len(pairs))

        texts = tuple(sorted({text for _, *items in pairs for text in items}))
        semaphore = asyncio.Semaphore(4)

        async def embed(text: str) -> tuple[str, list[float]]:
            async with semaphore:
                return text, await self._embedder.embed(text)

        try:
            vectors = dict(await asyncio.gather(*(embed(text) for text in texts)))
        except Exception:
            logging.warning("semantic lineage scoring unavailable")
            span.set_attribute("driftpatch.lineage.available", False)
            span.set_attribute("driftpatch.lineage.hint_emitted", False)
            return {}

        try:
            scores = {
                candidate_id: round(similarity, 6)
                for candidate_id, legacy, observed in pairs
                if (similarity := cosine_similarity(vectors[legacy], vectors[observed]))
                is not None
            }
            ranked = sorted(scores, key=lambda candidate_id: scores[candidate_id], reverse=True)
        except Exception:
            logging.warning("semantic lineage scoring unavailable")
            span.set_attribute("driftpatch.lineage.available", False)
            span.set_attribute("driftpatch.lineage.hint_emitted", False)
            return {}
        if len(ranked) < 2:
            span.set_attribute("driftpatch.lineage.hint_emitted", False)
            return {}
        best, runner_up = ranked[:2]
        margin = scores[best] - scores[runner_up]
        span.set_attribute("driftpatch.lineage.available", True)
        span.set_attribute("driftpatch.lineage.margin", margin)
        if scores[best] < MIN_SIMILARITY or margin < MIN_MARGIN:
            span.set_attribute("driftpatch.lineage.hint_emitted", False)
            return {}
        span.set_attribute("driftpatch.lineage.hint_emitted", True)
        return {best: scores[best]}


@cache
def configured_lineage_ranker() -> LineageRanker:
    return SemanticLineageRanker(VertexTextEmbedder())
