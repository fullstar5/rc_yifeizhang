import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class DeliveryCreate(BaseModel):
    target_url: HttpUrl
    method: Literal["POST", "PUT", "PATCH"] = "POST"
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any | None = None


class DeliveryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: str
    attempt_count: int
    next_attempt_at: datetime
    last_status_code: int | None
    last_error: str | None
    delivered_at: datetime | None
    created_at: datetime
    updated_at: datetime
