"""影响分析容量保护：深度/节点上限、截断标记、确定性可复现、演进后悬空边不传播。"""

from __future__ import annotations

from metadata_lineage.models import FieldSpec, FieldType
from tests.conftest import log_case


def _chain(service, n, field="f"):
    """e0.f -> e1.f -> ... -> e(n-1).f 的直链。"""
    for i in range(n):
        service.register_entity(
            f"e{i}", f"E{i}", [FieldSpec(field, FieldType.INT64)], actor="x")
    refs = [service.resolve_ref(f"e{i}", field) for i in range(n)]
    for i in range(n - 1):
        service.register_lineage(refs[i], refs[i + 1], actor="x")
    return refs


def test_no_limits_full_traversal(service, request):
    refs = _chain(service, 5)
    r = service.impact(refs[0])
    log_case(request, {"downstream": [x.entity_id for x in r.downstream],
                       "truncated": r.truncated})
    assert [x.entity_id for x in r.downstream] == ["e1", "e2", "e3", "e4"]
    assert r.truncated is False
    assert r.upstream_truncated is None and r.downstream_truncated is None


def test_downstream_depth_limit_truncates(service, request):
    refs = _chain(service, 5)
    r = service.impact(refs[0], max_downstream_depth=2)
    log_case(request, {"limit": "max_downstream_depth=2",
                       "downstream": [x.entity_id for x in r.downstream],
                       "truncated_reason": r.downstream_truncated})
    assert [x.entity_id for x in r.downstream] == ["e1", "e2"]
    assert r.truncated is True
    assert r.downstream_truncated == "depth"
    assert r.upstream_truncated is None
    # 参与传播的边只到第 2 层
    assert len(r.active_edges) == 2


def test_upstream_depth_limit_truncates(service, request):
    refs = _chain(service, 5)
    r = service.impact(refs[2], max_upstream_depth=1)
    log_case(request, {"limit": "max_upstream_depth=1",
                       "upstream": [x.entity_id for x in r.upstream],
                       "truncated_reason": r.upstream_truncated})
    assert [x.entity_id for x in r.upstream] == ["e1"]
    assert r.upstream_truncated == "depth"
    # 下游未设限，完整
    assert [x.entity_id for x in r.downstream] == ["e3", "e4"]


def test_node_limit_truncates(service, request):
    refs = _chain(service, 6)
    r = service.impact(refs[0], max_downstream_nodes=3)
    log_case(request, {"limit": "max_downstream_nodes=3",
                       "downstream": [x.entity_id for x in r.downstream],
                       "truncated_reason": r.downstream_truncated})
    assert len(r.downstream) == 3
    assert r.downstream_truncated == "nodes"


def test_depth_zero_returns_empty_without_marking_truncated(service, request):
    refs = _chain(service, 3)
    r = service.impact(refs[0], max_downstream_depth=0)
    log_case(request, {"limit": "depth=0", "downstream": r.downstream,
                       "reason": r.downstream_truncated})
    assert r.downstream == []
    assert r.downstream_truncated is None
    assert r.truncated is False


def test_truncated_result_is_deterministic(service, request):
    """同图同输入多次调用：顺序与内容完全一致。"""
    refs = _chain(service, 8)
    runs = []
    for _ in range(5):
        r = service.impact(refs[0], max_downstream_depth=3,
                           max_downstream_nodes=2)
        runs.append([(x.entity_id, x.field_uid) for x in r.downstream])
    log_case(request, {"runs_equal": len({tuple(map(tuple, x)) for x in runs}) == 1,
                       "sample": [eid for eid, _uid in runs[0]]})
    assert all(run == runs[0] for run in runs)
    assert runs[0] == [("e1", refs[1].field_uid), ("e2", refs[2].field_uid)]


