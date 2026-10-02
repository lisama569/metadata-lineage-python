"""目录与血缘服务：结构兼容判定、血缘登记/影响分析、属主标签并发更新。

约定：
- 每个公共方法的每次调用（成功 / 被拒绝 / 输入校验失败）都追加一条审计。
- 所有“读-判-写”序列在 store 的同一把锁内完成，避免并发交错。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .models import (
    AuditEntry,
    BatchItemResult,
    BatchItemStatus,
    BatchUpdateResult,
    BreakingChange,
    CompatibilityReport,
    Entity,
    EntityVersion,
    ErrorCode,
    FieldSpec,
    FieldVersion,
    LineageEdge,
    Ref,
    ServiceError,
    new_id,
    utcnow,
)
from .storage import CatalogStore

# 类型宽化表：old_type 可以安全演进为其中任意 new_type。
# 反向出现即“类型收窄”；两向都不沾即“不兼容类型变更”。
WIDENING: dict[Any, frozenset[Any]] = {
    # FieldType 枚举在下方 _is_widening 中按名字比较，避免导入期耦合
}

_WIDEN: dict[str, frozenset[str]] = {
    "INT32": frozenset({"INT64", "FLOAT64", "DECIMAL", "STRING"}),
    "INT64": frozenset({"FLOAT64", "DECIMAL", "STRING"}),
    "FLOAT64": frozenset({"DECIMAL", "STRING"}),
    "DECIMAL": frozenset({"STRING"}),
    "BOOL": frozenset({"STRING"}),
    "DATE": frozenset({"TIMESTAMP", "STRING"}),
    "TIMESTAMP": frozenset({"STRING"}),
    "STRING": frozenset(),
    "BYTES": frozenset({"STRING"}),
}

_UNSET = object()

# 供 API 层区分“请求未提及该字段”与“显式传 null 清空”。
UNSET: Any = _UNSET


@dataclass(frozen=True)
class BatchOwnerTagsItem:
    """批量属主/标签更新中的单条指令。

    owner/replace_tags 使用 _UNSET 哨兵表示“本条未提及”，
    None 表示显式清空属主；空 set 表示显式清空标签集合。
    """

    entity_id: str
    expected_revision: int
    owner: str | None | object = _UNSET
    add_tags: frozenset[str] = frozenset()
    remove_tags: frozenset[str] = frozenset()
    replace_tags: frozenset[str] | None | object = _UNSET
    actor: str = "anonymous"


@dataclass(frozen=True)
class ImpactResult:
    """影响分析结论。

    active_edges：当前有效结构上参与传播的边。
    dangling_edges：因字段被删除（强制演进）而断开的边；不参与遍历，永不静默删除。
    truncated：结果是否因容量上限（max_depth/max_nodes）被截断。
    upstream_truncated/downstream_truncated：各方向是否触到上限
    （depth=深度上限，nodes=节点数上限）。
    limits：本次实际生效的容量参数（None 表示该方向不限）。
    """

    upstream: list[Ref]
    downstream: list[Ref]
    active_edges: list[LineageEdge]
    dangling_edges: list[LineageEdge]
    truncated: bool = False
    upstream_truncated: str | None = None
    downstream_truncated: str | None = None
    limits: dict[str, int | None] = field(default_factory=dict)


def _type_change(old: str, new: str) -> ErrorCode | None:
    """返回类型变更类别；None 表示兼容。"""
    if old == new:
        return None
    if new in _WIDEN.get(old, frozenset()):
        return None
    if old in _WIDEN.get(new, frozenset()):
        return ErrorCode.BREAKING_TYPE_NARROWING
    return ErrorCode.BREAKING_INCOMPATIBLE_TYPE


def check_compatibility(
    old_fields: list[FieldVersion], new_fields: list[FieldSpec],
) -> CompatibilityReport:
    """纯函数：判定新字段集相对旧版本是否兼容，并给出 uid 分配。

    调用方需先完成输入校验（字段名唯一、显式 uid 均存在）。
    """
    old_by_uid = {f.uid: f for f in old_fields}
    uid_by_name = {f.name: f.uid for f in old_fields}

    assignments: dict[str, str] = {}
    pairs: list[tuple[FieldVersion | None, FieldSpec, str]] = []
    for spec in new_fields:
        if spec.uid is not None:
            uid = spec.uid
            old = old_by_uid[uid]
        elif spec.name in uid_by_name:
            uid = uid_by_name[spec.name]
            old = old_by_uid[uid]
        else:
            uid = new_id()
            old = None
        assignments[spec.name] = uid
        pairs.append((old, spec, uid))

    breaking: list[BreakingChange] = []
    used_uids = {uid for old, _s, uid in pairs if old is not None}

    # 1) 删除：旧 uid 未出现在新版本中
    for old in sorted(old_fields, key=lambda f: f.name):
        if old.uid not in used_uids:
            breaking.append(BreakingChange(
                ErrorCode.BREAKING_FIELD_REMOVED, old.name,
                f"field '{old.name}' is absent from the new version",
            ))

    # 2) 逐字段：改名 / 类型 / 可空性；新字段要求可空
    for old, spec, _uid in pairs:
        if old is None:
            if not spec.nullable:
                breaking.append(BreakingChange(
                    ErrorCode.BREAKING_NULLABILITY_NARROWED, spec.name,
                    "newly added field must be nullable (no default support)",
                ))
            continue
        if old.name != spec.name:
            breaking.append(BreakingChange(
                ErrorCode.BREAKING_FIELD_RENAMED, spec.name,
                f"field renamed: '{old.name}' -> '{spec.name}' "
                f"(same uid {old.uid}); rename is a breaking change",
            ))
        tcode = _type_change(old.type.value, spec.type.value)
        if tcode is not None:
            breaking.append(BreakingChange(
                tcode, spec.name,
                f"type change {old.type.value} -> {spec.type.value}",
            ))
        if old.nullable and not spec.nullable:
            breaking.append(BreakingChange(
                ErrorCode.BREAKING_NULLABILITY_NARROWED, spec.name,
                f"nullability narrowed: nullable {old.nullable} -> {spec.nullable}",
            ))

    return CompatibilityReport(
        compatible=not breaking,
        breaking_changes=breaking,
        uid_assignments=assignments,
    )


def _breaking_serializable(report: CompatibilityReport) -> list[dict[str, Any]]:
    return [
        {"code": c.code.value, "field": c.field_name, "detail": c.detail}
        for c in report.breaking_changes
    ]


class CatalogService:
    def __init__(self, store: CatalogStore | None = None) -> None:
        self.store = store or CatalogStore()

    # ---- 审计辅助 -------------------------------------------------
    def _audit(
        self, *, actor: str, action: str, target: str, status: str,
        reason_code: ErrorCode | None = None, detail: dict[str, Any] | None = None,
        revision_before: int | None = None, revision_after: int | None = None,
    ) -> None:
        self.store.append_audit(AuditEntry(
            audit_id=new_id(),
            at=utcnow(),
            actor=actor,
            action=action,
            target=target,
            status=status,
            reason_code=reason_code.value if reason_code else None,
            detail=detail or {},
            revision_before=revision_before,
            revision_after=revision_after,
        ))

    def _reject(
        self, code: ErrorCode, *, actor: str, action: str, target: str,
        detail: str, extra: dict[str, Any] | None = None,
        revision_before: int | None = None,
    ) -> None:
        status = "validation_failed" if code == ErrorCode.VALIDATION_ERROR else "rejected"
        self._audit(
            actor=actor, action=action, target=target, status=status,
            reason_code=code, detail={"message": detail, **(extra or {})},
            revision_before=revision_before,
        )
        raise ServiceError(code, detail, extra)

    # ---- 输入校验 -------------------------------------------------
    @staticmethod
    def _validate_fields(fields: list[FieldSpec]) -> None:
        if not fields:
            raise ServiceError(
                ErrorCode.VALIDATION_ERROR, "at least one field is required")
        names: set[str] = set()
        for f in fields:
            if not f.name or not f.name.strip():
                raise ServiceError(
                    ErrorCode.VALIDATION_ERROR, "field name must be non-empty")
            if f.name in names:
                raise ServiceError(
                    ErrorCode.VALIDATION_ERROR,
                    f"duplicate field name in input: {f.name}")
            names.add(f.name)

    # ---- 实体登记与结构演进 ---------------------------------------
    def register_entity(
        self, entity_id: str, name: str, fields: list[FieldSpec],
        actor: str, note: str = "",
    ) -> Entity:
        action, target = "register_entity", entity_id
        if not entity_id or not entity_id.strip():
            self._reject(ErrorCode.VALIDATION_ERROR, actor=actor,
                         action=action, target=target or "<empty>",
                         detail="entity_id must be non-empty")
        if not name or not name.strip():
            self._reject(ErrorCode.VALIDATION_ERROR, actor=actor,
                         action=action, target=target,
                         detail="entity name must be non-empty")
        try:
            self._validate_fields(fields)
        except ServiceError as e:
            self._audit(actor=actor, action=action, target=target,
                        status="validation_failed", reason_code=e.code,
                        detail={"message": e.detail or ""})
            raise

        with self.store.lock:
            if self.store.get_entity(entity_id) is not None:
                self._reject(ErrorCode.ENTITY_ALREADY_EXISTS, actor=actor,
                             action=action, target=target,
                             detail=f"entity '{entity_id}' already registered")
            snapshots = [
                FieldVersion(uid=new_id(), name=s.name, type=s.type,
                             nullable=s.nullable)
                for s in fields
            ]
            version = EntityVersion(
                entity_id=entity_id, version=1, fields=snapshots,
                created_at=utcnow(), created_by=actor, note=note,
            )
            entity = Entity(
                entity_id=entity_id, name=name, versions=[version],
                owner=None, tags=set(), revision=0,
            )
            self.store.put_entity(entity)
            self._audit(actor=actor, action=action, target=target,
                        status="success",
                        detail={"version": 1, "field_count": len(snapshots)})
            return entity

    def evolve_entity(
        self, entity_id: str, fields: list[FieldSpec], actor: str,
        note: str = "", allow_breaking: bool = False,
    ) -> tuple[Entity, CompatibilityReport]:
        action, target = "evolve_entity", entity_id
        try:
            self._validate_fields(fields)
        except ServiceError as e:
            self._audit(actor=actor, action=action, target=target,
                        status="validation_failed", reason_code=e.code,
                        detail={"message": e.detail or ""})
            raise

        with self.store.lock:
            entity = self.store.get_entity(entity_id)
            if entity is None:
                self._reject(ErrorCode.ENTITY_NOT_FOUND, actor=actor,
                             action=action, target=target,
                             detail=f"entity '{entity_id}' not found")
            old_version = entity.latest_version
            assert old_version is not None
            old_by_uid = {f.uid: f for f in old_version.fields}

            # 显式 uid 必须在旧版本中存在；多个新字段不得映射到同一 uid
            for s in fields:
                if s.uid is not None and s.uid not in old_by_uid:
                    self._reject(
                        ErrorCode.VALIDATION_ERROR, actor=actor,
                        action=action, target=target,
                        detail=f"field uid {s.uid} not found in current version")
            report = check_compatibility(old_version.fields, fields)
            if len(set(report.uid_assignments.values())) != len(fields):
                self._reject(
                    ErrorCode.VALIDATION_ERROR, actor=actor,
                    action=action, target=target,
                    detail="multiple new fields map to the same field uid")

            if report.breaking_changes and not allow_breaking:
                first = report.breaking_changes[0]
                self._reject(
                    first.code, actor=actor, action=action, target=target,
                    detail=first.detail,
                    extra={"breaking_changes": _breaking_serializable(report)},
                )

            new_snapshots = [
                FieldVersion(
                    uid=report.uid_assignments[s.name], name=s.name,
                    type=s.type, nullable=s.nullable,
                )
                for s in fields
            ]
            new_version = EntityVersion(
                entity_id=entity_id, version=old_version.version + 1,
                fields=new_snapshots, created_at=utcnow(),
                created_by=actor, note=note,
            )
            entity.versions.append(new_version)

            # 强制演进语义：被删字段的血缘边置为 inactive（悬空保留，不静默删除）；
            # 改名沿用同一 uid，边保持 active，自动跟随新名字。
            deactivated: list[str] = []
            removed_uids = {
                f.uid for f in old_version.fields
                if f.uid not in set(report.uid_assignments.values())
            }
            if removed_uids:
                removed_refs = {Ref(entity_id, uid) for uid in removed_uids}
                edge_ids = {
                    e.edge_id for e in self.store.edges()
                    if e.active and (e.src in removed_refs or e.dst in removed_refs)
                }
                self.store.deactivate_edges(edge_ids)
                deactivated = sorted(edge_ids)

            self.store.put_entity(entity)
            self._audit(
                actor=actor, action=action, target=target, status="success",
                detail={
                    "version": new_version.version,
                    "compatible": report.compatible,
                    "breaking_changes": _breaking_serializable(report),
                    "deactivated_lineage_edges": deactivated,
                },
            )
            return entity, report

    def get_entity(self, entity_id: str) -> Entity:
        entity = self.store.get_entity(entity_id)
        if entity is None:
            err = ServiceError(
                ErrorCode.ENTITY_NOT_FOUND,
                f"entity '{entity_id}' not found")
            self._audit(actor="anonymous", action="get_entity",
                        target=entity_id, status="rejected",
                        reason_code=err.code, detail={"message": err.detail})
            raise err
        return entity

    def resolve_ref(self, entity_id: str, field_name: str,
                    version: int | None = None) -> Ref:
        entity = self.store.get_entity(entity_id)
        if entity is None:
            raise ServiceError(
                ErrorCode.ENTITY_NOT_FOUND, f"entity '{entity_id}' not found")
        vobj = entity.latest_version
        if version is not None:
            vobj = next((v for v in entity.versions if v.version == version), None)
            if vobj is None:
                raise ServiceError(
                    ErrorCode.VERSION_NOT_FOUND,
                    f"version {version} of entity '{entity_id}' not found")
        assert vobj is not None
        fobj = vobj.field_by_name(field_name)
        if fobj is None:
            raise ServiceError(
                ErrorCode.FIELD_NOT_FOUND,
                f"field '{field_name}' not found in {entity_id}@v{vobj.version}")
        return Ref(entity_id=entity_id, field_uid=fobj.uid)

    def _validate_ref_current(self, ref: Ref) -> None:
        """端点必须在实体最新版本中仍然存在（被删字段的引用不可登记血缘）。"""
        entity = self.store.get_entity(ref.entity_id)
        if entity is None:
            raise ServiceError(
                ErrorCode.LINEAGE_ENDPOINT_NOT_FOUND,
                f"entity '{ref.entity_id}' not found")
        latest = entity.latest_version
        assert latest is not None
        if latest.field_by_uid(ref.field_uid) is None:
            raise ServiceError(
                ErrorCode.LINEAGE_FIELD_NOT_FOUND,
                f"field uid {ref.field_uid} does not exist in latest version "
                f"of '{ref.entity_id}' (removed or renamed-away)")

    # ---- 血缘 -----------------------------------------------------
    def register_lineage(self, src: Ref, dst: Ref, actor: str) -> LineageEdge:
        action = "register_lineage"
        target = f"{src.entity_id}:{src.field_uid}->{dst.entity_id}:{dst.field_uid}"
        with self.store.lock:
            try:
                self._validate_ref_current(src)
                self._validate_ref_current(dst)
            except ServiceError as e:
                self._audit(actor=actor, action=action, target=target,
                            status="validation_failed", reason_code=e.code,
                            detail={"message": e.detail or ""})
                raise

            if src == dst:
                self._reject(ErrorCode.LINEAGE_CYCLE, actor=actor,
                             action=action, target=target,
                             detail="self-referencing edge is a cycle")

            if self.store.find_edge(src, dst) is not None:
                self._reject(ErrorCode.LINEAGE_DUPLICATE, actor=actor,
                             action=action, target=target,
                             detail="identical lineage edge already registered")

            if self._reachable(dst, src, active_only=True):
                self._reject(
                    ErrorCode.LINEAGE_CYCLE, actor=actor, action=action,
                    target=target,
                    detail=f"adding edge would create a cycle: "
                           f"{dst.entity_id}:{dst.field_uid} already reaches "
                           f"{src.entity_id}:{src.field_uid}")

            edge = LineageEdge(
                edge_id=new_id(), src=src, dst=dst,
                created_at=utcnow(), created_by=actor, active=True,
            )
            self.store.add_edge(edge)
            self._audit(actor=actor, action=action, target=target,
                        status="success", detail={"edge_id": edge.edge_id})
            return edge

    def _reachable(self, start: Ref, goal: Ref, active_only: bool) -> bool:
        """沿出边判断 start 是否能到达 goal。"""
        edges = [e for e in self.store.edges() if e.active or not active_only]
        adj: dict[Ref, list[Ref]] = {}
        for e in sorted(edges, key=lambda x: x.edge_id):
            adj.setdefault(e.src, []).append(e.dst)
        seen = {start}
        queue = deque([start])
        while queue:
            cur = queue.popleft()
            for nxt in adj.get(cur, []):
                if nxt == goal:
                    return True
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        return False

    def impact(
        self, ref: Ref,
        max_upstream_depth: int | None = None,
        max_downstream_depth: int | None = None,
        max_upstream_nodes: int | None = None,
        max_downstream_nodes: int | None = None,
    ) -> ImpactResult:
        """字段级影响分析，带容量保护的双向 BFS。

        - 仅 active 边参与遍历（悬空边永不参与传播）；
        - max_*_depth：最多追多少层（按边计，0 表示不追）；
        - max_*_nodes：每个方向最多返回多少个节点（不含起点）；
        - 触到任一上限：结果标记 truncated，并在对应方向记录截断原因
          （"depth" 或 "nodes"）；
        - 顺序确定性：每层邻居按 (entity_id, field_uid) 排序后依次入队，
          同图同输入多次调用结果一致。
        """
        try:
            self._validate_ref_current(ref)
        except ServiceError:
            raise
        limits = {
            "max_upstream_depth": max_upstream_depth,
            "max_downstream_depth": max_downstream_depth,
            "max_upstream_nodes": max_upstream_nodes,
            "max_downstream_nodes": max_downstream_nodes,
        }
        bad = [k for k, v in limits.items() if v is not None and v < 0]
        if bad:
            self._audit(
                actor="anonymous", action="impact",
                target=f"{ref.entity_id}:{ref.field_uid}",
                status="validation_failed", reason_code=ErrorCode.VALIDATION_ERROR,
                detail={"message": "limit must be >= 0", "invalid": bad},
            )
            raise ServiceError(
                ErrorCode.VALIDATION_ERROR,
                f"limit must be >= 0: {', '.join(bad)}")
        all_edges = self.store.edges()
        active = [e for e in all_edges if e.active]
        dangling = [
            e for e in all_edges
            if not e.active and (e.src == ref or e.dst == ref)
        ]

        def walk(forward: bool, max_depth: int | None,
                 max_nodes: int | None
                 ) -> tuple[list[Ref], list[LineageEdge], str | None]:
            # 邻接表：每个源点的邻居按确定性顺序排序
            adj: dict[Ref, list[tuple[LineageEdge, Ref]]] = {}
            for e in active:
                key, nxt = (e.src, e.dst) if forward else (e.dst, e.src)
                adj.setdefault(key, []).append((e, nxt))
            for neighbors in adj.values():
                neighbors.sort(key=lambda pair: (pair[1].entity_id,
                                                 pair[1].field_uid))

            order: list[Ref] = []
            used: list[LineageEdge] = []
            seen_edges: set[str] = set()
            reason: str | None = None

            if max_depth is not None and max_depth <= 0:
                return order, used, reason

            seen = {ref}
            # 每层是一组待展开的节点；depth = 该层距起点的层数
            frontier = [ref]
            depth = 0
            while frontier and (max_depth is None or depth < max_depth):
                depth += 1
                next_frontier: list[Ref] = []
                depth_cut = False
                for cur in frontier:
                    for edge, nxt in adj.get(cur, []):
                        if edge.edge_id not in seen_edges:
                            seen_edges.add(edge.edge_id)
                            used.append(edge)
                        if nxt in seen:
                            continue
                        if max_nodes is not None and len(order) >= max_nodes:
                            reason = reason or "nodes"
                            depth_cut = True
                            break
                        seen.add(nxt)
                        order.append(nxt)
                        next_frontier.append(nxt)
                    if depth_cut:
                        break
                if depth_cut:
                    break
                frontier = next_frontier
                if max_depth is not None and depth >= max_depth:
                    # 边界之外仍存在未访问节点才计为深度截断
                    # （菱形收敛到已访问节点不算遗漏）
                    beyond = any(
                        nxt not in seen
                        for node in next_frontier
                        for _edge, nxt in adj.get(node, [])
                    )
                    if beyond:
                        reason = reason or "depth"
                    break
            return order, used, reason

        upstream, up_edges, up_reason = walk(
            forward=False, max_depth=max_upstream_depth,
            max_nodes=max_upstream_nodes)
        downstream, down_edges, down_reason = walk(
            forward=True, max_depth=max_downstream_depth,
            max_nodes=max_downstream_nodes)
        edge_ids = ({e.edge_id for e in up_edges}
                    | {e.edge_id for e in down_edges})
        active_edges = [
            e for e in sorted(active, key=lambda x: x.edge_id)
            if e.edge_id in edge_ids
        ]
        return ImpactResult(
            upstream=upstream,
            downstream=downstream,
            active_edges=active_edges,
            dangling_edges=sorted(dangling, key=lambda x: x.edge_id),
            truncated=up_reason is not None or down_reason is not None,
            upstream_truncated=up_reason,
            downstream_truncated=down_reason,
            limits=limits,
        )

    # ---- 属主与标签（乐观锁） -------------------------------------
    @staticmethod
    def _validate_tag_values(tags: set[str]) -> list[str]:
        return sorted(t for t in tags if not isinstance(t, str) or not t or not t.strip())

    def update_owner_tags(
        self, entity_id: str, *, expected_revision: int,
        owner: str | None | object = _UNSET, add_tags: set[str] | None = None,
        remove_tags: set[str] | None = None,
        replace_tags: set[str] | None | object = _UNSET,
        actor: str = "anonymous",
    ) -> tuple[Entity, int]:
        """按字段独立更新属主与标签。

        - owner 未传入（_UNSET）：保持原值；传入 None：显式清空；传入 str：设置。
        - replace_tags 传入：整体替换标签集合（传空集合即清空标签），
          不得与 add_tags/remove_tags 同时出现；二者皆未传：标签原样不动。
        """
        action = "update_owner_tags"
        add_tags = set(add_tags or ())
        remove_tags = set(remove_tags or ())
        replace_mode = replace_tags is not _UNSET
        if replace_mode and not isinstance(replace_tags, set):
            self._audit(
                actor=actor, action=action, target=entity_id,
                status="validation_failed", reason_code=ErrorCode.VALIDATION_ERROR,
                detail={"message": "replace_tags must be a set (use empty set to "
                                   "clear all tags); null is not allowed"},
            )
            raise ServiceError(
                ErrorCode.VALIDATION_ERROR,
                "replace_tags must be a set (use empty set to clear all tags); "
                "null is not allowed")
        replace_set = set(replace_tags) if replace_mode else set()

        def _validation_fail(message: str, extra: dict[str, Any] | None = None):
            self._audit(
                actor=actor, action=action, target=entity_id,
                status="validation_failed", reason_code=ErrorCode.VALIDATION_ERROR,
                detail={"message": message, **(extra or {})},
            )
            raise ServiceError(ErrorCode.VALIDATION_ERROR, message, extra)

        if replace_mode and (add_tags or remove_tags):
            _validation_fail(
                "replace_tags cannot be combined with add_tags/remove_tags",
                {"add_tags": sorted(add_tags), "remove_tags": sorted(remove_tags)})
        if add_tags & remove_tags:
            _validation_fail(
                "same tag appears in both add_tags and remove_tags",
                {"tags": sorted(add_tags & remove_tags)})
        tag_pool = replace_set if replace_mode else (add_tags | remove_tags)
        bad = self._validate_tag_values(tag_pool)
        if bad:
            _validation_fail("tag must be non-empty", {"tags": bad})
        if owner is not _UNSET and owner is not None and (
                not isinstance(owner, str) or not owner.strip()):
            _validation_fail("owner must be a non-empty string or null to clear")

        with self.store.lock:
            entity = self.store.get_entity(entity_id)
            if entity is None:
                self._reject(ErrorCode.ENTITY_NOT_FOUND, actor=actor,
                             action=action, target=entity_id,
                             detail=f"entity '{entity_id}' not found")
            rev_before = entity.revision
            if rev_before != expected_revision:
                self._reject(
                    ErrorCode.CONFLICT_REVISION, actor=actor,
                    action=action, target=entity_id,
                    detail=(f"stale write: expected revision {expected_revision}, "
                            f"current is {rev_before}"),
                    extra={"current_revision": rev_before},
                    revision_before=rev_before,
                )

            owner_mode = "unchanged"
            if owner is not _UNSET:
                entity.owner = None if owner is None else str(owner)
                owner_mode = "cleared" if owner is None else "set"
            tag_mode = "unchanged"
            if replace_mode:
                entity.tags = set(replace_set)
                tag_mode = "replace"
            elif add_tags or remove_tags:
                entity.tags = (entity.tags | add_tags) - remove_tags
                tag_mode = "add_remove"
            entity.revision = rev_before + 1
            self.store.put_entity(entity)
            self._audit(
                actor=actor, action=action, target=entity_id, status="success",
                detail={"owner": entity.owner, "owner_mode": owner_mode,
                        "tags": sorted(entity.tags), "tag_mode": tag_mode},
                revision_before=rev_before, revision_after=entity.revision,
            )
            return entity, entity.revision

    # ---- 属主与标签：批量原子更新 ---------------------------------
    def batch_update_owner_tags(
        self, items: list[BatchOwnerTagsItem],
    ) -> BatchUpdateResult:
        """整批属主/标签更新：全部成功才提交，任一失败整批不落库。

        每个实体按各自 expected_revision 校验；每条尝试（含最终因同批
        他人失败而被跳过的条目）都独立写一条审计。
        """
        action = "batch_update_owner_tags"
        batch_id = new_id()

        def _audit_item(item: BatchOwnerTagsItem, status: str,
                        reason: ErrorCode | None, message: str,
                        rev_before: int | None = None,
                        rev_after: int | None = None,
                        extra: dict[str, Any] | None = None) -> None:
            self._audit(
                actor=item.actor, action=action, target=item.entity_id,
                status=("validation_failed"
                        if reason == ErrorCode.VALIDATION_ERROR else status),
                reason_code=reason,
                detail={"message": message, "batch_id": batch_id,
                        **(extra or {})},
                revision_before=rev_before, revision_after=rev_after,
            )

        with self.store.lock:
            if not items:
                self._audit(
                    actor="anonymous", action=action, target="<batch>",
                    status="validation_failed",
                    reason_code=ErrorCode.VALIDATION_ERROR,
                    detail={"message": "batch must contain at least one item",
                            "batch_id": batch_id},
                )
                raise ServiceError(
                    ErrorCode.VALIDATION_ERROR,
                    "batch must contain at least one item")

            results: list[BatchItemResult] = [None] * len(items)  # type: ignore[list-item]
            # entity_id -> 已通过校验的首个条目下标；同实体重复视为冲突
            seen: dict[str, int] = {}
            pending: list[tuple[int, BatchOwnerTagsItem, Entity, int]] = []

            for i, item in enumerate(items):
                def fail(code: ErrorCode, message: str,
                         current: int | None, *,
                         status_override: str | None = None) -> None:
                    _audit_item(item, "rejected", code, message,
                                rev_before=current,
                                extra={"index": i, "batch_size": len(items)})
                    results[i] = BatchItemResult(
                        index=i, entity_id=item.entity_id,
                        status=(status_override or
                                BatchItemStatus[code.name].value),
                        reason_code=code.value, detail=message,
                        current_revision=current, revision_after=None)

                # 1) 输入校验
                problem: tuple[str, dict[str, Any]] | None = None
                replace_mode = item.replace_tags is not _UNSET
                if not item.entity_id or not item.entity_id.strip():
                    problem = ("entity_id must be non-empty", {})
                elif replace_mode and not isinstance(item.replace_tags, frozenset):
                    problem = ("replace_tags must be a list of strings "
                               "(use [] to clear); null is not allowed", {})
                elif replace_mode and (item.add_tags or item.remove_tags):
                    problem = ("replace_tags cannot be combined with "
                               "add_tags/remove_tags", {})
                elif item.add_tags & item.remove_tags:
                    problem = ("same tag appears in both add_tags and remove_tags",
                               {"tags": sorted(item.add_tags & item.remove_tags)})
                elif self._validate_tag_values(
                        set(item.replace_tags) if replace_mode
                        else set(item.add_tags | item.remove_tags)):
                    problem = ("tag must be non-empty", {})
                elif item.owner is not _UNSET and item.owner is not None and (
                        not isinstance(item.owner, str) or not item.owner.strip()):
                    problem = ("owner must be a non-empty string or null to clear",
                               {})
                if problem is not None:
                    msg, extra = problem
                    _audit_item(item, "rejected", ErrorCode.VALIDATION_ERROR,
                                msg, extra={"index": i, **extra})
                    results[i] = BatchItemResult(
                        index=i, entity_id=item.entity_id,
                        status=BatchItemStatus.VALIDATION_ERROR.value,
                        reason_code=ErrorCode.VALIDATION_ERROR.value,
                        detail=msg, current_revision=None, revision_after=None)
                    continue

                # 2) 实体存在性
                entity = self.store.get_entity(item.entity_id)
                if entity is None:
                    fail(ErrorCode.ENTITY_NOT_FOUND,
                         f"entity '{item.entity_id}' not found", None)
                    continue

                # 3) 同批重复实体
                if item.entity_id in seen:
                    fail(ErrorCode.VALIDATION_ERROR,
                         f"entity '{item.entity_id}' appears more than once "
                         f"in the same batch (first at index {seen[item.entity_id]})",
                         entity.revision)
                    continue

                # 4) 乐观锁版本
                if entity.revision != item.expected_revision:
                    fail(ErrorCode.CONFLICT_REVISION,
                         f"stale write: expected revision "
                         f"{item.expected_revision}, current is {entity.revision}",
                         entity.revision)
                    continue

                seen[item.entity_id] = i
                pending.append((i, item, entity, entity.revision))

            # 只有所有条目都通过校验才提交
            committed = len(pending) == len(items)

            if committed:
                for i, item, entity, rev_before in pending:
                    if item.owner is not _UNSET:
                        entity.owner = (None if item.owner is None
                                        else str(item.owner))
                    if item.replace_tags is not _UNSET:
                        entity.tags = set(item.replace_tags)  # type: ignore[arg-type]
                    elif item.add_tags or item.remove_tags:
                        entity.tags = ((entity.tags | set(item.add_tags))
                                       - set(item.remove_tags))
                    entity.revision = rev_before + 1
                    self.store.put_entity(entity)
                    results[i] = BatchItemResult(
                        index=i, entity_id=item.entity_id,
                        status=BatchItemStatus.SUCCESS.value,
                        reason_code=None, detail=None,
                        current_revision=rev_before,
                        revision_after=entity.revision)
                    _audit_item(
                        item, "success", None,
                        f"committed in batch {batch_id}",
                        rev_before=rev_before, rev_after=entity.revision,
                        extra={"index": i, "owner": entity.owner,
                               "tags": sorted(entity.tags)})
            else:
                for i, item, entity, rev_before in pending:
                    results[i] = BatchItemResult(
                        index=i, entity_id=item.entity_id,
                        status=BatchItemStatus.BATCH_ABORTED.value,
                        reason_code=BatchItemStatus.BATCH_ABORTED.value,
                        detail=("item passed its own checks but was not written "
                                "because another item in the batch failed"),
                        current_revision=rev_before, revision_after=None)
                    _audit_item(
                        item, "rejected", None,
                        f"not written: batch {batch_id} aborted by another item",
                        rev_before=rev_before,
                        extra={"index": i, "status": "BATCH_ABORTED"})

            return BatchUpdateResult(
                batch_id=batch_id, committed=committed, results=list(results))

    # ---- 审计 -----------------------------------------------------
    def audit_log(self) -> list[AuditEntry]:
        return self.store.audit_log()
