from __future__ import annotations

import asyncio
import os
import re
from typing import Protocol

import google.auth
import httpx
from google.auth.credentials import Credentials
from google.auth.transport.requests import Request

from .schemas import ExternalModel, ShortText

_TEMPLATE = re.compile(
    r"^projects/[A-Za-z0-9-]+/locations/europe-west1/templates/[A-Za-z0-9_-]+$"
)
_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_ENDPOINT = "https://modelarmor.europe-west1.rep.googleapis.com"


class SafetyVerdict(ExternalModel):
    allowed: bool
    reason: ShortText


class SafetyScreen(Protocol):
    async def screen_prompt(self, text: str) -> SafetyVerdict: ...

    async def screen_response(self, text: str) -> SafetyVerdict: ...


class LocalSafetyScreen:
    async def screen_prompt(self, _text: str) -> SafetyVerdict:
        return SafetyVerdict(allowed=True, reason="local_only")

    async def screen_response(self, _text: str) -> SafetyVerdict:
        return SafetyVerdict(allowed=True, reason="local_only")

    async def screen(self, text: str) -> SafetyVerdict:
        return await self.screen_prompt(text)


class FailClosedSafetyScreen:
    def __init__(self, reason: str) -> None:
        self._reason = reason

    async def screen_prompt(self, _text: str) -> SafetyVerdict:
        return SafetyVerdict(allowed=False, reason=self._reason)

    async def screen_response(self, _text: str) -> SafetyVerdict:
        return SafetyVerdict(allowed=False, reason=self._reason)

    async def screen(self, text: str) -> SafetyVerdict:
        return await self.screen_prompt(text)


class ModelArmorScreen:
    def __init__(
        self,
        template: str,
        *,
        credentials: Credentials | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not _TEMPLATE.fullmatch(template):
            raise ValueError("Model Armor template must be in europe-west1")
        self._template = template
        self._credentials = credentials or google.auth.default(scopes=[_SCOPE])[0]
        self._transport = transport

    async def _access_token(self) -> str:
        if not self._credentials.valid or not self._credentials.token:
            await asyncio.to_thread(self._credentials.refresh, Request())
        token = self._credentials.token
        if not token:
            raise RuntimeError("credentials returned no access token")
        return token

    async def _sanitize(self, text: str, method: str, field: str) -> SafetyVerdict:
        try:
            token = await self._access_token()
            async with httpx.AsyncClient(
                timeout=10,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    f"{_ENDPOINT}/v1/{self._template}:{method}",
                    headers={"Authorization": f"Bearer {token}"},
                    json={field: {"text": text}},
                )
                response.raise_for_status()
                payload = response.json()
        except Exception:
            return SafetyVerdict(allowed=False, reason="screen_unavailable")

        if not isinstance(payload, dict):
            return SafetyVerdict(allowed=False, reason="malformed_response")
        result = payload.get("sanitizationResult")
        if not isinstance(result, dict):
            return SafetyVerdict(allowed=False, reason="malformed_response")
        invocation = result.get("invocationResult")
        match = result.get("filterMatchState")
        if not isinstance(invocation, str) or not isinstance(match, str):
            return SafetyVerdict(allowed=False, reason="malformed_response")
        if invocation != "SUCCESS":
            return SafetyVerdict(allowed=False, reason="incomplete_scan")
        if match == "MATCH_FOUND":
            return SafetyVerdict(allowed=False, reason="policy_match")
        if match != "NO_MATCH_FOUND":
            return SafetyVerdict(allowed=False, reason="malformed_response")
        return SafetyVerdict(allowed=True, reason="no_match")

    async def screen_prompt(self, text: str) -> SafetyVerdict:
        return await self._sanitize(
            text, "sanitizeUserPrompt", "userPromptData"
        )

    async def screen_response(self, text: str) -> SafetyVerdict:
        return await self._sanitize(
            text, "sanitizeModelResponse", "modelResponseData"
        )

    async def screen(self, text: str) -> SafetyVerdict:
        return await self.screen_prompt(text)


def configured_safety_screen() -> SafetyScreen:
    if not os.environ.get("K_SERVICE"):
        return LocalSafetyScreen()
    template = os.environ.get("MODEL_ARMOR_TEMPLATE")
    if not template:
        return FailClosedSafetyScreen("configuration_missing")
    try:
        return ModelArmorScreen(template)
    except Exception:
        return FailClosedSafetyScreen("configuration_invalid")
