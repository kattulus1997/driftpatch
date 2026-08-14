from __future__ import annotations

import json

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RequestBodyLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        max_bytes: int,
        strict_json_paths: tuple[str, ...] = (),
    ) -> None:
        self._app = app
        self._max_bytes = max_bytes
        self._strict_json_paths = frozenset(strict_json_paths)

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                if int(content_length) > self._max_bytes:
                    await self._reject(scope, receive, send)
                    return
            except ValueError:
                await self._reject(scope, receive, send)
                return

        received = 0
        messages: list[Message] = []
        while True:
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max_bytes:
                    await self._reject(scope, receive, send)
                    return
                messages.append(message)
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                return

        async def replay_receive() -> Message:
            if messages:
                return messages.pop(0)
            return {"type": "http.disconnect"}

        if (
            scope.get("method") == "POST"
            and scope.get("path") in self._strict_json_paths
        ):
            body = b"".join(message.get("body", b"") for message in messages)
            problem = self._strict_json_problem(body)
            if problem is not None:
                code, detail = problem
                response = JSONResponse(
                    status_code=400,
                    content={"detail": {"code": code, "message": detail}},
                )
                await response(scope, replay_receive, send)
                return

        await self._app(scope, replay_receive, send)

    @staticmethod
    def _strict_json_problem(body: bytes) -> tuple[str, str] | None:
        class DuplicateKey(ValueError):
            pass

        def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
            value: dict[str, object] = {}
            for key, item in pairs:
                if key in value:
                    raise DuplicateKey
                value[key] = item
            return value

        try:
            payload = json.loads(
                body.decode("utf-8", errors="strict"),
                object_pairs_hook=object_pairs,
            )
        except DuplicateKey:
            return "duplicate_json_key", "The request contains a duplicate JSON key."
        except (UnicodeDecodeError, json.JSONDecodeError):
            return "invalid_json", "The custom-run request is not valid JSON."

        def valid_unicode(value: object) -> bool:
            if isinstance(value, str):
                try:
                    value.encode("utf-8", errors="strict")
                except UnicodeEncodeError:
                    return False
            if isinstance(value, dict):
                return all(
                    valid_unicode(key) and valid_unicode(item)
                    for key, item in value.items()
                )
            if isinstance(value, list):
                return all(valid_unicode(item) for item in value)
            return True

        if not valid_unicode(payload):
            return "invalid_unicode", "The request contains invalid Unicode."
        return None

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=413,
            content={"detail": "Request body exceeds the API limit."},
        )
        await response(scope, receive, send)
