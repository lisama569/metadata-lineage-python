"""目录与血缘服务：结构兼容判定、血缘登记/影响分析、属主标签并发更新。

约定：
- 每个公共方法的每次调用（成功 / 被拒绝 / 输入校验失败）都追加一条审计。
- 所有“读-判-写”序列在 store 的同一把锁内完成，避免并发交错。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

from .models import (
    AuditEntry,
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


@dataclass(frozen=True)
class ImpactResult:
    """影响分析结论。

    active_edges：当前有效结构上参与传播的边（结果节点子图内部的边）。
    dangling_edges：因字段被删除（强制演进）而断开的边；不参与遍历，永不静默删除。
    truncated：是否因容量上限（max_depth / max_nodes）挡住了真实存在但
        未返回的可达节点。边界节点的边只回指/汇合到已返回节点、或上限
        恰好覆盖全部可达节点时，结果完整，该标志为 False。
    """

    upstream: list[Ref]
    downstream: list[Ref]
    active_edges: list[LineageEdge]
    dangling_edges: list[LineageEdge]
    truncated: bool = False


@dataclass(frozen=True)
class OwnerTagsUpdate:
    """批量属主/标签更新中的单条改动。

    owner / set_tags 的“未提到”用 _UNSET 表示；owner=None 表示显式清空属主。
    """

    entity_id: str
    expected_revision: int
    owner: object = _UNSET
    set_tags: object = _UNSET
    add_tags: frozenset[str] = frozenset()
    remove_tags: frozenset[str] = frozenset()
    actor: str = "anonymous"


@dataclass(frozen=True)
class BatchItemResult:
    """批量更新中单个实体的逐条结论。

    status: success | conflict | validation_failed | not_found | aborted
    （aborted = 本条自身合法，但因整批原子性未落库）。
    """

    entity_id: str
    status: str
    reason_code: str | None
    revision: int | None
    detail: str


@dataclass(frozen=True)
class BatchResult:
    """批量原子更新结论：applied=False 时任何改动都未落库。"""

    applied: bool
    results: list[BatchItemResult]


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

    def impact(self, ref: Ref, *, max_depth: int | None = None,
               max_nodes: int | None = None) -> ImpactResult:
        """上下游影响分析，可选容量上限。

        - max_depth：沿每个方向最多追多少层（1 = 仅直接邻居）；
        - max_nodes：每个方向最多返回多少个节点。

        截断判定（truncated）只在“确实还有可达节点没返回”时成立：遍历
        结束后若存在一条有效边，其起点已在结果中、终点不在结果中，说明
        上限挡住了真实存在的节点。分支汇合、边界节点的边回指已出现节点等
        情形不构成遗漏，即使触到上限也判完整；max_nodes 恰好等于全部可达
        节点数时同样不截断。遍历顺序按 edge_id 排序的 BFS，同样输入多次
        调用结论一致。悬空边不参与遍历。
        """
        for name, limit in (("max_depth", max_depth), ("max_nodes", max_nodes)):
            if limit is not None and (not isinstance(limit, int) or limit < 1):
                raise ServiceError(
                    ErrorCode.VALIDATION_ERROR,
                    f"{name} must be a positive integer, got {limit!r}")
        try:
            self._validate_ref_current(ref)
        except ServiceError:
            raise
        active = [e for e in self.store.edges() if e.active]
        dangling = [
            e for e in self.store.edges()
            if not e.active and (e.src == ref or e.dst == ref)
        ]

        def walk(forward: bool) -> tuple[list[Ref], list[LineageEdge], bool]:
            def ends(e: LineageEdge) -> tuple[Ref, Ref]:
                # 正向：沿 src -> dst；反向：沿 dst -> src
                return (e.src, e.dst) if forward else (e.dst, e.src)

            adj: dict[Ref, list[tuple[LineageEdge, Ref]]] = {}
            for e in sorted(active, key=lambda x: x.edge_id):
                tail, head = ends(e)
                adj.setdefault(tail, []).append((e, head))
            order: list[Ref] = []
            seen = {ref}
            queue: deque[tuple[Ref, int]] = deque([(ref, 0)])
            while queue:
                cur, depth = queue.popleft()
                if max_nodes is not None and len(order) >= max_nodes:
                    break  # 容量已取满：剩余队列不再展开（是否截断事后判定）
                if max_depth is not None and depth >= max_depth:
                    continue  # 深度边界：本节点不向外展开
                for _edge, nxt in adj.get(cur, []):
                    if nxt not in seen:
                        seen.add(nxt)
                        order.append(nxt)
                        queue.append((nxt, depth + 1))
                        if max_nodes is not None and len(order) >= max_nodes:
                            break  # 恰好取满 max_nodes 个

            # 截断判据：有效边 tail 已返回而 head 未返回 = 上限挡住了真实
            # 存在的可达节点。回边 / 指向已出现节点的边（分支汇合、边界
            # 回流）两端都在 seen 内，不算遗漏；全部可达节点均返回时该
            # 条件不可能成立。悬空边不在 active 中，永不触发。
            truncated = False
            for e in active:
                tail, head = ends(e)
                if tail in seen and head not in seen:
                    truncated = True
                    break

            # 参与传播的边 = 结果节点子图内部、两端都已返回的有效边
            used_edges = [
                e for e in active
                if ends(e)[0] in seen and ends(e)[1] in seen
            ]
            return order, used_edges, truncated

        upstream, up_edges, up_trunc = walk(forward=False)
        downstream, down_edges, down_trunc = walk(forward=True)
        edge_ids = {e.edge_id for e in up_edges} | {e.edge_id for e in down_edges}
        active_edges = [
            e for e in sorted(active, key=lambda x: x.edge_id)
            if e.edge_id in edge_ids
        ]
        return ImpactResult(
            upstream=upstream,
            downstream=downstream,
            active_edges=active_edges,
            dangling_edges=sorted(dangling, key=lambda x: x.edge_id),
            truncated=up_trunc or down_trunc,
        )

    # ---- 属主与标签（乐观锁） -------------------------------------
    @staticmethod
    def _validate_owner_tags_input(
        owner: object, set_tags: object,
        add_tags: set[str], remove_tags: set[str],
    ) -> None:
        """属主/标签更新的输入校验（单个与批量共用）。

        - set_tags（整体替换）与 add/remove（增量改动）互斥；
        - 同一标签不得同时出现在 add 与 remove；
        - 标签必须非空；属主若显式给出必须是非空字符串或 None（清空）。
        """
        if set_tags is not _UNSET and (add_tags or remove_tags):
            raise ServiceError(
                ErrorCode.VALIDATION_ERROR,
                "set_tags (full replace) cannot be combined with "
                "add_tags/remove_tags (incremental)")
        if add_tags & remove_tags:
            raise ServiceError(
                ErrorCode.VALIDATION_ERROR,
                "same tag appears in both add_tags and remove_tags")
        all_tags = set(add_tags | remove_tags)
        if set_tags is not _UNSET:
            all_tags |= set(set_tags)  # type: ignore[arg-type]
        if any(not t or not t.strip() for t in all_tags):
            raise ServiceError(
                ErrorCode.VALIDATION_ERROR, "tag must be non-empty")
        if owner is not _UNSET and owner is not None:
            if not isinstance(owner, str) or not owner.strip():
                raise ServiceError(
                    ErrorCode.VALIDATION_ERROR,
                    "owner must be a non-empty string, null (clear), "
                    "or omitted (keep)")

    @staticmethod
    def _apply_owner_tags(
        entity: Entity, *, owner: object, set_tags: object,
        add_tags: set[str], remove_tags: set[str],
    ) -> None:
        """按“未提到即保持原值”的语义应用属主/标签改动（就地修改）。"""
        if owner is not _UNSET:
            entity.owner = None if owner is None else str(owner)
        if set_tags is not _UNSET:
            entity.tags = set(set_tags)  # type: ignore[arg-type]
        else:
            entity.tags = (entity.tags | add_tags) - remove_tags

    def update_owner_tags(
        self, entity_id: str, *, expected_revision: int,
        owner: str | None | object = _UNSET,
        set_tags: set[str] | object = _UNSET,
        add_tags: set[str] | None = None,
        remove_tags: set[str] | None = None, actor: str = "anonymous",
    ) -> tuple[Entity, int]:
        action = "update_owner_tags"
        add_tags = set(add_tags or ())
        remove_tags = set(remove_tags or ())
        try:
            self._validate_owner_tags_input(owner, set_tags, add_tags, remove_tags)
        except ServiceError as e:
            self._audit(
                actor=actor, action=action, target=entity_id,
                status="validation_failed", reason_code=e.code,
                detail={"message": e.detail or ""},
            )
            raise

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

            self._apply_owner_tags(
                entity, owner=owner, set_tags=set_tags,
                add_tags=add_tags, remove_tags=remove_tags)
            entity.revision = rev_before + 1
            self.store.put_entity(entity)
            self._audit(
                actor=actor, action=action, target=entity_id, status="success",
                detail={"owner": entity.owner, "tags": sorted(entity.tags)},
                revision_before=rev_before, revision_after=entity.revision,
            )
            return entity, entity.revision

    def batch_update_owner_tags(
        self, items: list[OwnerTagsUpdate],
    ) -> BatchResult:
        """批量原子更新：整批要么全部落库、要么全部不生效。

        每个实体按自己的 expected_revision 做乐观锁校验；任何一条失败
        （版本过期 / 输入不合法 / 实体不存在）都会使整批不落库，但每条
        尝试都会留下独立审计，返回值逐条给出结论。
        """
        action = "batch_update_owner_tags"
        if not items:
            self._reject(ErrorCode.VALIDATION_ERROR, actor="anonymous",
                         action=action, target="<batch>",
                         detail="batch must contain at least one item")

        # 同一实体在一批中出现两次：语义含糊，两条都判为校验失败
        counts: dict[str, int] = {}
        for it in items:
            counts[it.entity_id] = counts.get(it.entity_id, 0) + 1

        results: list[BatchItemResult] = []
        # (entity, item) 仅收集自身校验通过的条目，待整批一致通过后才落库
        pending: list[tuple[Entity, OwnerTagsUpdate]] = []

        with self.store.lock:
            for it in items:
                fail: tuple[str, ErrorCode, str] | None = None
                entity: Entity | None = None
                rev_before: int | None = None

                if counts[it.entity_id] > 1:
                    fail = ("validation_failed", ErrorCode.VALIDATION_ERROR,
                            "entity appears more than once in the same batch")
                else:
                    try:
                        self._validate_owner_tags_input(
                            it.owner, it.set_tags,
                            set(it.add_tags), set(it.remove_tags))
                    except ServiceError as e:
                        fail = ("validation_failed", e.code, e.detail or "")
                    else:
                        entity = self.store.get_entity(it.entity_id)
                        if entity is None:
                            fail = ("not_found", ErrorCode.ENTITY_NOT_FOUND,
                                    f"entity '{it.entity_id}' not found")
                        else:
                            rev_before = entity.revision
                            if rev_before != it.expected_revision:
                                fail = (
                                    "conflict", ErrorCode.CONFLICT_REVISION,
                                    f"stale write: expected revision "
                                    f"{it.expected_revision}, current is "
                                    f"{rev_before}")

                if fail is not None:
                    status, code, msg = fail
                    results.append(BatchItemResult(
                        entity_id=it.entity_id, status=status,
                        reason_code=code.value, revision=None, detail=msg))
                    self._audit(
                        actor=it.actor, action=action, target=it.entity_id,
                        status=("validation_failed" if status
                                == "validation_failed" else "rejected"),
                        reason_code=code, detail={"message": msg},
                        revision_before=rev_before,
                    )
                else:
                    assert entity is not None and rev_before is not None
                    pending.append((entity, it))
                    results.append(BatchItemResult(
                        entity_id=it.entity_id, status="success",
                        reason_code=None, revision=rev_before + 1,
                        detail="applied"))

            applied = len(pending) == len(items)
            if applied:
                # 整批一致通过：在同一把锁内逐条应用并落库
                for entity, it in pending:
                    self._apply_owner_tags(
                        entity, owner=it.owner, set_tags=it.set_tags,
                        add_tags=set(it.add_tags),
                        remove_tags=set(it.remove_tags))
                    entity.revision += 1
                    self.store.put_entity(entity)
                    self._audit(
                        actor=it.actor, action=action, target=it.entity_id,
                        status="success",
                        detail={"owner": entity.owner,
                                "tags": sorted(entity.tags)},
                        revision_before=it.expected_revision,
                        revision_after=entity.revision,
                    )
            else:
                # 整批不落库：自身合法的条目标记为 aborted，同样逐条留痕
                aborted_ids = {e.entity_id for e, _it in pending}
                results = [
                    BatchItemResult(
                        entity_id=r.entity_id,
                        status=("aborted" if r.entity_id in aborted_ids
                                else r.status),
                        reason_code=(ErrorCode.BATCH_ABORTED.value
                                     if r.entity_id in aborted_ids
                                     else r.reason_code),
                        revision=None,
                        detail=("batch aborted: no change persisted"
                                if r.entity_id in aborted_ids else r.detail),
                    )
                    for r in results
                ]
                for entity, it in pending:
                    self._audit(
                        actor=it.actor, action=action, target=it.entity_id,
                        status="rejected", reason_code=ErrorCode.BATCH_ABORTED,
                        detail={"message": "batch aborted: no change persisted"},
                        revision_before=it.expected_revision,
                    )

            return BatchResult(applied=applied, results=results)

    # ---- 审计 -----------------------------------------------------
    def audit_log(self) -> list[AuditEntry]:
        return self.store.audit_log()
