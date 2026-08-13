from __future__ import annotations

import asyncio
from typing import Any

import httpx
from google.auth.transport.requests import Request
from google.oauth2.id_token import fetch_id_token


class ServiceRequestError(RuntimeError):
    pass


async def request_json(
    method: str,
    url: str,
    *,
    audience: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        token = await asyncio.to_thread(fetch_id_token, Request(), audience)
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.request(
                method,
                url,
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
            response.raise_for_status()
            return response.json()
    except Exception as exc:
        raise ServiceRequestError("private service request failed") from exc
