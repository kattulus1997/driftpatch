from __future__ import annotations

import asyncio
from typing import Any

import httpx
from google.auth.transport.requests import Request
from google.oauth2.id_token import fetch_id_token


class ServiceRequestError(RuntimeError):
    def __init__(
        self,
        detail: str,
        *,
        status_code: int = 503,
        code: str | None = None,
    ) -> None:
        self.detail = detail
        self.status_code = status_code
        self.code = code
        super().__init__(detail)


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
            if response.is_error:
                try:
                    problem = response.json().get("detail", {})
                except (AttributeError, ValueError):
                    problem = {}
                if isinstance(problem, dict):
                    code = problem.get("code")
                    detail = problem.get("message", "private service rejected request")
                else:
                    code = None
                    detail = "private service rejected request"
                raise ServiceRequestError(
                    detail,
                    status_code=response.status_code,
                    code=code,
                )
            result = response.json()
            if not isinstance(result, dict):
                raise ServiceRequestError("private service returned an invalid response")
            return result
    except ServiceRequestError:
        raise
    except Exception as exc:
        raise ServiceRequestError("private service request failed") from exc
