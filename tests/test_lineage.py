"""血缘登记、重复/环路识别、上下游影响范围、退役/改名字段的稳定语义。"""

from __future__ import annotations

import logging

from metadata_lineage import EdgeStatus, RejectReason

from conftest import expect_reason, log_decision

logger = logging.getLogger("metadata-lineage-tests")


def _seed(service):
    c = service.catalog
    c.register_entity("A", [("id", "INT"), ("name", "STRING")], owner="a")
    c.register_entity("B", [("bid", "INT"), ("bname", "STRING")], owner="b")
    c.register_entity("C", [("cid", "INT")], owner="c")
    g = service.lineage
    g.add_edge(("A", "id"), ("B", "bid"))
    g.add_edge(("A", "name"), ("B", "bname"))
    g.add_edge(("B", "bid"), ("C", "cid"))
    return g


def test_duplicate_and_cycle_are_distinct(service):
    g = _seed(service)
    exc1 = expect_reason(lambda: g.add_edge(("A", "id"), ("B", "bid")),
                         RejectReason.DUPLICATE_EDGE)
    log_decision("重复登记 A.id->B.bid", {"edge": "A.id->B.bid"},
                 f"REJECTED {exc1.reason.name}")

    exc2 = expect_reason(lambda: g.add_edge(("C", "cid"), ("A", "id")),
                         RejectReason.CYCLE_DETECTED)
    log_decision("登记闭环 C.cid->A.id", {"edge": "C.cid->A.id"},
                 f"REJECTED {exc2.reason.name}")

    exc3 = expect_reason(lambda: g.add_edge(("A", "id"), ("A", "id")),
                         RejectReason.CYCLE_DETECTED)
    assert exc3.reason is RejectReason.CYCLE_DETECTED


def test_endpoint_validation(service):
    g = _seed(service)
    exc = expect_reason(lambda: g.add_edge(("A", "id"), ("B", "ghost")),
                        RejectReason.ENDPOINT_NOT_FOUND)
    log_decision("目标字段不存在", {"edge": "A.id->B.ghost"},
                 f"REJECTED {exc.reason.name}: {exc.message}")

    expect_reason(lambda: g.add_edge(("Z", "id"), ("B", "bid")),
                  RejectReason.ENTITY_NOT_FOUND)


def test_upstream_downstream_impact(service):
    g = _seed(service)
    assert g.upstream("C", "cid") == {("A", "id"), ("B", "bid")}
    assert g.downstream("A", "id") == {("B", "bid"), ("C", "cid")}
    assert g.downstream("A", "name") == {("B", "bname")}
    # 未知字段不报错：以给定起点计算，结果为空集
    assert g.downstream("C", "cid") == set()
    log_decision("影响范围 A.id", {"upstream of C.cid": sorted(map(str, g.upstream("C", "cid"))),
                                  "downstream of A.id": sorted(map(str, g.downstream("A", "id")))},
                 "ACTIVE 边上的闭包，方向稳定")


def test_retired_field_breaks_edges_without_losing_them(service):
    g = _seed(service)
    c = service.catalog
    c.retire_field("B", "bid")
    broken = g.broken_edges()
    assert len(broken) == 2
    assert {e.status for e in broken} == {EdgeStatus.BROKEN}

    # 影响分析不再穿越 BROKEN 边：C.cid 与 A.id 之间的链路被显式切断
    assert g.upstream("C", "cid") == set()
    assert g.downstream("A", "id") == set()
    # 但边没有被删除：可审计、可区分
    broken_pairs = {(e.source, e.target) for e in g.broken_edges()}
    assert (("A", "id"), ("B", "bid")) in broken_pairs
    assert (("B", "bid"), ("C", "cid")) in broken_pairs
    assert len(g.edges(include_broken=False)) == 1
    log_decision("显式退役 B.bid", {"retired": "B.bid"},
                 "2 条边置 BROKEN 并保留；影响闭包不再穿越；A.name->B.bname 仍 ACTIVE")

    # 退役端点不能再登记新血缘
    expect_reason(lambda: g.add_edge(("A", "id"), ("B", "bid")),
                  RejectReason.ENDPOINT_RETIRED)


def test_rename_rewrites_lineage_endpoints(service):
    g = _seed(service)
    c = service.catalog
    c.rename_field("A", "name", "full_name")
    moved = [e for e in g.active_edges() if e.target == ("B", "bname") and e.source[0] == "A"]
    assert len(moved) == 1
    assert moved[0].source == ("A", "full_name")
    assert moved[0].target == ("B", "bname")
    assert moved[0].status is EdgeStatus.ACTIVE

    # 血缘连续：新端点上的上下游仍可查
    assert g.downstream("A", "full_name") == {("B", "bname")}
    # 旧名登记被显式拒绝，不错连
    exc = expect_reason(lambda: g.add_edge(("A", "name"), ("C", "cid")),
                        RejectReason.ENDPOINT_NOT_FOUND)
    assert "full_name" in exc.message
    log_decision("显式改名 A.name->A.full_name",
                 {"rename": "A.name->A.full_name"},
                 "边端点原子改写、状态保持 ACTIVE；旧名登记被拒绝且提示新名")
