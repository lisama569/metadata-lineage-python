"""HTTP 层：拒绝原因经响应体 reason 区分；过期写入与成功、校验失败状态可辨。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from metadata_lineage.app import create_app


@pytest.fixture
def client():
    return TestClient(create_app())


def _reg(client, eid, fields, owner):
    return client.post("/entities", json={
        "entity_id": eid, "owner": owner,
        "fields": [{"name": n, "type": t} for n, t in fields],
    })


def test_register_evolve_rejections(client):
    r = _reg(client, "t", [("a", "INT"), ("b", "STRING")], "x")
    assert r.status_code == 200

    r = client.post("/entities/t/evolve",
                    json={"fields": [{"name": "a", "type": "DOUBLE"},
                                     {"name": "b", "type": "STRING"}]})
    assert r.status_code == 200  # 兼容拓宽

    r = client.post("/entities/t/evolve",
                    json={"fields": [{"name": "a", "type": "INT"},
                                     {"name": "b", "type": "STRING"}]})
    assert r.status_code == 409 and r.json()["error"] == "TYPE_NARROWED"

    r = client.post("/entities/t/evolve",
                    json={"fields": [{"name": "a", "type": "DOUBLE"}]})
    assert r.status_code == 409 and r.json()["error"] == "FIELD_REMOVED"

    r = client.post("/entities/zz/evolve", json={"fields": []})
    assert r.status_code == 404 and r.json()["error"] == "ENTITY_NOT_FOUND"


def test_lineage_cycle_and_impact_over_http(client):
    _reg(client, "A", [("id", "INT")], "a")
    _reg(client, "B", [("bid", "INT")], "b")
    _reg(client, "C", [("cid", "INT")], "c")

    def edge(s, sf, t, tf):
        return client.post("/lineage/edges", json={
            "source_entity": s, "source_field": sf,
            "target_entity": t, "target_field": tf})

    assert edge("A", "id", "B", "bid").status_code == 200
    assert edge("B", "bid", "C", "cid").status_code == 200
    r = edge("A", "id", "B", "bid")
    assert r.status_code == 409 and r.json()["error"] == "DUPLICATE_EDGE"
    r = edge("C", "cid", "A", "id")
    assert r.status_code == 409 and r.json()["error"] == "CYCLE_DETECTED"

    ups = client.get("/lineage/upstream/C", params={"field": "cid"}).json()
    assert {("A", "id"), ("B", "bid")} == {(x["entity"], x["field"]) for x in ups}


def test_concurrent_tag_conflict_outcomes(client):
    _reg(client, "t", [("a", "INT")], "x")
    ok = client.put("/entities/t/tags",
                    json={"add": ["pii"], "expected_revision": 1})
    assert ok.json()["outcome"] == "APPLIED"
    stale = client.put("/entities/t/owner",
                       json={"owner": "y", "expected_revision": 1})
    body = stale.json()
    assert body["outcome"] == "REJECTED_STALE"
    assert body["reason"] == "STALE_VERSION"
    assert body["actual_revision"] == 2

    audit = client.get("/audit").json()
    assert any(a["success"] and a["action"] == "update_tags" for a in audit)
    assert any(not a["success"] and a["reason"] == "STALE_VERSION" for a in audit)
