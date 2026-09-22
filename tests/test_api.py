"""HTTP 层：成功、404、409（冲突/重复/环路/过期写）、422 可通过 code 区分。"""

from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from metadata_lineage.api import create_app
from metadata_lineage.service import CatalogService
from metadata_lineage.storage import CatalogStore


def _client() -> TestClient:
    return TestClient(create_app(CatalogService(CatalogStore())))


def _register(client, eid="e1", fields=None):
    fields = fields or [
        {"name": "id", "type": "INT64", "nullable": False},
        {"name": "amount", "type": "INT32", "nullable": True},
    ]
    return client.post(f"/entities/{eid}",
                       json={"name": "orders", "fields": fields, "actor": "alice"})


def test_register_and_get(client=None):
    client = _client()
    r = _register(client)
    assert r.status_code == 201
    assert r.json()["revision"] == 0
    got = client.get("/entities/e1")
    assert got.status_code == 200
    assert len(got.json()["versions"][0]["fields"]) == 2


def test_get_unknown_entity_404():
    r = _client().get("/entities/nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "ENTITY_NOT_FOUND"


def test_duplicate_entity_409():
    client = _client()
    _register(client)
    r = _register(client)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ENTITY_ALREADY_EXISTS"


def test_breaking_evolution_409_with_distinct_code():
    client = _client()
    _register(client)
    r = client.post("/entities/e1/versions", json={
        "actor": "bob",
        "fields": [{"name": "id", "type": "INT64", "nullable": False}],
    })
    assert r.status_code == 409
    body = r.json()
    assert body["error"]["code"] == "BREAKING_FIELD_REMOVED"
    codes = [c["code"] for c in body["error"]["extra"]["breaking_changes"]]
    assert "BREAKING_FIELD_REMOVED" in codes


def test_validation_error_422():
    client = _client()
    # 字段列表为空 -> 422 VALIDATION_ERROR
    r = client.post("/entities/e1",
                    json={"name": "orders", "fields": []})
    logging.getLogger("cases").warning(
        "CASE test_validation_error_422 | status=%s body=%s",
        r.status_code, r.json())
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_lineage_duplicate_and_cycle_409():
    client = _client()
    _register(client, "a", [{"name": "f", "type": "INT64"}])
    _register(client, "b", [{"name": "f", "type": "INT64"}])
    _register(client, "c", [{"name": "f", "type": "INT64"}])

    def edge(s, d):
        return client.post("/lineage", json={
            "src": {"entity_id": s, "field_name": "f"},
            "dst": {"entity_id": d, "field_name": "f"},
        })

    assert edge("a", "b").status_code == 201
    dup = edge("a", "b")
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "LINEAGE_DUPLICATE"
    assert edge("b", "c").status_code == 201
    cyc = edge("c", "a")
    assert cyc.status_code == 409
    assert cyc.json()["error"]["code"] == "LINEAGE_CYCLE"


def test_owner_tags_conflict_409_distinct_from_validation():
    client = _client()
    _register(client)
    ok = client.put("/entities/e1/owner-tags",
                    json={"expected_revision": 0, "owner": "bob"})
    assert ok.status_code == 200
    stale = client.put("/entities/e1/owner-tags",
                       json={"expected_revision": 0, "owner": "eve"})
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "CONFLICT_REVISION"
    bad = client.put("/entities/e1/owner-tags", json={
        "expected_revision": 1, "add_tags": ["x"], "remove_tags": ["x"]})
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "VALIDATION_ERROR"


def test_audit_endpoint_lists_every_attempt():
    client = _client()
    _register(client)
    client.put("/entities/e1/owner-tags",
               json={"expected_revision": 0, "owner": "bob"})
    client.put("/entities/e1/owner-tags",
               json={"expected_revision": 0, "owner": "eve"})  # 冲突
    entries = client.get("/audit").json()
    assert any(e["status"] == "success" for e in entries)
    assert any(e["reason_code"] == "CONFLICT_REVISION" for e in entries)
