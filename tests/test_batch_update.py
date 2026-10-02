"""批量原子更新：逐条结果可区分、整批原子、按各自版本号校验、审计全覆盖。"""

from __future__ import annotations

import threading

import pytest

from metadata_lineage.models import (
    BatchItemStatus,
    ErrorCode,
    FieldSpec,
    FieldType,
    ServiceError,
)
from metadata_lineage.service import BatchOwnerTagsItem
from tests.conftest import log_case


def _entity(service, eid, owner=None, tags=()):
    service.register_entity(eid, eid.upper(),
                            [FieldSpec("f", FieldType.INT64)], actor="x")
    if owner is not None or tags:
        service.update_owner_tags(eid, expected_revision=0, owner=owner,
                                  add_tags=set(tags), actor="x")


def _item(eid, rev, **kw):
    return BatchOwnerTagsItem(entity_id=eid, expected_revision=rev, **kw)


def test_batch_all_success_commits_each_own_revision(service, request):
    for eid in ("a", "b", "c"):
        _entity(service, eid)
    service.update_owner_tags("b", expected_revision=0, owner="old", actor="x")

    r = service.batch_update_owner_tags([
        _item("a", 0, owner="oa"),
        _item("b", 1, replace_tags=frozenset({"t1", "t2"})),
        _item("c", 0, add_tags=frozenset({"z"})),
    ])
    log_case(request, {
        "committed": r.committed,
        "results": [(x.entity_id, x.status, x.revision_after)
                    for x in r.results],
    })
    assert r.committed is True
    assert [x.status for x in r.results] == ["SUCCESS"] * 3
    # 各按自己的 revision 推进
    assert service.get_entity("a").revision == 1
    assert service.get_entity("b").revision == 2
    assert service.get_entity("c").revision == 1
    # b 只整体换标签：属主 old 保持
    assert service.get_entity("b").owner == "old"
    assert service.get_entity("b").tags == {"t1", "t2"}


def test_batch_mixed_results_and_atomic_rollback(service, request):
    for eid in ("a", "b", "c"):
        _entity(service, eid)
    service.update_owner_tags("a", expected_revision=0, owner="keep", actor="x")
    service.update_owner_tags("b", expected_revision=0, owner="old", actor="x")
    # a rev=1（会通过），b rev=1（用过期 0），c rev=0（校验失败），d 不存在
    r = service.batch_update_owner_tags([
        _item("a", 1, owner="must-not-land"),
        _item("b", 0, owner="stale"),
        _item("c", 0, add_tags=frozenset({"p"}), remove_tags=frozenset({"p"})),
        _item("d", 0, owner="ghost"),
    ])
    statuses = [x.status for x in r.results]
    reasons = [x.reason_code for x in r.results]
    currents = [x.current_revision for x in r.results]
    log_case(request, {
        "committed": r.committed,
        "per_item": [{"id": x.entity_id, "status": x.status,
                      "reason": x.reason_code,
                      "current_revision": x.current_revision}
                     for x in r.results],
    })
    assert r.committed is False
    assert statuses == [
        BatchItemStatus.BATCH_ABORTED.value,
        BatchItemStatus.CONFLICT_REVISION.value,
        BatchItemStatus.VALIDATION_ERROR.value,
        BatchItemStatus.ENTITY_NOT_FOUND.value,
    ]
    assert reasons[1] == "CONFLICT_REVISION"
    assert currents[1] == 1
    # 原子性：通过自身校验的 a 也不得落库
    ent_a = service.get_entity("a")
    assert ent_a.owner == "keep" and ent_a.revision == 1
    assert service.get_entity("b").owner == "old"


def test_batch_same_entity_twice_is_validation_error(service, request):
    _entity(service, "a")
    r = service.batch_update_owner_tags([
        _item("a", 0, owner="u1"),
        _item("a", 0, owner="u2"),
    ])
    log_case(request, {"statuses": [x.status for x in r.results]})
    assert r.committed is False
    assert r.results[1].status == BatchItemStatus.VALIDATION_ERROR.value
    assert service.get_entity("a").revision == 0


def test_batch_replace_null_and_empty_owner_rejected(service, request):
    _entity(service, "a")
    r = service.batch_update_owner_tags([
        _item("a", 0, replace_tags=None),
    ])
    assert r.committed is False
    assert r.results[0].status == BatchItemStatus.VALIDATION_ERROR.value

    r2 = service.batch_update_owner_tags([
        _item("a", 0, owner="   "),
    ])
    log_case(request, {"null_replace": r.results[0].detail,
                       "blank_owner": r2.results[0].status})
    assert r2.results[0].status == BatchItemStatus.VALIDATION_ERROR.value
    assert service.get_entity("a").revision == 0


