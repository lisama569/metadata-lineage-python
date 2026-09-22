"""并发更新一致性：乐观锁 CAS、成功/冲突/校验失败可区分、审计全覆盖。"""

from __future__ import annotations

import threading

import pytest

from metadata_lineage.models import ErrorCode, FieldSpec, FieldType, ServiceError
from tests.conftest import log_case


def _entity(service):
    service.register_entity(
        "e1", "orders", [FieldSpec("id", FieldType.INT64)], actor="alice")


def test_cas_success_bumps_revision(service, request):
    _entity(service)
    entity, rev = service.update_owner_tags(
        "e1", expected_revision=0, owner="bob",
        add_tags={"pii"}, actor="bob")
    log_case(request, {"new_revision": rev, "owner": entity.owner,
                       "tags": sorted(entity.tags)})
    assert rev == 1
    assert entity.owner == "bob"
    assert entity.tags == {"pii"}


def test_stale_write_conflict_is_distinct(service, request):
    _entity(service)
    service.update_owner_tags("e1", expected_revision=0, owner="bob",
                              actor="bob")
    with pytest.raises(ServiceError) as exc:
        # 仍以过期 revision=0 写入
        service.update_owner_tags("e1", expected_revision=0,
                                  add_tags={"critical"}, actor="carol")
    log_case(request, {"expected_revision": 0,
                       "rejected_code": exc.value.code.value,
                       "extra": exc.value.extra})
    assert exc.value.code is ErrorCode.CONFLICT_REVISION
    assert exc.value.extra["current_revision"] == 1
    # 过期写入不得生效
    assert service.get_entity("e1").tags == set()


def test_validation_failure_distinct_from_conflict(service, request):
    _entity(service)
    with pytest.raises(ServiceError) as exc:
        service.update_owner_tags(
            "e1", expected_revision=0, add_tags={"x"}, remove_tags={"x"},
            actor="bob")
    log_case(request, {"rejected_code": exc.value.code.value})
    assert exc.value.code is ErrorCode.VALIDATION_ERROR
    # 校验失败不得推进 revision
    assert service.get_entity("e1").revision == 0


def test_concurrent_writes_exactly_one_succeeds(service, request):
    """多线程同时以同一 expected_revision 写入：恰有一个成功，其余冲突。"""
    _entity(service)
    results: list[str] = []
    barrier = threading.Barrier(8)

    def worker(tag: str) -> None:
        barrier.wait()
        try:
            service.update_owner_tags("e1", expected_revision=0,
                                      add_tags={tag}, actor=tag)
            results.append("success")
        except ServiceError as e:
            results.append(e.code.value)

    threads = [threading.Thread(target=worker, args=(f"t{i}",))
               for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = [r for r in results if r == "success"]
    conflicts = [r for r in results if r == "CONFLICT_REVISION"]
    log_case(request, {"results": sorted(results),
                       "success_count": len(successes),
                       "conflict_count": len(conflicts)})
    assert len(successes) == 1
    assert len(conflicts) == 7
    # 最终 revision 恰好推进一次，只留下胜出者的标签
    entity = service.get_entity("e1")
    assert entity.revision == 1
    assert len(entity.tags) == 1


def test_audit_covers_success_rejection_and_conflict(service, request):
    _entity(service)
    # 成功
    service.update_owner_tags("e1", expected_revision=0, owner="bob",
                              add_tags={"pii"}, actor="bob")
    # 冲突
    with pytest.raises(ServiceError):
        service.update_owner_tags("e1", expected_revision=0, owner="eve",
                                  actor="eve")
    # 校验失败
    with pytest.raises(ServiceError):
        service.update_owner_tags("e1", expected_revision=1,
                                  add_tags={"z"}, remove_tags={"z"},
                                  actor="bob")
    # 不兼容演进被拒
    with pytest.raises(ServiceError):
        service.evolve_entity("e1", [], actor="bob")

    audit = service.audit_log()
    statuses = {(a.action, a.status, a.reason_code) for a in audit}
    log_case(request, {"audit_count": len(audit),
                       "statuses": sorted((a, s, str(c)) for a, s, c in statuses)})
    assert any(a == "update_owner_tags" and s == "success"
               for a, s, _c in statuses)
    assert any(a == "update_owner_tags" and s == "rejected"
               and c == "CONFLICT_REVISION" for a, s, c in statuses)
    assert any(a == "update_owner_tags" and s == "validation_failed"
               and c == "VALIDATION_ERROR" for a, s, c in statuses)
    assert any(s == "validation_failed" and a == "evolve_entity"
               for a, s, _c in statuses)
    # 每条审计都有时间戳、操作者与目标，不可缺失关键字段
    for entry in audit:
        assert entry.audit_id and entry.at and entry.actor
        assert entry.action and entry.target and entry.status


def test_audit_is_append_only_under_concurrency(service, request):
    """高并发下审计记录不得丢失：每次尝试（成功或冲突）都必须留痕。"""
    _entity(service)
    before = len(service.audit_log())
    barrier = threading.Barrier(16)

    def worker(i: int) -> None:
        barrier.wait()
        try:
            service.update_owner_tags("e1", expected_revision=0,
                                      owner=f"u{i}", actor=f"u{i}")
        except ServiceError:
            pass

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    audit = service.audit_log()
    owner_attempts = [a for a in audit[before:] if a.action == "update_owner_tags"]
    log_case(request, {"attempts_logged": len(owner_attempts)})
    assert len(owner_attempts) == 16
