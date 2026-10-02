"""HTTP 层新能力：owner 未填 vs 显式清空、set_tags 整体替换、批量端点、影响截断。"""

from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from metadata_lineage.api import create_app
from metadata_lineage.service import CatalogService
from metadata_lineage.storage import CatalogStore


def _client() -> TestClient:
    return TestClient(create_app(CatalogService(CatalogStore())))


def _register(client, eid="e1"):
    return client.post(f"/entities/{eid}", json={
        "name": eid, "actor": "alice",
        "fields": [{"name": "f", "type": "INT64", "nullable": True}]})


def _log(case, **kw):
    logging.getLogger("cases").warning("CASE %s | INPUT/EVIDENCE: %s", case, kw)


def test_owner_omitted_keeps_explicit_null_clears():
    """HTTP 层区分“没填 owner”与 "owner": null。"""
    client = _client()
    _register(client)
    r = client.put("/entities/e1/owner-tags",
                   json={"expected_revision": 0, "owner": "bob",
                         "add_tags": ["pii"]})
    assert r.status_code == 200 and r.json()["owner"] == "bob"

    # 不提 owner：保持原值
    r = client.put("/entities/e1/owner-tags",
                   json={"expected_revision": 1, "add_tags": ["core"]})
    _log("owner_omitted", body=r.json())
    assert r.json()["owner"] == "bob"
    assert r.json()["tags"] == ["core", "pii"]

    # 显式 null：清空属主，标签不动
    r = client.put("/entities/e1/owner-tags",
                   json={"expected_revision": 2, "owner": None})
    _log("owner_explicit_null", body=r.json())
    assert r.json()["owner"] is None
    assert r.json()["tags"] == ["core", "pii"]


def test_set_tags_full_replace_over_http():
    client = _client()
    _register(client)
    client.put("/entities/e1/owner-tags",
               json={"expected_revision": 0, "add_tags": ["a", "b"]})
    r = client.put("/entities/e1/owner-tags",
                   json={"expected_revision": 1, "set_tags": ["x"]})
    _log("set_tags_replace", body=r.json())
    assert r.status_code == 200
    assert r.json()["tags"] == ["x"]  # a/b 被整体替换

    # set_tags 与增量改动混用 -> 422
    r = client.put("/entities/e1/owner-tags", json={
        "expected_revision": 2, "set_tags": ["y"], "add_tags": ["z"]})
    _log("set_tags_mixed", status=r.status_code, body=r.json())
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_batch_endpoint_atomic_and_per_item_results():
    client = _client()
    for eid in ("e1", "e2", "e3"):
        _register(client, eid)
    # e2 先被推进到 revision=1
    client.put("/entities/e2/owner-tags",
               json={"expected_revision": 0, "owner": "eve"})

    r = client.post("/owner-tags/batch", json={"items": [
        {"entity_id": "e1", "expected_revision": 0, "owner": "bob"},
        {"entity_id": "e2", "expected_revision": 0, "owner": "carol"},
        {"entity_id": "ghost", "expected_revision": 0, "set_tags": ["t"]},
    ]})
    body = r.json()
    _log("batch_aborted", status=r.status_code, body=body)
    assert r.status_code == 200
    assert body["applied"] is False
    per = {x["entity_id"]: x["status"] for x in body["results"]}
    assert per == {"e1": "aborted", "e2": "conflict", "ghost": "not_found"}
    # 整批未落库
    assert client.get("/entities/e1").json()["owner"] is None

    # 修正版本号后整批成功
    r = client.post("/owner-tags/batch", json={"items": [
        {"entity_id": "e1", "expected_revision": 0, "owner": "bob"},
        {"entity_id": "e2", "expected_revision": 1, "owner": "carol"},
        {"entity_id": "e3", "expected_revision": 0, "set_tags": ["t"]},
    ]})
    body = r.json()
    _log("batch_applied", body=body)
    assert body["applied"] is True
    assert [x["status"] for x in body["results"]] == ["success"] * 3
    assert client.get("/entities/e1").json()["owner"] == "bob"
    assert client.get("/entities/e2").json()["owner"] == "carol"
    assert client.get("/entities/e3").json()["tags"] == ["t"]


def test_impact_truncated_flag_over_http():
    client = _client()
    for eid in "abcd":
        _register(client, eid)
    for s, d in (("a", "b"), ("b", "c"), ("c", "d")):
        client.post("/lineage", json={
            "src": {"entity_id": s, "field_name": "f"},
            "dst": {"entity_id": d, "field_name": "f"}})

    r = client.get("/lineage/impact",
                   params={"entity_id": "a", "field_name": "f",
                           "max_depth": 1})
    body = r.json()
    _log("impact_truncated", body=body)
    assert r.status_code == 200
    assert [x["entity_id"] for x in body["downstream"]] == ["b"]
    assert body["truncated"] is True

    r = client.get("/lineage/impact",
                   params={"entity_id": "a", "field_name": "f"})
    assert r.json()["truncated"] is False
    assert len(r.json()["downstream"]) == 3

    # 非法上限 -> 422
    r = client.get("/lineage/impact",
                   params={"entity_id": "a", "field_name": "f",
                           "max_depth": 0})
    _log("impact_bad_limit", status=r.status_code, body=r.json())
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