def test_empty_batch_rejected(service, request):
    with pytest.raises(ServiceError) as exc:
        service.batch_update_owner_tags([])
    log_case(request, {"rejected_code": exc.value.code.value})
    assert exc.value.code is ErrorCode.VALIDATION_ERROR


def test_batch_every_attempt_audited_per_entity(service, request):
    for eid in ("a", "b", "c", "d"):
        _entity(service, eid)
    before = len(service.audit_log())
    r = service.batch_update_owner_tags([
        _item("a", 0, owner="ok"),          # 通过但被回滚
        _item("b", 99, owner="stale"),      # 过期
        _item("c", 0, add_tags=frozenset({"p"}),
              remove_tags=frozenset({"p"})),  # 校验失败
        _item("missing", 0, owner="x"),     # 不存在
    ])
    entries = [a for a in service.audit_log()[before:]
               if a.action == "batch_update_owner_tags"]
    log_case(request, {"committed": r.committed,
                       "audit_entries": len(entries),
                       "targets": sorted(a.target for a in entries)})
    assert not r.committed
    # 每个实体的每一次尝试都要有审计，不能整批只留一条
    assert len(entries) == 4
    assert {a.target for a in entries} == {"a", "b", "c", "missing"}
    # 通过但回滚的条目审计要标明未写入
    a_audit = next(a for a in entries if a.target == "a")
    assert a_audit.status == "rejected"
    assert a_audit.detail["status"] == "BATCH_ABORTED"
    # 全部审计共享同一 batch_id
    assert len({a.detail["batch_id"] for a in entries}) == 1


def test_concurrent_batches_stale_writes_never_pass(service, request):
    """多个并发批量以同一过期版本写同一实体：恰一批成功，其余整批失败。"""
    for eid in ("a", "b"):
        _entity(service, eid)
    barrier = threading.Barrier(8)
    outcomes: list[str] = []

    def worker(i: int) -> None:
        barrier.wait()
        r = service.batch_update_owner_tags([
            _item("a", 0, owner=f"u{i}"),
            _item("b", 0, add_tags=frozenset({f"t{i}"})),
        ])
        outcomes.append("committed" if r.committed else "aborted")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    committed = [o for o in outcomes if o == "committed"]
    log_case(request, {"committed": len(committed),
                       "aborted": len(outcomes) - len(committed)})
    assert len(committed) == 1
    assert service.get_entity("a").revision == 1
    assert service.get_entity("b").revision == 1
    assert len(service.get_entity("b").tags) == 1


def test_http_batch_endpoint_mixed_results():
    from fastapi.testclient import TestClient
    from metadata_lineage.api import create_app
    from metadata_lineage.service import CatalogService
    from metadata_lineage.storage import CatalogStore

    svc = CatalogService(CatalogStore())
    svc.register_entity("a", "A", [FieldSpec("f", FieldType.INT64)], actor="x")
    svc.register_entity("b", "B", [FieldSpec("f", FieldType.INT64)], actor="x")
    svc.update_owner_tags("b", expected_revision=0, owner="old", actor="x")
    client = TestClient(create_app(svc))

    # 成功批
    ok = client.post("/entities/owner-tags:batch", json={"items": [
        {"entity_id": "a", "expected_revision": 0, "owner": "oa"},
        {"entity_id": "b", "expected_revision": 1, "replace_tags": ["t"]},
    ]})
    assert ok.status_code == 200
    body = ok.json()
    assert body["committed"] is True
    assert [r["status"] for r in body["results"]] == ["SUCCESS", "SUCCESS"]

    # 混合批：a 通过但被回滚、b 过期、c 不存在
    mixed = client.post("/entities/owner-tags:batch", json={"items": [
        {"entity_id": "a", "expected_revision": 1, "owner": "noland"},
        {"entity_id": "b", "expected_revision": 1, "owner": "stale"},
        {"entity_id": "c", "expected_revision": 0, "add_tags": ["p"],
         "remove_tags": ["p"]},
    ]})
    assert mixed.status_code == 200  # 批量业务结果走响应体，不是 HTTP 错误
    m = mixed.json()
    statuses = [r["status"] for r in m["results"]]
    assert m["committed"] is False
    assert statuses == ["BATCH_ABORTED", "CONFLICT_REVISION", "VALIDATION_ERROR"]
    # a 未落库
    got = client.get("/entities/a").json()
    assert got["owner"] == "oa" and got["revision"] == 1

    # 空批 -> 422
    empty = client.post("/entities/owner-tags:batch", json={"items": []})
    assert empty.status_code == 422
    assert empty.json()["error"]["code"] == "VALIDATION_ERROR"
