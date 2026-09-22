"""不兼容结构演进场景：删除、类型收窄、隐式改名、类型无关变更必须可区分。"""

from __future__ import annotations

import logging

from metadata_lineage import RejectReason

from conftest import expect_reason, log_decision

logger = logging.getLogger("metadata-lineage-tests")


def test_compatible_widening_and_version_trail(service):
    c = service.catalog
    c.register_entity("t", [("a", "INT"), ("b", "STRING")], owner="x")
    v2 = c.evolve_schema("t", [("a", "BIGINT"), ("b", "STRING"), ("c", "BOOL")],
                         note="widen and add")
    assert v2.version == 2
    # 类型拓宽生效，新增字段保留，历史版本不被覆盖
    assert c.get_version("t", 1).field_map()["a"].type.name == "INT"
    assert c.get_version("t", 2).field_map()["a"].type.name == "BIGINT"
    assert [v.version for v in c.list_versions("t")] == [1, 2]
    log_decision("兼容演进：INT->BIGINT 拓宽并新增字段",
                 {"fields": ["a:BIGINT", "b:STRING", "c:BOOL"]},
                 "APPLIED v1->v2，旧版本快照保留")


def test_type_narrow_rejected(service):
    c = service.catalog
    c.register_entity("t", [("a", "DOUBLE")], owner="x")
    payload = [("a", "BIGINT")]
    exc = expect_reason(lambda: c.evolve_schema("t", payload), RejectReason.TYPE_NARROWED)
    assert "narrowed" in exc.message
    # 被拒绝后当前版本仍是 1，没有产生脏版本
    assert c.get_entity("t").current_version == 1
    log_decision("类型收窄", {"fields": ["a:BIGINT (旧 DOUBLE)"]},
                 f"REJECTED {exc.reason.name}: {exc.message}")


def test_type_incompatible_rejected(service):
    c = service.catalog
    c.register_entity("t", [("a", "INT")], owner="x")
    payload = [("a", "STRING")]
    exc = expect_reason(lambda: c.evolve_schema("t", payload),
                        RejectReason.TYPE_INCOMPATIBLE)
    log_decision("类型无关变更", {"fields": ["a:STRING (旧 INT)"]},
                 f"REJECTED {exc.reason.name}: {exc.message}")


def test_field_removal_rejected(service):
    c = service.catalog
    c.register_entity("t", [("a", "INT"), ("b", "STRING")], owner="x")
    exc = expect_reason(lambda: c.evolve_schema("t", [("a", "INT")]),
                        RejectReason.FIELD_REMOVED)
    assert "b" in exc.message
    log_decision("隐式删除字段 b", {"fields": ["a:INT"]},
                 f"REJECTED {exc.reason.name}: {exc.message}")


def test_implicit_rename_distinguished_from_removal(service):
    c = service.catalog
    c.register_entity("t", [("a", "INT"), ("b", "STRING")], owner="x")
    # 一减一增且类型一致 -> 隐式改名，原因必须与删除区分
    exc = expect_reason(
        lambda: c.evolve_schema("t", [("a", "INT"), ("b2", "STRING")]),
        RejectReason.FIELD_RENAMED,
    )
    assert "b->b2" in exc.message or "b2" in exc.message
    log_decision("隐式改名 b->b2（同类型一减一增）",
                 {"fields": ["a:INT", "b2:STRING"]},
                 f"REJECTED {exc.reason.name}: {exc.message}")

    # 类型不同的一减一增不算改名，按删除拒绝
    c.evolve_schema("t", [("a", "BIGINT"), ("b", "STRING")])  # 先做一次兼容演进
    exc2 = expect_reason(
        lambda: c.evolve_schema("t", [("a", "BIGINT"), ("b3", "INT")]),
        RejectReason.FIELD_REMOVED,
    )
    log_decision("STRING 消失、INT 新增（非同类型配对）",
                 {"fields": ["a:BIGINT", "b3:INT"]},
                 f"REJECTED {exc2.reason.name}: {exc2.message}")


def test_explicit_retire_and_rename_produce_versions(service):
    c = service.catalog
    c.register_entity("t", [("a", "INT"), ("b", "STRING")], owner="x")
    v2 = c.rename_field("t", "b", "full_name")
    assert [f.name for f in v2.fields] == ["a", "full_name"]
    v3 = c.retire_field("t", "a")
    assert [f.name for f in v3.fields] == ["full_name"]
    assert c.retired_fields("t") == {"a": 3}
    assert c.current_field_name("t", "b") == "full_name"
    log_decision("显式改名后显式退役", {"rename": "b->full_name", "retire": "a"},
                 "APPLIED v1->v2->v3，退役与别名均可追溯")
