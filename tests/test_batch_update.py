"""批量原子属主/标签更新：整批全成或全不生效、逐条结论、逐条审计、并发安全。"""

from __future__ import annotations

import threading

from metadata_lineage.models import FieldSpec, FieldType
from metadata_lineage.service import OwnerTagsUpdate
from tests.conftest import log_case


def _entities(service, n=3):
    for i in range(1, n + 1):
        service.register_entity(
            f"e{i}", f"entity{i}",
            [FieldSpec("id", FieldType.INT64)], actor="alice")


def test_batch_all_success_applied(service, request):
    _entities(service, 3)
    result = service.batch_update_owner_tags([
        OwnerTagsUpdate("e1", 0, owner="bob", actor="u1"),
        OwnerTagsUpdate("e2", 0, set_tags=frozenset({"pii"}), actor="u2"),
        OwnerTagsUpdate("e3", 0, owner=None, add_tags=frozenset({"x"}),
                        actor="u3"),
    ])
    log_case(request, {"applied": result.applied,
                       "statuses": [(r.entity_id, r.status, r.revision)
                                    for r in result.results]})
    assert result.applied is True
    assert [r.status for r in result.results] == ["success"] * 3
    assert [r.revision for r in result.results] == [1, 1, 1]
    assert service.get_entity("e1").owner == "bob"
    assert service.get_entity("e2").tags == {"pii"}
    e3 = service.get_entity("e3")
    assert e3.owner is None and e3.tags == {"x"}


def test_batch_one_stale_aborts_all(service, request):
    """任一实体版本过期 → 整批不落库；过期者 conflict，合法者 aborted。"""
    _entities(service, 3)
    # e2 先被别人推进到 revision=1
    service.update_owner_tags("e2", expected_revision=0, owner="eve",
                              actor="eve")
    result = service.batch_update_owner_tags([
        OwnerTagsUpdate("e1", 0, owner="bob", actor="u1"),
        OwnerTagsUpdate("e2", 0, owner="carol", actor="u2"),  # 过期
        OwnerTagsUpdate("e3", 0, add_tags=frozenset({"t"}), actor="u3"),
    ])
    statuses = {r.entity_id: (r.status, r.reason_code)
                for r in result.results}
    log_case(request, {"applied": result.applied, "statuses": statuses})
    assert result.applied is False
    assert statuses["e1"] == ("aborted", "BATCH_ABORTED")
    assert statuses["e2"] == ("conflict", "CONFLICT_REVISION")
    assert statuses["e3"] == ("aborted", "BATCH_ABORTED")
    # 整批不落库：e1/e3 保持原值，e2 只留有之前那次独立更新
    assert service.get_entity("e1").owner is None
    assert service.get_entity("e1").revision == 0
    assert service.get_entity("e3").tags == set()
    assert service.get_entity("e2").owner == "eve"
    assert service.get_entity("e2").revision == 1


def test_batch_invalid_input_aborts_all(service, request):
    _entities(service, 2)
    result = service.batch_update_owner_tags([
        OwnerTagsUpdate("e1", 0, owner="bob", actor="u1"),
        OwnerTagsUpdate("e2", 0, add_tags=frozenset({"z"}),
                        remove_tags=frozenset({"z"}), actor="u2"),
    ])
    statuses = {r.entity_id: (r.status, r.reason_code)
                for r in result.results}
    log_case(request, {"applied": result.applied, "statuses": statuses})
    assert result.applied is False
    assert statuses["e2"] == ("validation_failed", "VALIDATION_ERROR")
    assert statuses["e1"] == ("aborted", "BATCH_ABORTED")
    assert service.get_entity("e1").revision == 0
    assert service.get_entity("e2").revision == 0


def test_batch_missing_entity_aborts_all(service, request):
    _entities(service, 2)
    result = service.batch_update_owner_tags([
        OwnerTagsUpdate("e1", 0, owner="bob", actor="u1"),
        OwnerTagsUpdate("ghost", 0, owner="bob", actor="u2"),
    ])
    statuses = {r.entity_id: (r.status, r.reason_code)
                for r in result.results}
    log_case(request, {"applied": result.applied, "statuses": statuses})
    assert result.applied is False
    assert statuses["ghost"] == ("not_found", "ENTITY_NOT_FOUND")
    assert statuses["e1"] == ("aborted", "BATCH_ABORTED")
    assert service.get_entity("e1").owner is None


def test_batch_audit_per_item(service, request):
    """每个实体的每次尝试都有独立审计，不是整批一条。"""
    _entities(service, 3)
    service.update_owner_tags("e2", expected_revision=0, owner="eve",
                              actor="eve")
    before = len(service.audit_log())
    service.batch_update_owner_tags([
        OwnerTagsUpdate("e1", 0, owner="bob", actor="u1"),
        OwnerTagsUpdate("e2", 0, owner="carol", actor="u2"),
        OwnerTagsUpdate("e3", 0, owner="dave", actor="u3"),
    ])
    entries = [a for a in service.audit_log()[before:]
               if a.action == "batch_update_owner_tags"]
    per_target = {a.target: (a.status, a.reason_code) for a in entries}
    log_case(request, {"audit_entries": len(entries), "per_target": per_target})
    assert len(entries) == 3  # 逐条留痕
    assert per_target["e1"] == ("rejected", "BATCH_ABORTED")
    assert per_target["e2"] == ("rejected", "CONFLICT_REVISION")
    assert per_target["e3"] == ("rejected", "BATCH_ABORTED")


def test_batch_success_audit_carries_revisions(service, request):
    _entities(service, 2)
    before = len(service.audit_log())
    service.batch_update_owner_tags([
        OwnerTagsUpdate("e1", 0, owner="bob", actor="u1"),
        OwnerTagsUpdate("e2", 0, add_tags=frozenset({"t"}), actor="u2"),
    ])
    entries = [a for a in service.audit_log()[before:]
               if a.action == "batch_update_owner_tags"]
    log_case(request, {"entries": [(a.target, a.status, a.revision_before,
                                    a.revision_after) for a in entries]})
    assert len(entries) == 2
    for a in entries:
        assert a.status == "success"
        assert a.revision_before == 0 and a.revision_after == 1


def test_concurrent_batches_stale_one_loses(service, request):
    """并发下同一实体的过期批量写不得被误放行：恰有一批整体生效。"""
    _entities(service, 2)
    barrier = threading.Barrier(2)
    outcomes: list[bool] = []

    def batch(owner: str):
        barrier.wait()
        r = service.batch_update_owner_tags([
            OwnerTagsUpdate("e1", 0, owner=owner, actor=owner),
            OwnerTagsUpdate("e2", 0, owner=owner, actor=owner),
        ])
        outcomes.append(r.applied)

    threads = [threading.Thread(target=batch, args=(f"u{i}",))
               for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    log_case(request, {"applied_outcomes": outcomes,
                       "e1_revision": service.get_entity("e1").revision})
    assert sorted(outcomes) == [False, True]  # 恰一批生效
    assert service.get_entity("e1").revision == 1
    assert service.get_entity("e2").revision == 1
    # 两批共 4 次尝试，4 条审计
    entries = [a for a in service.audit_log()
               if a.action == "batch_update_owner_tags"]
    assert len(entries) == 4