def test_diamond_convergence_not_falsely_reported_as_truncated(service, request):
    """边界之外只剩指向已访问节点的边（菱形收敛）不应误报截断。"""
    refs = _chain(service, 4)  # e0->e1->e2->e3
    # 再加一条 e1->e3，使 e3 在 depth=2 时也被收敛访问到
    service.register_lineage(refs[1], refs[3], actor="x")
    # depth=2 时 e1、e3 均已访问（e3 经由 e1 直达），边界外无未访问节点
    r = service.impact(refs[0], max_downstream_depth=2)
    ids = {x.entity_id for x in r.downstream}
    log_case(request, {"limit": "depth=2", "reached": sorted(ids),
                       "reason": r.downstream_truncated})
    assert ids == {"e1", "e2", "e3"}
    assert r.downstream_truncated is None


def test_dangling_edges_excluded_under_limits_after_evolution(service, request):
    """字段被删导致悬空：即使设了深度上限，悬空部分也不能被当成有效传播。"""
    refs = _chain(service, 4)
    # 强制演进删除 e1.f，边 e0->e1、e1->e2 变悬空
    service.evolve_entity(
        "e1", [FieldSpec("g", FieldType.STRING, True)],
        actor="admin", allow_breaking=True)

    r = service.impact(refs[0], max_downstream_depth=3)
    log_case(request, {
        "downstream": [x.entity_id for x in r.downstream],
        "dangling": len(r.dangling_edges),
        "active_edges": len(r.active_edges),
        "truncated": r.truncated,
    })
    # 不得穿透悬空边到达 e2/e3
    assert r.downstream == []
    assert r.truncated is False
    assert len(r.dangling_edges) == 1  # 仅与起点直接相连的悬空边
    assert r.active_edges == []


def test_rename_keeps_propagation_stable_under_limits(service, request):
    """改名（uid 不变）后，有界遍历的结论仍然正确且稳定。"""
    refs = _chain(service, 4)
    old_uid = refs[1].field_uid
    service.evolve_entity(
        "e1", [FieldSpec("f2", FieldType.INT64, True, uid=old_uid)],
        actor="admin", allow_breaking=True)
    ref1_new = service.resolve_ref("e1", "f2")
    r1 = service.impact(ref1_new, max_upstream_depth=1, max_downstream_depth=1)
    r2 = service.impact(ref1_new, max_upstream_depth=1, max_downstream_depth=1)
    log_case(request, {
        "upstream": [x.entity_id for x in r1.upstream],
        "downstream": [x.entity_id for x in r1.downstream],
        "stable": ([(x.entity_id, x.field_uid) for x in r1.upstream]
                   == [(x.entity_id, x.field_uid) for x in r2.upstream]),
    })
    assert [x.entity_id for x in r1.upstream] == ["e0"]
    assert [x.entity_id for x in r1.downstream] == ["e2"]
    assert r1.dangling_edges == []


def test_http_impact_limit_query_params():
    from fastapi.testclient import TestClient
    from metadata_lineage.api import create_app
    from metadata_lineage.service import CatalogService
    from metadata_lineage.storage import CatalogStore

    svc = CatalogService(CatalogStore())
    for i in range(4):
        svc.register_entity(f"e{i}", f"E{i}",
                            [FieldSpec("f", FieldType.INT64)], actor="x")
    refs = [svc.resolve_ref(f"e{i}", "f") for i in range(4)]
    for i in range(3):
        svc.register_lineage(refs[i], refs[i + 1], actor="x")
    client = TestClient(create_app(svc))

    r = client.get("/lineage/impact", params={
        "entity_id": "e0", "field_name": "f",
        "max_downstream_depth": "1",
    })
    body = r.json()
    assert r.status_code == 200
    assert [x["entity_id"] for x in body["downstream"]] == ["e1"]
    assert body["truncated"] is True
    assert body["downstream_truncated"] == "depth"
    assert body["limits"]["max_downstream_depth"] == 1

    bad = client.get("/lineage/impact", params={
        "entity_id": "e0", "field_name": "f",
        "max_downstream_nodes": "-1",
    })
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "VALIDATION_ERROR"
