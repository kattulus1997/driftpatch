import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.ledger import (
    ClaimDisposition,
    InMemoryEventStore,
)


@pytest.mark.asyncio
async def test_concurrent_claims_acquire_one_model_execution_lease() -> None:
    store = InMemoryEventStore()
    event_id = "concurrent-event"

    claims = await asyncio.gather(
        *(
            store.claim(
                event_id=event_id,
                scenario_id="column-rename",
                trigger="test",
                now=datetime.now(UTC),
                lease=timedelta(minutes=65),
            )
            for _ in range(20)
        )
    )

    assert sum(claim.disposition is ClaimDisposition.ACQUIRED for claim in claims) == 1
    assert sum(claim.disposition is ClaimDisposition.IN_PROGRESS for claim in claims) == 19
    assert len({claim.token for claim in claims if claim.token}) == 1


@pytest.mark.asyncio
async def test_execution_preflight_requires_the_matching_attempt_capability() -> None:
    store = InMemoryEventStore()
    now = datetime(2026, 8, 12, 15, 0, tzinfo=UTC)
    claim = await store.claim(
        event_id="claimed-event",
        scenario_id="column-rename",
        trigger="test",
        now=now,
        lease=timedelta(minutes=65),
    )
    assert claim.attempt_id is not None and claim.token is not None

    stale = await store.preflight(
        event_id="claimed-event",
        scenario_id="column-rename",
        attempt_id=claim.attempt_id,
        attempt_token="11111111-1111-4111-8111-111111111111",
        now=now,
        lease=timedelta(minutes=11),
    )
    active = await store.preflight(
        event_id="claimed-event",
        scenario_id="column-rename",
        attempt_id=claim.attempt_id,
        attempt_token=claim.token,
        now=now,
        lease=timedelta(minutes=11),
    )
    duplicate = await store.preflight(
        event_id="claimed-event",
        scenario_id="column-rename",
        attempt_id=claim.attempt_id,
        attempt_token=claim.token,
        now=now,
        lease=timedelta(minutes=11),
    )

    assert stale.disposition == "stale"
    assert active.disposition == "run"
    assert active.execution_token is not None
    assert duplicate.disposition == "busy"


@pytest.mark.asyncio
async def test_expired_claim_can_be_reacquired_with_a_new_token() -> None:
    store = InMemoryEventStore()
    now = datetime(2026, 8, 12, 15, 0, tzinfo=UTC)
    first = await store.claim(
        event_id="expired-event",
        scenario_id="column-rename",
        trigger="test",
        now=now,
        lease=timedelta(seconds=30),
    )
    second = await store.claim(
        event_id="expired-event",
        scenario_id="column-rename",
        trigger="test",
        now=now + timedelta(seconds=31),
        lease=timedelta(seconds=30),
    )

    assert first.disposition is ClaimDisposition.ACQUIRED
    assert second.disposition is ClaimDisposition.ACQUIRED
    assert first.token != second.token


@pytest.mark.asyncio
async def test_expired_claims_have_a_durable_daily_dispatch_ceiling() -> None:
    store = InMemoryEventStore()
    now = datetime(2026, 8, 12, 15, 0, tzinfo=UTC)

    first = await store.claim(
        event_id="bounded-event",
        scenario_id="column-rename",
        trigger="test",
        now=now,
        lease=timedelta(seconds=30),
    )
    second = await store.claim(
        event_id="bounded-event",
        scenario_id="column-rename",
        trigger="test",
        now=now + timedelta(seconds=31),
        lease=timedelta(seconds=30),
    )
    exhausted = await store.claim(
        event_id="bounded-event",
        scenario_id="column-rename",
        trigger="test",
        now=now + timedelta(seconds=62),
        lease=timedelta(seconds=30),
    )

    assert first.disposition is ClaimDisposition.ACQUIRED
    assert second.disposition is ClaimDisposition.ACQUIRED
    assert exhausted.disposition is ClaimDisposition.EXHAUSTED


@pytest.mark.asyncio
async def test_only_the_active_token_can_release_a_processing_claim() -> None:
    store = InMemoryEventStore()
    now = datetime(2026, 8, 12, 15, 0, tzinfo=UTC)
    first = await store.claim(
        event_id="retryable-event",
        scenario_id="column-rename",
        trigger="test",
        now=now,
        lease=timedelta(minutes=8),
    )

    assert await store.release(event_id="retryable-event", claim_token="wrong") is False
    assert first.token is not None
    assert await store.release(event_id="retryable-event", claim_token=first.token) is True

    second = await store.claim(
        event_id="retryable-event",
        scenario_id="column-rename",
        trigger="test",
        now=now,
        lease=timedelta(minutes=8),
    )
    assert second.disposition is ClaimDisposition.ACQUIRED
    assert second.token != first.token

    assert second.token is not None
    assert await store.release(event_id="retryable-event", claim_token=second.token)
    exhausted = await store.claim(
        event_id="retryable-event",
        scenario_id="column-rename",
        trigger="test",
        now=now,
        lease=timedelta(minutes=8),
    )
    assert exhausted.disposition is ClaimDisposition.EXHAUSTED


@pytest.mark.asyncio
async def test_event_identifier_cannot_be_rebound_to_another_scenario() -> None:
    store = InMemoryEventStore()
    now = datetime(2026, 8, 12, 15, 0, tzinfo=UTC)
    await store.claim(
        event_id="bound-event",
        scenario_id="column-rename",
        trigger="test",
        now=now,
        lease=timedelta(minutes=8),
    )

    with pytest.raises(ValueError, match="another scenario"):
        await store.claim(
            event_id="bound-event",
            scenario_id="delimiter-change",
            trigger="test",
            now=now,
            lease=timedelta(minutes=8),
        )
