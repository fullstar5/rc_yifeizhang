from fastapi.testclient import TestClient


def request_body() -> dict:
    return {
        "target_url": "https://example.com/hooks/registration",
        "method": "POST",
        "headers": {"Authorization": "Bearer secret"},
        "body": {"user_id": 42},
    }


def test_create_and_get_delivery(client: TestClient) -> None:
    created = client.post(
        "/v1/deliveries",
        headers={"Idempotency-Key": "registration-42"},
        json=request_body(),
    )
    assert created.status_code == 202
    assert created.json()["status"] == "pending"

    fetched = client.get(f"/v1/deliveries/{created.json()['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == created.json()["id"]


def test_same_idempotency_key_returns_same_delivery(client: TestClient) -> None:
    first = client.post(
        "/v1/deliveries",
        headers={"Idempotency-Key": "same-key"},
        json=request_body(),
    )
    second = client.post(
        "/v1/deliveries",
        headers={"Idempotency-Key": "same-key"},
        json=request_body(),
    )
    assert second.status_code == 202
    assert second.json()["id"] == first.json()["id"]


def test_idempotency_key_reuse_with_other_payload_conflicts(client: TestClient) -> None:
    client.post(
        "/v1/deliveries",
        headers={"Idempotency-Key": "conflict-key"},
        json=request_body(),
    )
    changed = request_body()
    changed["body"] = {"user_id": 99}

    response = client.post(
        "/v1/deliveries",
        headers={"Idempotency-Key": "conflict-key"},
        json=changed,
    )
    assert response.status_code == 409


def test_rejects_non_allowlisted_host_and_unsafe_header(client: TestClient) -> None:
    body = request_body()
    body["target_url"] = "http://127.0.0.1/admin"
    assert (
        client.post(
            "/v1/deliveries",
            headers={"Idempotency-Key": "bad-host"},
            json=body,
        ).status_code
        == 422
    )

    body = request_body()
    body["headers"] = {"Host": "internal"}
    assert (
        client.post(
            "/v1/deliveries",
            headers={"Idempotency-Key": "bad-header"},
            json=body,
        ).status_code
        == 422
    )


def test_rejects_header_value_worker_cannot_encode(client: TestClient) -> None:
    body = request_body()
    body["headers"] = {"X-Name": "中文"}
    response = client.post(
        "/v1/deliveries",
        headers={"Idempotency-Key": "bad-header-value"},
        json=body,
    )
    assert response.status_code == 422


def test_rejects_non_standard_json_numbers(client: TestClient) -> None:
    raw = """
    {
      "target_url": "https://example.com/hook",
      "method": "POST",
      "body": {"value": NaN}
    }
    """
    response = client.post(
        "/v1/deliveries",
        headers={
            "Content-Type": "application/json",
            "Idempotency-Key": "invalid-json-number",
        },
        content=raw,
    )
    assert response.status_code == 422
