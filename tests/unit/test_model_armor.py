from __future__ import annotations

import httpx
import pytest

from app.model_armor import ModelArmorScreen, configured_safety_screen

TEMPLATE = "projects/test-project/locations/europe-west1/templates/driftpatch"


class _Credentials:
    valid = True
    token = "test-access-token"

    def refresh(self, _request) -> None:
        raise AssertionError("valid credentials must not be refreshed")


def _screen(response: dict, requests: list[httpx.Request]) -> ModelArmorScreen:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=response)

    return ModelArmorScreen(
        TEMPLATE,
        credentials=_Credentials(),
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_explicit_success_and_no_match_is_the_only_allow_verdict() -> None:
    requests: list[httpx.Request] = []
    screen = _screen(
        {
            "sanitizationResult": {
                "invocationResult": "SUCCESS",
                "filterMatchState": "NO_MATCH_FOUND",
                "filterResults": {},
            }
        },
        requests,
    )

    verdict = await screen.screen_prompt("bounded structural profile")

    assert verdict.allowed is True
    assert verdict.reason == "no_match"
    assert len(requests) == 1
    assert requests[0].url == (
        "https://modelarmor.europe-west1.rep.googleapis.com/v1/"
        f"{TEMPLATE}:sanitizeUserPrompt"
    )
    assert requests[0].headers["authorization"] == "Bearer test-access-token"
    assert requests[0].read() == b'{"userPromptData":{"text":"bounded structural profile"}}'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "reason"),
    [
        (
            {"invocationResult": "SUCCESS", "filterMatchState": "MATCH_FOUND"},
            "policy_match",
        ),
        (
            {"invocationResult": "PARTIAL", "filterMatchState": "NO_MATCH_FOUND"},
            "incomplete_scan",
        ),
        ({"filterMatchState": "NO_MATCH_FOUND"}, "malformed_response"),
    ],
)
async def test_match_partial_or_malformed_responses_fail_closed(
    result: dict, reason: str
) -> None:
    screen = _screen({"sanitizationResult": result}, [])

    verdict = await screen.screen_prompt("profile")

    assert verdict.allowed is False
    assert verdict.reason == reason


@pytest.mark.asyncio
async def test_transport_failure_blocks_without_exposing_the_input() -> None:
    def fail(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("credential-bearing detail")

    screen = ModelArmorScreen(
        TEMPLATE,
        credentials=_Credentials(),
        transport=httpx.MockTransport(fail),
    )

    verdict = await screen.screen_prompt("private profile")

    assert verdict.allowed is False
    assert verdict.reason == "screen_unavailable"
    assert "private profile" not in verdict.model_dump_json()
    assert "credential" not in verdict.model_dump_json()


@pytest.mark.asyncio
async def test_model_response_uses_the_distinct_official_method_and_payload() -> None:
    requests: list[httpx.Request] = []
    screen = _screen(
        {
            "sanitizationResult": {
                "invocationResult": "SUCCESS",
                "filterMatchState": "NO_MATCH_FOUND",
            }
        },
        requests,
    )

    verdict = await screen.screen_response('{"candidate_ids":[]}')

    assert verdict.allowed is True
    assert requests[0].url.path.endswith(":sanitizeModelResponse")
    assert requests[0].read() == (
        b'{"modelResponseData":{"text":"{\\"candidate_ids\\":[]}"}}'
    )


@pytest.mark.asyncio
async def test_cloud_without_model_armor_configuration_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("K_SERVICE", "driftpatch-worker")
    monkeypatch.delenv("MODEL_ARMOR_TEMPLATE", raising=False)

    verdict = await configured_safety_screen().screen_prompt("bounded profile")

    assert verdict.allowed is False
    assert verdict.reason == "configuration_missing"


@pytest.mark.asyncio
async def test_local_execution_uses_a_content_free_permitter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("MODEL_ARMOR_TEMPLATE", raising=False)

    verdict = await configured_safety_screen().screen_prompt("local test profile")

    assert verdict.allowed is True
    assert verdict.reason == "local_only"
    assert "profile" not in verdict.model_dump_json()
