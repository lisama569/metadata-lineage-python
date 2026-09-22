"""血缘：重复登记、环路拒绝、上下游影响范围、删除/改名字段的稳定语义。"""

from __future__ import annotations

import pytest

from metadata_lineage.models import ErrorCode, FieldSpec, FieldType, ServiceError
from tests.conftest import log_case


def _setup_chain(service):
    """a.f -> b.f -> c.f 的三节点链路。"""
    service.register_entity("a", "A", [FieldSpec("f", FieldType.INT64)], actor="x")
    service.register_entity("b", "B", [FieldSpec("f", FieldType.INT64)], actor="x")
    service.register_entity("c", "C", [FieldSpec("f", FieldType.INT64)], actor="x")
    af = service.resolve_ref("a", "f")
    bf = service.resolve_ref("b", "f")
    cf = service.resolve_ref("c", "f")
    service.register_lineage(af, bf, actor="x")
    service.register_lineage(bf, cf, actor="x")
    return af, bf, cf


def test_upstream_and_downstream_impact(service, request):
    af, bf, cf = _setup_chain(service)
    r = service.impact(bf)
    log_case(request, {
        "node": "b.f",
        "upstream": [x.entity_id for x in r.upstream],
        "downstream": [x.entity_id for x in r.downstream],
    })
    assert r.upstream == [af]
    assert r.downstream == [cf]
    assert len(r.active_edges) == 2


def test_duplicate_lineage_rejected(service, request):
    af, bf, _cf = _setup_chain(service)
    with pytest.raises(ServiceError) as exc:
        service.register_lineage(af, bf, actor="y")
    log_case(request, {"rejected_code": exc.value.code.value})
    assert exc.value.code is ErrorCode.LINEAGE_DUPLICATE


def test_cycle_detected(service, request):
    af, _bf, cf = _setup_chain(service)
    with pytest.raises(ServiceError) as exc:
        # c.f -> a.f 将形成 a->b->c->a 的环
        service.register_lineage(cf, af, actor="y")
    log_case(request, {"attempt": "c.f -> a.f",
                       "rejected_code": exc.value.code.value,
                       "detail": exc.value.detail})
    assert exc.value.code is ErrorCode.LINEAGE_CYCLE
    # 被拒的边不得落库
    assert all(not (e.src == cf and e.dst == af)
               for e in service.store.edges())


def test_self_edge_is_cycle(service, request):
    af, _bf, _cf = _setup_chain(service)
    with pytest.raises(ServiceError) as exc:
        service.register_lineage(af, af, actor="y")
    log_case(request, {"rejected_code": exc.value.code.value})
    assert exc.value.code is ErrorCode.LINEAGE_CYCLE


def test_removed_field_lineage_deactivated_not_lost(service, request):
    """强制删除字段：相关边变悬空并从影响遍历中排除，但记录保留可查。"""
    af, bf, cf = _setup_chain(service)
    # 强制演进 b：删除字段 f
    service.evolve_entity(
        "b", [FieldSpec("g", FieldType.STRING, True)],
        actor="admin", allow_breaking=True,
    )
    # 在最新结构上引用被删字段登记血缘必须失败（不得错连到新字段 g）
    removed_ref = bf
    with pytest.raises(ServiceError) as exc:
        service.register_lineage(
            removed_ref, service.resolve_ref("c", "f"), actor="y")
    log_case(request, {"attempt": "b.removed_f -> c.f",
                       "rejected_code": exc.value.code.value})
    assert exc.value.code is ErrorCode.LINEAGE_FIELD_NOT_FOUND

    # a.f 的下游不再穿透到 b/c（断开），但边仍以 dangling 形式保留
    r = service.impact(af)
    log_case(request, {
        "downstream": [(x.entity_id, x.field_uid) for x in r.downstream],
        "dangling_edge_count": len(r.dangling_edges),
    })
    assert r.downstream == []
    assert len(r.dangling_edges) == 1
    assert r.dangling_edges[0].active is False


def test_renamed_field_lineage_remains_attached(service, request):
    """改名（强制演进，uid 不变）：血缘边不悬空、不错连，自动跟随新名字。"""
    af, bf, cf = _setup_chain(service)
    old_uid = bf.field_uid
    service.evolve_entity(
        "b", [FieldSpec("f_renamed", FieldType.INT64, True, uid=old_uid)],
        actor="admin", allow_breaking=True,
    )
    new_ref = service.resolve_ref("b", "f_renamed")
    assert new_ref.field_uid == old_uid  # 同一字段身份
    r = service.impact(new_ref)
    log_case(request, {
        "renamed_to": "f_renamed",
        "upstream": [x.entity_id for x in r.upstream],
        "downstream": [x.entity_id for x in r.downstream],
        "dangling": len(r.dangling_edges),
    })
    assert r.upstream == [af]
    assert r.downstream == [cf]
    assert r.dangling_edges == []
