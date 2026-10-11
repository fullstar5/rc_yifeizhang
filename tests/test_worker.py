import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models import Delivery, DeliveryStatus
from app.worker import (
    Outcome,
    claim_one,
    classify_response,
    finalize_attempt,
    retry_delay,
    send_request,
)


def settings(**overrides) -> Settings:
    return Settings(_env_file=None, allowed_hosts=["example.com"], **overrides)


def new_delivery(**overrides) -> Delivery:
    now = datetime.now(UTC)
    values = {
        "idempotency_key": str(uuid.uuid4()),
        "request_fingerprint": "f" * 64,
        "target_url": "https://example.com/hook",
        "http_method": "POST",
        "headers": {},
        "body": {"event": "test"},
        "status": DeliveryStatus.PENDING.value,
        "attempt_count": 0,
        "next_attempt_at": now,
        "created_at": now,
        "updated_at": now,
    }
    values.update(overrides)
    return Delivery(**values)


def test_response_classification_and_backoff() -> None:
    assert classify_response(204).outcome == Outcome.DELIVERED
    assert classify_response(429).outcome == Outcome.RETRY
    assert classify_response(503).outcome == Outcome.RETRY
    assert classify_response(400).outcome == Outcome.DEAD
    assert retry_delay(3, base_seconds=2, max_seconds=100, jitter=0) == 8
    assert retry_delay(20, base_seconds=2, max_seconds=100, jitter=0) == 100


def test_http_send_success_and_network_error() -> None:
    delivery = new_delivery()
    success_client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(202))
    )
    assert send_request(success_client, delivery).outcome == Outcome.DELIVERED

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unavailable", request=request)

    failing_client = httpx.Client(transport=httpx.MockTransport(fail))
    assert send_request(failing_client, delivery).outcome == Outcome.RETRY
    success_client.close()
    failing_client.close()


def test_claim_and_successful_finalize(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as db:
        db.add(new_delivery())
        db.commit()

    with session_factory() as db:
        claimed = claim_one(db, settings())
    assert claimed is not None
    assert claimed.status == DeliveryStatus.PROCESSING.value
    assert claimed.attempt_count == 1

    with session_factory() as db:
        assert finalize_attempt(db, claimed, classify_response(200), settings())
        stored = db.get(Delivery, claimed.id)
        assert stored is not None
        assert stored.status == DeliveryStatus.DELIVERED.value
        assert stored.delivered_at is not None


def test_retry_limit_moves_delivery_to_dead(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as db:
        delivery = new_delivery(
            status=DeliveryStatus.PROCESSING.value,
            attempt_count=3,
            claim_token=uuid.uuid4(),
            locked_until=datetime.now(UTC) + timedelta(seconds=30),
        )
        db.add(delivery)
        db.commit()

    with session_factory() as db:
        assert finalize_attempt(
            db,
            delivery,
            classify_response(503),
            settings(max_attempts=3),
        )
        stored = db.get(Delivery, delivery.id)
        assert stored is not None
        assert stored.status == DeliveryStatus.DEAD.value


def test_expired_lease_is_reclaimed_and_stale_worker_is_fenced(
    session_factory: sessionmaker[Session],
) -> None:
    old_token = uuid.uuid4()
    with session_factory() as db:
        delivery = new_delivery(
            status=DeliveryStatus.PROCESSING.value,
            attempt_count=1,
            claim_token=old_token,
            locked_until=datetime.now(UTC) - timedelta(seconds=1),
        )
        db.add(delivery)
        db.commit()

    with session_factory() as db:
        reclaimed = claim_one(db, settings())
    assert reclaimed is not None
    assert reclaimed.claim_token != old_token
    assert reclaimed.attempt_count == 2

    stale = new_delivery(
        id=reclaimed.id,
        status=DeliveryStatus.PROCESSING.value,
        attempt_count=1,
        claim_token=old_token,
    )
    with session_factory() as db:
        assert not finalize_attempt(db, stale, classify_response(200), settings())
        stored = db.scalar(select(Delivery).where(Delivery.id == reclaimed.id))
        assert stored is not None
        assert stored.status == DeliveryStatus.PROCESSING.value


def test_expired_final_attempt_is_not_sent_again(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as db:
        delivery = new_delivery(
            status=DeliveryStatus.PROCESSING.value,
            attempt_count=3,
            claim_token=uuid.uuid4(),
            locked_until=datetime.now(UTC) - timedelta(seconds=1),
        )
        db.add(delivery)
        db.commit()

    with session_factory() as db:
        assert claim_one(db, settings(max_attempts=3)) is None
        stored = db.get(Delivery, delivery.id)
        assert stored is not None
        assert stored.status == DeliveryStatus.DEAD.value
        assert stored.attempt_count == 3
