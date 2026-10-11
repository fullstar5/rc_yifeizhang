import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.models import Delivery
from app.worker import claim_one
from tests.test_worker import new_delivery

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="set TEST_DATABASE_URL to run PostgreSQL integration tests",
)


def test_concurrent_workers_claim_a_delivery_only_once() -> None:
    engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    delivery = new_delivery()
    with factory() as db:
        db.add(delivery)
        db.commit()

    def claim():
        with factory() as db:
            item = claim_one(db, Settings(_env_file=None))
            return item.id if item else None

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            claimed_ids = list(pool.map(lambda _: claim(), range(2)))
        assert claimed_ids.count(delivery.id) == 1
        assert claimed_ids.count(None) == 1
    finally:
        with factory() as db:
            db.execute(delete(Delivery).where(Delivery.id == delivery.id))
            db.commit()
        engine.dispose()
