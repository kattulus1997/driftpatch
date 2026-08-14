from __future__ import annotations

import asyncio
import json
import logging
import os
from functools import cache
from typing import Protocol

from .event_identity import task_id
from .schemas import TaskRequest

LOGGER = logging.getLogger("uvicorn.error")


class EventDeliveryError(RuntimeError):
    """Raised when a bounded incident cannot be queued."""


class EventPublisher(Protocol):
    async def publish(self, task: TaskRequest) -> None: ...


class UnconfiguredPublisher:
    async def publish(self, task: TaskRequest) -> None:
        del task
        raise EventDeliveryError("Cloud Tasks is not configured")


class CloudTasksEventPublisher:
    def __init__(
        self,
        *,
        project: str,
        location: str,
        queue: str,
        worker_url: str,
        invoker_service_account: str,
    ) -> None:
        from google.cloud import tasks_v2

        self._client = None
        self._parent = tasks_v2.CloudTasksClient.queue_path(project, location, queue)
        self._worker_url = worker_url.rstrip("/")
        self._invoker_service_account = invoker_service_account

    async def publish(self, request: TaskRequest) -> None:
        from google.api_core.exceptions import AlreadyExists
        from google.cloud import tasks_v2

        payload = json.dumps(
            request.model_dump(mode="json"),
            separators=(",", ":"),
        ).encode()
        task = tasks_v2.Task(
            name=f"{self._parent}/tasks/{task_id(request.event_id, request.attempt_id)}",
            http_request=tasks_v2.HttpRequest(
                http_method=tasks_v2.HttpMethod.POST,
                url=f"{self._worker_url}/tasks/run",
                headers={"Content-Type": "application/json"},
                body=payload,
                oidc_token=tasks_v2.OidcToken(
                    service_account_email=self._invoker_service_account,
                    audience=self._worker_url,
                ),
            ),
        )
        try:
            def create_task() -> None:
                client = self._client or tasks_v2.CloudTasksClient(transport="rest")
                try:
                    client.create_task(parent=self._parent, task=task)
                finally:
                    if self._client is None:
                        client.transport.close()

            await asyncio.to_thread(create_task)
        except AlreadyExists:
            return
        except Exception as exc:
            LOGGER.error(
                "Cloud Tasks enqueue failed: %s",
                type(exc).__name__,
            )
            raise EventDeliveryError("Cloud Tasks enqueue failed") from exc


@cache
def configured_publisher() -> EventPublisher:
    project = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
    location = os.getenv("CLOUD_TASKS_LOCATION", "").strip()
    queue = os.getenv("CLOUD_TASKS_QUEUE", "").strip()
    worker_url = os.getenv("WORKER_URL", "").strip()
    invoker = os.getenv("TASK_INVOKER_SERVICE_ACCOUNT", "").strip()
    if not all((project, location, queue, worker_url, invoker)):
        return UnconfiguredPublisher()
    return CloudTasksEventPublisher(
        project=project,
        location=location,
        queue=queue,
        worker_url=worker_url,
        invoker_service_account=invoker,
    )
