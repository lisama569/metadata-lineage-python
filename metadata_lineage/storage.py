"""内存存储：集中锁、实体/边/审计的持久化容器。

所有写操作必须持锁；读操作在锁内拷贝快照后返回，避免迭代期间被并发改写。
"""

from __future__ import annotations

import copy
import threading
from typing import Any

from .models import AuditEntry, Entity, LineageEdge, Ref


class CatalogStore:
    """线程安全的内存目录存储。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entities: dict[str, Entity] = {}
        self._edges: dict[str, LineageEdge] = {}
        # 端点对(src,dst) -> edge_id，用于识别重复登记
        self._edge_index: dict[tuple[Ref, Ref], str] = {}
        # 字段被哪些边引用：ref -> set(edge_id)，供结构演进时定位悬空边
        self._ref_index: dict[Ref, set[str]] = {}
        self._audit: list[AuditEntry] = []

    # ---- 锁与快照 -------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """在锁内导出可安全读取的深拷贝快照。"""
        with self._lock:
            return {
                "entities": copy.deepcopy(self._entities),
                "edges": copy.deepcopy(self._edges),
            }

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    # ---- 实体 -----------------------------------------------------
    def get_entity(self, entity_id: str) -> Entity | None:
        with self._lock:
            obj = self._entities.get(entity_id)
            return copy.deepcopy(obj) if obj is not None else None

    def put_entity(self, entity: Entity) -> None:
        with self._lock:
            self._entities[entity.entity_id] = copy.deepcopy(entity)

    # ---- 血缘边 ---------------------------------------------------
    def get_edge(self, edge_id: str) -> LineageEdge | None:
        with self._lock:
            edge = self._edges.get(edge_id)
            return copy.deepcopy(edge) if edge is not None else None

    def find_edge(self, src: Ref, dst: Ref) -> LineageEdge | None:
        with self._lock:
            edge_id = self._edge_index.get((src, dst))
            if edge_id is None:
                return None
            return copy.deepcopy(self._edges[edge_id])

    def add_edge(self, edge: LineageEdge) -> None:
        with self._lock:
            self._edges[edge.edge_id] = copy.deepcopy(edge)
            self._edge_index[(edge.src, edge.dst)] = edge.edge_id
            self._ref_index.setdefault(edge.src, set()).add(edge.edge_id)
            self._ref_index.setdefault(edge.dst, set()).add(edge.edge_id)

    def edges(self) -> list[LineageEdge]:
        with self._lock:
            return [copy.deepcopy(e) for e in self._edges.values()]

    def edges_touching(self, ref: Ref) -> list[LineageEdge]:
        with self._lock:
            ids = self._ref_index.get(ref, set())
            return [copy.deepcopy(self._edges[i]) for i in sorted(ids)]

    def deactivate_edges(self, edge_ids: set[str]) -> int:
        """把指定边置为 inactive（结构演进断开悬空血缘时使用）。"""
        with self._lock:
            n = 0
            for eid in edge_ids:
                edge = self._edges.get(eid)
                if edge is not None and edge.active:
                    edge.active = False
                    n += 1
            return n

    # ---- 审计 -----------------------------------------------------
    def append_audit(self, entry: AuditEntry) -> None:
        # 审计只追加、永不修改/删除；锁保证顺序与可见性。
        with self._lock:
            self._audit.append(entry)

    def audit_log(self) -> list[AuditEntry]:
        with self._lock:
            return list(self._audit)
