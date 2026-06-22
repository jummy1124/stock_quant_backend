from tests.conftest import auth_header

UPSERT = {
    "name": "台積電",
    "market": "上市",
    "target_price": 120.0,
    "cost_price": 95.5,
    "last_close": 109.5,
}


def test_records_require_auth(client):
    assert client.get("/userapi/records").status_code == 401
    assert client.put("/userapi/records/TWSE/2330", json=UPSERT).status_code == 401
    assert client.delete("/userapi/records/TWSE/2330").status_code == 401


def test_empty_records(client):
    h = auth_header(client, email="empty@example.com")
    resp = client.get("/userapi/records", headers=h)
    assert resp.status_code == 200
    assert resp.json() == {"records": []}


def test_upsert_creates_record(client):
    h = auth_header(client, email="c@example.com")
    resp = client.put("/userapi/records/TWSE/2330", json=UPSERT, headers=h)
    assert resp.status_code == 200, resp.text
    rec = resp.json()
    assert rec["symbol"] == "2330"
    assert rec["market_code"] == "TWSE"
    assert rec["name"] == "台積電"
    assert rec["market"] == "上市"
    assert rec["target_price"] == 120.0
    assert rec["cost_price"] == 95.5
    assert rec["last_close"] == 109.5
    assert "updated_at" in rec

    listing = client.get("/userapi/records", headers=h).json()["records"]
    assert len(listing) == 1
    assert listing[0]["symbol"] == "2330"


def test_upsert_updates_existing(client):
    h = auth_header(client, email="u@example.com")
    client.put("/userapi/records/TWSE/2330", json=UPSERT, headers=h)

    updated = dict(UPSERT, target_price=150.0, name="台積電-更新")
    resp = client.put("/userapi/records/TWSE/2330", json=updated, headers=h)
    assert resp.status_code == 200
    assert resp.json()["target_price"] == 150.0
    assert resp.json()["name"] == "台積電-更新"

    # Still only one record (upsert, not insert).
    listing = client.get("/userapi/records", headers=h).json()["records"]
    assert len(listing) == 1


def test_upsert_null_prices(client):
    h = auth_header(client, email="null@example.com")
    body = {"name": "X", "market": "上櫃"}
    resp = client.put("/userapi/records/TPEX/6488", json=body, headers=h)
    assert resp.status_code == 200
    rec = resp.json()
    assert rec["target_price"] is None
    assert rec["cost_price"] is None
    assert rec["last_close"] is None
    assert rec["market_code"] == "TPEX"


def test_delete_existing_returns_204(client):
    h = auth_header(client, email="d@example.com")
    client.put("/userapi/records/TWSE/2330", json=UPSERT, headers=h)
    resp = client.delete("/userapi/records/TWSE/2330", headers=h)
    assert resp.status_code == 204
    assert client.get("/userapi/records", headers=h).json()["records"] == []


def test_delete_missing_is_idempotent_204(client):
    h = auth_header(client, email="dm@example.com")
    resp = client.delete("/userapi/records/TWSE/9999", headers=h)
    assert resp.status_code == 204


def test_user_isolation(client):
    a = auth_header(client, email="alice@example.com")
    b = auth_header(client, email="bob@example.com")

    # Alice creates a record.
    client.put("/userapi/records/TWSE/2330", json=UPSERT, headers=a)

    # Bob cannot see it.
    assert client.get("/userapi/records", headers=b).json()["records"] == []

    # Bob can create a record with the same key — independent row.
    bob_body = dict(UPSERT, name="Bob's 2330", target_price=200.0)
    client.put("/userapi/records/TWSE/2330", json=bob_body, headers=b)

    alice_rec = client.get("/userapi/records", headers=a).json()["records"][0]
    bob_rec = client.get("/userapi/records", headers=b).json()["records"][0]
    assert alice_rec["target_price"] == 120.0
    assert bob_rec["target_price"] == 200.0
    assert bob_rec["name"] == "Bob's 2330"


def test_user_cannot_delete_others_record(client):
    a = auth_header(client, email="alice2@example.com")
    b = auth_header(client, email="bob2@example.com")
    client.put("/userapi/records/TWSE/2330", json=UPSERT, headers=a)

    # Bob deletes same key: idempotent 204 but Alice's record is untouched.
    assert client.delete("/userapi/records/TWSE/2330", headers=b).status_code == 204
    alice_listing = client.get("/userapi/records", headers=a).json()["records"]
    assert len(alice_listing) == 1
