import hashlib
import json
import re
import uuid
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import Delivery
from app.schemas import DeliveryCreate, DeliveryRead

DBSession = Annotated[Session, Depends(get_db)]

FORBIDDEN_HEADERS = {
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield


app = FastAPI(
    title="Reliable HTTP Notification Service",
    version="0.1.0",
    lifespan=lifespan,
)
settings = get_settings()


def normalize_headers(headers: dict[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for name, value in headers.items():
        key = name.strip().lower()
        if name != name.strip() or not HEADER_NAME.fullmatch(name) or key in FORBIDDEN_HEADERS:
            raise HTTPException(status_code=422, detail=f"header is not allowed: {name}")
        if key in normalized:
            raise HTTPException(status_code=422, detail=f"duplicate header: {name}")
        if "\r" in value or "\n" in value:
            raise HTTPException(status_code=422, detail=f"invalid header value: {name}")
        try:
            value.encode("ascii")
        except UnicodeEncodeError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"header value must be ASCII: {name}",
            ) from exc
        normalized[key] = value
    return normalized


def validate_target_host(host: str | None) -> None:
    if host is None:
        raise HTTPException(status_code=422, detail="target URL must contain a host")
    host = host.lower().rstrip(".")
    for allowed in settings.allowed_hosts:
        allowed = allowed.strip().lower().rstrip(".")
        if host == allowed:
            return
        if allowed.startswith("*.") and host.endswith(allowed[1:]) and host != allowed[2:]:
            return
    raise HTTPException(status_code=422, detail="target host is not allowlisted")


def fingerprint(payload: DeliveryCreate, headers: dict[str, str]) -> str:
    try:
        canonical = json.dumps(
            {
                "target_url": str(payload.target_url),
                "method": payload.method,
                "headers": headers,
                "body": payload.body,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="body must be valid JSON") from exc
    return hashlib.sha256(canonical.encode()).hexdigest()


def ensure_same_request(existing: Delivery, request_fingerprint: str) -> Delivery:
    if existing.request_fingerprint != request_fingerprint:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="idempotency key was already used with a different request",
        )
    return existing


@app.post(
    "/v1/deliveries",
    response_model=DeliveryRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_delivery(
    payload: DeliveryCreate,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=128),
    ],
    db: DBSession,
) -> Delivery:
    if idempotency_key != idempotency_key.strip() or not idempotency_key.isprintable():
        raise HTTPException(status_code=422, detail="invalid idempotency key")
    if payload.target_url.username or payload.target_url.password or payload.target_url.fragment:
        raise HTTPException(
            status_code=422,
            detail="target URL must not contain credentials or a fragment",
        )
    validate_target_host(payload.target_url.host)
    headers = normalize_headers(payload.headers)
    request_fingerprint = fingerprint(payload, headers)

    existing = db.scalar(
        select(Delivery).where(Delivery.idempotency_key == idempotency_key)
    )
    if existing is not None:
        return ensure_same_request(existing, request_fingerprint)

    delivery = Delivery(
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        target_url=str(payload.target_url),
        http_method=payload.method,
        headers=headers,
        body=payload.body,
    )
    db.add(delivery)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raced = db.scalar(
            select(Delivery).where(Delivery.idempotency_key == idempotency_key)
        )
        if raced is None:
            raise
        return ensure_same_request(raced, request_fingerprint)
    db.refresh(delivery)
    return delivery


@app.get("/v1/deliveries/{delivery_id}", response_model=DeliveryRead)
def get_delivery(delivery_id: uuid.UUID, db: DBSession) -> Delivery:
    delivery = db.get(Delivery, delivery_id)
    if delivery is None:
        raise HTTPException(status_code=404, detail="delivery not found")
    return delivery


@app.get("/health/live")
def liveness() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
def readiness(db: DBSession) -> dict[str, str]:
    db.execute(text("SELECT 1"))
    return {"status": "ok"}
