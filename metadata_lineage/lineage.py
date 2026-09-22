"""血缘图：登记（查重/查环）、影响分析、字段退役/改名时的边维护。"""

from __future__ import annotations

import threading
from collections import deque
from typing import Callable

from .errors import MetadataError, RejectReason
from .models import EdgeStatus, LineageEdge, utc_now

# (entity_id, field_name)
Endpoint = tuple[str, str]


class LineageGraph:
    """
    字段级有向血缘图。

    * 边语义：source 产出 target（target 依赖 source）。
    * ACTIVE 边参与影响分析；BROKEN 边（字段被显式退役或改名撞键）保留但
      不参与遍历，可通过 broken_edges() 查询——绝不静默删除或错连。
    * 字段改名时相关边端点原子改写，血缘连续。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._edges: list[LineageEdge] = []
        # 端点存在性校验回调：(entity_id, field_name) -> None 或抛
        # ENDPOINT_RETIRED / ENDPOINT_NOT_FOUND。由 wiring 层注入。
        self._endpoint_exists: Callable[[Endpoint], None] | None = None

    def set_endpoint_resolver(self, resolver: Callable[[Endpoint], None]) -> None:
        self._endpoint_exists = resolver

    # --------------------------------------------------------------- 登记
    def add_edge(self, source: Endpoint, target: Endpoint) -> LineageEdge:
        """
        登记一条字段级血缘边。

        * 自环或登记后成环：拒绝 CYCLE_DETECTED
        * 完全相同的 (source,target) ACTIVE 边已存在：拒绝 DUPLICATE_EDGE
          （仅存在 BROKEN 同对边时重新激活该边）
        * 端点不存在：拒绝 ENDPOINT_NOT_FOUND；端点已退役：ENDPOINT_RETIRED
        """
        if source == target:
            raise MetadataError(
                RejectReason.CYCLE_DETECTED,
                f"self-loop not allowed: {source[0]}.{source[1]}",
            )
        with self._lock:
            if self._endpoint_exists is not None:
                self._endpoint_exists(source)
                self._endpoint_exists(target)

            same_pair = [e for e in self._edges if e.source == source and e.target == target]
            if any(e.status is EdgeStatus.ACTIVE for e in same_pair):
                raise MetadataError(
                    RejectReason.DUPLICATE_EDGE,
                    f"duplicate lineage edge: {source[0]}.{source[1]} -> {target[0]}.{target[1]}",
                )

            if self._would_cycle(source, target):
                raise MetadataError(
                    RejectReason.CYCLE_DETECTED,
                    f"edge would create a cycle: "
                    f"{source[0]}.{source[1]} -> {target[0]}.{target[1]}",
                )

            edge = LineageEdge(
                source=source, target=target,
                status=EdgeStatus.ACTIVE, registered_at=utc_now(),
            )
            if same_pair:
                # 复用原 BROKEN 边的登记时间，重新激活
                idx = self._edges.index(same_pair[0])
                self._edges[idx] = edge
            else:
                self._edges.append(edge)
            return edge

    def _active_pairs(self) -> list[tuple[Endpoint, Endpoint]]:
        return [(e.source, e.target) for e in self._edges if e.status is EdgeStatus.ACTIVE]

    def _would_cycle(self, source: Endpoint, target: Endpoint) -> bool:
        """加入 source->target 后，若从 target 沿下游可回到 source 则成环。"""
        out: dict[Endpoint, list[Endpoint]] = {}
        for s, t in self._active_pairs():
            out.setdefault(s, []).append(t)
        queue: deque[Endpoint] = deque([target])
        seen = {target}
        while queue:
            cur = queue.popleft()
            if cur == source:
                return True
            for nxt in out.get(cur, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        return False

    # ------------------------------------------------------------ 影响分析
    @staticmethod
    def _traverse(start: Endpoint, adj: dict[Endpoint, list[Endpoint]]) -> set[Endpoint]:
        result: set[Endpoint] = set()
        queue: deque[Endpoint] = deque([start])
        seen = {start}
        while queue:
            cur = queue.popleft()
            for nxt in adj.get(cur, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    result.add(nxt)
                    queue.append(nxt)
        return result

    def _impact(self, entity_id: str, field: str | None, reverse: bool) -> set[Endpoint]:
        pairs = self._active_pairs()
        adj: dict[Endpoint, list[Endpoint]] = {}
        for s, t in pairs:
            k, v = (t, s) if reverse else (s, t)
            adj.setdefault(k, []).append(v)
        if field is not None:
            starts = [(entity_id, field)]
        else:
            starts = sorted({ep for s, t in pairs for ep in (s, t) if ep[0] == entity_id})
        result: set[Endpoint] = set()
        for st in starts:
            result |= self._traverse(st, adj)
        return result

    def upstream(self, entity_id: str, field: str | None = None) -> set[Endpoint]:
        """上游影响范围：直接/间接产出目标的字段（ACTIVE 边反向）。"""
        with self._lock:
            return self._impact(entity_id, field, reverse=True)

    def downstream(self, entity_id: str, field: str | None = None) -> set[Endpoint]:
        """下游影响范围：直接/间接依赖目标的字段（ACTIVE 边正向）。"""
        with self._lock:
            return self._impact(entity_id, field, reverse=False)

    # --------------------------------------------- 字段退役/改名的稳定语义
    def on_field_retired(self, entity_id: str, field_name: str) -> list[LineageEdge]:
        """
        字段显式退役：引用该字段的 ACTIVE 边一律置 BROKEN 并保留。
        BROKEN 边不参与影响分析，可经 broken_edges() 显式审计。
        """
        ep = (entity_id, field_name)
        affected: list[LineageEdge] = []
        with self._lock:
            for i, edge in enumerate(self._edges):
                if edge.status is EdgeStatus.ACTIVE and ep in (edge.source, edge.target):
                    broken = LineageEdge(
                        source=edge.source, target=edge.target,
                        status=EdgeStatus.BROKEN, registered_at=edge.registered_at,
                    )
                    self._edges[i] = broken
                    affected.append(broken)
            return affected

    def on_field_renamed(
        self, entity_id: str, old_name: str, new_name: str
    ) -> list[LineageEdge]:
        """
        字段显式改名：引用旧端点的边端点原子改写。

        若改写后与另一条 ACTIVE 边端点完全相同（改名产生重复血缘），改写
        得到的边置为 BROKEN 保留，已存在的 ACTIVE 边不动——不覆盖、不错连、
        不丢失，差异可经 broken_edges() 审计。
        """
        old_ep, new_ep = (entity_id, old_name), (entity_id, new_name)
        with self._lock:
            existing_active = set(self._active_pairs())
            affected: list[LineageEdge] = []
            for i, edge in enumerate(self._edges):
                s, t = edge.source, edge.target
                if old_ep not in (s, t):
                    continue
                ns = new_ep if s == old_ep else s
                nt = new_ep if t == old_ep else t
                status = edge.status
                if status is EdgeStatus.ACTIVE and (ns, nt) in existing_active:
                    status = EdgeStatus.BROKEN
                moved = LineageEdge(
                    source=ns, target=nt, status=status,
                    registered_at=edge.registered_at,
                )
                self._edges[i] = moved
                affected.append(moved)
            return affected

    # ---------------------------------------------------------------- 查询
    def edges(self, include_broken: bool = True) -> list[LineageEdge]:
        with self._lock:
            out = [
                e for e in self._edges
                if include_broken or e.status is EdgeStatus.ACTIVE
            ]
            return sorted(out, key=lambda e: (e.source, e.target, e.status.name))

    def active_edges(self) -> list[LineageEdge]:
        with self._lock:
            return [e for e in self._edges if e.status is EdgeStatus.ACTIVE]

    def broken_edges(self) -> list[LineageEdge]:
        with self._lock:
            return [e for e in self._edges if e.status is EdgeStatus.BROKEN]
