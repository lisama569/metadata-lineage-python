"""属主/标签的并发更新：乐观锁冲突可区分，成功/过期/校验失败均有审计。"""

from __future__ import annotations

import logging
import threading

from metadata_lineage import RejectReason, UpdateOutcome

from conftest import expect_reason, log_decision

logger = logging.getLogger("metadata-lineage-tests")


def test_stale_write_is_distinguishable(service):
    c = service.catalog
    c.register_entity("t", [("a", "INT")], owner="alice")

    applied = c.update_tags("t", add={"pii"}, expected_version=1)
    assert applied.outcome is UpdateOutcome.APPLIED
    assert applied.entity.revision == 2

    stale = c.update_owner("t", owner="mallory", expected_version=1)
    assert stale.outcome is UpdateOutcome.REJECTED_STALE
    assert stale.reason == RejectReason.STALE_VERSION.name
    assert stale.actual_version == 2
    assert stale.entity is None
    # 过期写入绝不生效
    assert c.get_entity("t").owner == "alice"
    log_decision("过期写入（expected=1, actual=2）",
                 {"owner": "mallory", "expected_revision": 1},
                 f"REJECTED {stale.outcome.name}/{stale.reason}，属主未变")

    invalid = c.update_owner("t", owner="   ", expected_version=2)
    assert invalid.outcome is UpdateOutcome.REJECTED_INVALID
    assert invalid.reason == RejectReason.VALIDATION_FAILED.name
    log_decision("校验失败（空白 owner）", {"owner": "   "},
                 f"REJECTED {invalid.outcome.name}/{invalid.reason}")

    fresh = c.update_owner("t", owner="bob", expected_version=2)
    assert fresh.outcome is UpdateOutcome.APPLIED
    assert fresh.entity.owner == "bob"


def test_tag_validation_failures(service):
    c = service.catalog
    c.register_entity("t", [("a", "INT")], owner="alice")
    r1 = c.update_tags("t", add={""}, expected_version=1)
    assert r1.outcome is UpdateOutcome.REJECTED_INVALID
    r2 = c.update_tags("t", add={"x"}, remove={"x"}, expected_version=1)
    assert r2.outcome is UpdateOutcome.REJECTED_INVALID
    assert c.get_entity("t").tags == frozenset()


def test_concurrent_tag_updates_exactly_one_wins(service):
    """两个线程基于同一 revision 并发打不同标签：只允许一方成功。"""
    c = service.catalog
    c.register_entity("t", [("a", "INT")], owner="alice")
    barrier = threading.Barrier(2)
    results: list = []

    def worker(tag: str):
        barrier.wait()
        r = c.update_tags("t", add={tag}, expected_version=1, actor=f"w-{tag}")
        results.append((tag, r.outcome.name))

    t1 = threading.Thread(target=worker, args=("red",))
    t2 = threading.Thread(target=worker, args=("blue",))
    t1.start(); t2.start(); t1.join(); t2.join()

    outcomes = {tag: out for tag, out in results}
    log_decision("并发标签冲突（双方 expected_revision=1）",
                 {"red": outcomes["red"], "blue": outcomes["blue"]},
                 "恰一个 APPLIED，另一个 REJECTED_STALE")
    assert sorted(outcomes.values()).count("APPLIED") == 1
    assert sorted(outcomes.values()).count("REJECTED_STALE") == 1

    tags = c.get_entity("t").tags
    assert tags in ({"red"}, {"blue"})
    # 失败者可用最新 revision 重试并成功
    winner_tag = next(iter(tags))
    loser_tag = "blue" if winner_tag == "red" else "red"
    retry = c.update_tags("t", add={loser_tag}, expected_version=2)
    assert retry.outcome is UpdateOutcome.APPLIED
    assert retry.entity.tags == {"red", "blue"}


def test_schema_evolution_bumps_revision_and_stale_tag_rejected(service):
    c = service.catalog
    c.register_entity("t", [("a", "INT")], owner="alice")
    c.update_tags("t", add={"pii"}, expected_version=1)   # revision -> 2
    c.evolve_schema("t", [("a", "BIGINT")])                # revision -> 3
    stale = c.update_tags("t", add={"x"}, expected_version=2)
    assert stale.outcome is UpdateOutcome.REJECTED_STALE
    assert stale.actual_version == 3
    fresh = c.update_tags("t", add={"x"}, expected_version=3)
    assert fresh.outcome is UpdateOutcome.APPLIED


def test_audit_never_missing_for_success_failure_and_conflict(service):
    c = service.catalog
    c.register_entity("t", [("a", "DOUBLE"), ("b", "STRING")], owner="alice", actor="root")
    # 失败的演进（收窄）
    try:
        c.evolve_schema("t", [("a", "BIGINT"), ("b", "STRING")])
    except Exception:
        pass
    # 失败的演进（删除）
    try:
        c.evolve_schema("t", [("a", "DOUBLE")])
    except Exception:
        pass
    # 过期写入
    c.update_tags("t", add={"pii"}, expected_version=1)
    c.update_owner("t", owner="bob", expected_version=1)

    audit = c.audit_log()
    actions = [(a.action, a.success, a.reason) for a in audit]
    assert actions[0] == ("register_entity", True, None)
    assert ("evolve_schema", False, "TYPE_NARROWED") in actions
    assert ("evolve_schema", False, "FIELD_REMOVED") in actions
    assert ("update_tags", True, None) in actions
    assert ("update_owner", False, "STALE_VERSION") in actions
    # 序号严格连续递增、时间戳不缺
    assert [a.seq for a in audit] == list(range(1, len(audit) + 1))
    assert all(a.timestamp for a in audit)
    assert all(a.actor for a in audit)
    log_decision("审计完整性", {"rows": len(audit)},
                 "成功/拒绝/冲突每条变更均有序号、时间戳、actor 与原因")


def test_entity_not_found_errors(service):
    c = service.catalog
    expect_reason(lambda: c.get_entity("nope"), RejectReason.ENTITY_NOT_FOUND)
    expect_reason(
        lambda: c.evolve_schema("nope", [("a", "INT")]),
        RejectReason.ENTITY_NOT_FOUND,
    )
