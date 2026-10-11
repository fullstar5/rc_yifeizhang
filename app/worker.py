import logging
import random
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import httpx
from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import SessionLocal
from app.models import Delivery, DeliveryStatus

logger = logging.getLogger("delivery-worker")


class Outcome(StrEnum):
    DELIVERED = "delivered"
    RETRY = "retry"
    DEAD = "dead"


@dataclass(frozen=True)
class AttemptResult:
    outcome: Outcome
    status_code: int | None = None
    error: str | None = None


def classify_response(status_code: int) -> AttemptResult:
    if 200 <= status_code < 300:
        return AttemptResult(Outcome.DELIVERED, status_code=status_code)
    if status_code in {408, 425, 429} or status_code >= 500:
        return AttemptResult(
            Outcome.RETRY,
            status_code=status_code,
            error=f"retryable HTTP status {status_code}",
        )
    return AttemptResult(
        Outcome.DEAD,
        status_code=status_code,
        error=f"non-retryable HTTP status {status_code}",
    )


def retry_delay(
    attempt_count: int,
    base_seconds: float,
    max_seconds: float,
    jitter: float | None = None,
) -> float:
    exponential = min(base_seconds * (2 ** max(0, attempt_count - 1)), max_seconds)
    jitter_seconds = random.uniform(0, base_seconds) if jitter is None else jitter
    return min(exponential + jitter_seconds, max_seconds)


def claim_one(db: Session, settings: Settings, now: datetime | None = None) -> Delivery | None:
    now = now or datetime.now(UTC)
    due = and_(
        Delivery.status.in_([DeliveryStatus.PENDING.value, DeliveryStatus.RETRY.value]),
        Delivery.next_attempt_at <= now,
    )
    abandoned = and_(
        Delivery.status == DeliveryStatus.PROCESSING.value,
        Delivery.locked_until <= now,
    )
    delivery = db.scalar(
        select(Delivery)
        .where(or_(due, abandoned))
        .order_by(Delivery.next_attempt_at, Delivery.created_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if delivery is None:
        db.rollback()
        return None

    # if reached maximum attempt, mark as dead
    if delivery.attempt_count >= settings.max_attempts:
        delivery.status = DeliveryStatus.DEAD.value
        delivery.locked_until = None
        delivery.claim_token = None
        delivery.last_error = "attempt budget exhausted after an expired lease"
        delivery.updated_at = now
        db.commit()
        return None

    delivery.status = DeliveryStatus.PROCESSING.value
    delivery.attempt_count += 1
    delivery.claim_token = uuid.uuid4()
    delivery.locked_until = now + timedelta(seconds=settings.lease_seconds)
    delivery.updated_at = now
    db.commit()
    return delivery


def send_request(client: httpx.Client, delivery: Delivery) -> AttemptResult:
    try:
        kwargs: dict[str, object] = {}
        if delivery.body is not None:
            kwargs["json"] = delivery.body
        with client.stream(
            delivery.http_method,
            delivery.target_url,
            headers=delivery.headers,
            **kwargs,
        ) as response:
            status_code = response.status_code
    except httpx.RequestError as exc:
        return AttemptResult(Outcome.RETRY, error=f"{type(exc).__name__}: {exc}")
    except (TypeError, UnicodeError, ValueError) as exc:
        return AttemptResult(Outcome.DEAD, error=f"{type(exc).__name__}: {exc}")
    return classify_response(status_code)


def finalize_attempt(
    db: Session,
    delivery: Delivery,
    result: AttemptResult,
    settings: Settings,
    now: datetime | None = None,
) -> bool:
    now = now or datetime.now(UTC)
    outcome = result.outcome
    if outcome == Outcome.RETRY and delivery.attempt_count >= settings.max_attempts:
        outcome = Outcome.DEAD

    values: dict[str, object] = {
        "last_status_code": result.status_code,
        "last_error": result.error[:1000] if result.error else None,
        "locked_until": None,
        "claim_token": None,
        "updated_at": now,
    }
    if outcome == Outcome.DELIVERED:
        values.update(status=DeliveryStatus.DELIVERED.value, delivered_at=now)
    elif outcome == Outcome.DEAD:
        values.update(status=DeliveryStatus.DEAD.value)
    else:
        delay = retry_delay(
            delivery.attempt_count,
            settings.retry_base_seconds,
            settings.retry_max_seconds,
        )
        values.update(
            status=DeliveryStatus.RETRY.value,
            next_attempt_at=now + timedelta(seconds=delay),
        )

    result_proxy = db.execute(
        update(Delivery)
        .where(
            Delivery.id == delivery.id,
            Delivery.status == DeliveryStatus.PROCESSING.value,
            Delivery.claim_token == delivery.claim_token,
        )
        .values(**values)
    )
    db.commit()
    return result_proxy.rowcount == 1


def run() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    timeout = httpx.Timeout(settings.request_timeout_seconds)
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        while True:
            try:
                with SessionLocal() as db:
                    delivery = claim_one(db, settings)
                if delivery is None:
                    time.sleep(settings.worker_poll_seconds)
                    continue

                logger.info(
                    "delivery attempt started delivery_id=%s attempt=%s",
                    delivery.id,
                    delivery.attempt_count,
                )
                result = send_request(client, delivery)
                with SessionLocal() as db:
                    applied = finalize_attempt(db, delivery, result, settings)
                logger.info(
                    "delivery attempt finished delivery_id=%s outcome=%s "
                    "applied=%s status_code=%s",
                    delivery.id,
                    result.outcome.value,
                    applied,
                    result.status_code,
                )
            except Exception:
                logger.exception("worker iteration failed; task will be recovered by its lease")
                time.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    run()
