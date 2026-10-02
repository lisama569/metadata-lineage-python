"""影响分析容量保护：深度/节点上限、截断标记、结论稳定可复现、悬空边不算传播。"""

from __future__ import annotations

import pytest

from metadata_lineage.models import ErrorCode, FieldSpec, FieldType, ServiceError
from tests.conftest import log_case


def _setup_chain(service):
    """a.f -> b.f -> c.f -> d.f 的四节点链路。"""
    for name in "abcd":
        service.register_entity(
            name, name.upper(), [FieldSpec("f", FieldType.INT64)], actor="x")
    refs = {name: service.resolve_ref(name, "f") for name in "abcd"}
    service.register_lineage(refs["a"], refs["b"], actor="x")
    service.register_lineage(refs["b"], refs["c"], actor="x")
    service.register_lineage(refs["c"], refs["d"], actor="x")
    return refs


def test_max_depth_truncates_downstream(service, request):
    refs = _setup_chain(service)
    r = service.impact(refs["a"], max_depth=1)
    log_case(request, {"input": "impact(a.f, max_depth=1)",
                       "downstream": [x.entity_id for x in r.downstream],
                       "truncated": r.truncated})
    assert [x.entity_id for x in r.downstream] == ["b"]  # 只追一层
    assert r.truncated is True  # c/d 被截断


def test_max_depth_covers_whole_graph_not_truncated(service, request):
    refs = _setup_chain(service)
    r = service.impact(refs["a"], max_depth=3)
    log_case(request, {"input": "impact(a.f, max_depth=3)",
                       "downstream": [x.entity_id for x in r.downstream],
                       "truncated": r.truncated})
    assert [x.entity_id for x in r.downstream] == ["b", "c", "d"]
    assert r.truncated is False  # 上限足够，无截断


def test_max_nodes_truncates(service, request):
    refs = _setup_chain(service)
    r = service.impact(refs["a"], max_nodes=2)
    log_case(request, {"input": "impact(a.f, max_nodes=2)",
                       "downstream": [x.entity_id for x in r.downstream],
                       "truncated": r.truncated})
    assert [x.entity_id for x in r.downstream] == ["b", "c"]
    assert r.truncated is True


def test_truncated_result_is_deterministic(service, request):
    """同样输入多次调用：顺序与内容完全一致。"""
    refs = _setup_chain(service)
    runs = [service.impact(refs["a"], max_depth=2, max_nodes=2)
            for _ in range(5)]
    signatures = [
        ([x.entity_id for x in r.upstream],
         [x.entity_id for x in r.downstream],
         [e.edge_id for e in r.active_edges], r.truncated)
        for r in runs
    ]
    log_case(request, {"runs": len(runs), "signatures": signatures})
    assert len(set(map(str, signatures))) == 1
    assert signatures[0][3] is True


def test_limits_apply_to_upstream_too(service, request):
    refs = _setup_chain(service)
    r = service.impact(refs["d"], max_depth=2)
    log_case(request, {"input": "impact(d.f, max_depth=2)",
                       "upstream": [x.entity_id for x in r.upstream],
                       "truncated": r.truncated})
    assert [x.entity_id for x in r.upstream] == ["c", "b"]
    assert r.truncated is True


def test_invalid_limits_rejected(service, request):
    refs = _setup_chain(service)
    for bad in (0, -1):
        with pytest.raises(ServiceError) as exc:
            service.impact(refs["a"], max_depth=bad)
        assert exc.value.code is ErrorCode.VALIDATION_ERROR
    with pytest.raises(ServiceError) as exc:
        service.impact(refs["a"], max_nodes=0)
    log_case(request, {"input": "max_depth∈{0,-1}, max_nodes=0",
                       "rejected_code": exc.value.code.value})
    assert exc.value.code is ErrorCode.VALIDATION_ERROR


def test_truncation_stable_after_breaking_evolution(service, request):
    """字段被删（强制演进）后：悬空边不参与传播，截断结论稳定且正确。"""
    refs = _setup_chain(service)
    # 强制演进 b：删除字段 f，a->b 与 b->c 两条边悬空
    service.evolve_entity(
        "b", [FieldSpec("g", FieldType.STRING, True)],
        actor="admin", allow_breaking=True,
    )
    # 再接一条 c.f -> e.f，验证不会经悬空的 b 错连
    service.register_entity("e", "E", [FieldSpec("f", FieldType.INT64)],
                            actor="x")
    service.register_lineage(refs["c"], service.resolve_ref("e", "f"),
                             actor="x")

    r1 = service.impact(refs["a"], max_depth=5)
    r2 = service.impact(refs["a"], max_depth=5)
    log_case(request, {
        "after": "b.f deleted (breaking); c.f -> e.f added",
        "downstream": [x.entity_id for x in r1.downstream],
        "dangling": [(e.src.entity_id, e.dst.entity_id)
                     for e in r1.dangling_edges],
        "truncated": r1.truncated,
    })
    # 悬空边不算传播：a 的下游为空，不能穿透到 c/d/e
    assert r1.downstream == []
    assert len(r1.dangling_edges) == 1  # a.f -> b.f(已删)
    assert r1.truncated is False  # 悬空边不触发截断标记
    # 多次调用结论一致
    assert ([x.entity_id for x in r1.downstream]
            == [x.entity_id for x in r2.downstream])
    assert [e.edge_id for e in r1.dangling_edges] == \
           [e.edge_id for e in r2.dangling_edges]

    # 从 c 出发的图不受影响，且节点数上限截断仍生效
    rc = service.impact(refs["c"], max_depth=5, max_nodes=1)
    rc2 = service.impact(refs["c"], max_depth=5, max_nodes=1)
    log_case(request, {"input": "impact(c.f, max_nodes=1) after evolution",
                       "downstream": [x.entity_id for x in rc.downstream],
                       "truncated": rc.truncated})
    # c 的直接邻居是 d 和 e，取哪个由 edge_id 顺序决定（图内确定）
    assert len(rc.downstream) == 1
    assert rc.downstream[0].entity_id in {"d", "e"}
    assert rc.truncated is True  # 另一个邻居被节点数上限截断
    # 同一张图多次调用：被截断时取到的节点也稳定一致
    assert ([x.entity_id for x in rc.downstream]
            == [x.entity_id for x in rc2.downstream])
