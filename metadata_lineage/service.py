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

    active_edges：当前有效结构上参与传播的边。
    dangling_edges：因字段被删除（强制演进）而断开的边；不参与遍历，永不静默删除。
    """

    upstream: list[Ref]
    downstream: list[Ref]
    active_edges: list[LineageEdge]
    dangling_edges: list[LineageEdge]


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

    def impact(self, ref: Ref) -> ImpactResult:
        try:
            self._validate_ref_current(ref)
        except ServiceError:
            raise
        active = [e for e in self.store.edges() if e.active]
        dangling = [
            e for e in self.store.edges()
            if not e.active and (e.src == ref or e.dst == ref)
        ]

        def walk(forward: bool) -> tuple[list[Ref], list[LineageEdge]]:
            adj: dict[Ref, list[tuple[LineageEdge, Ref]]] = {}
            for e in sorted(active, key=lambda x: x.edge_id):
                if forward:
                    adj.setdefault(e.src, []).append((e, e.dst))
                else:
                    adj.setdefault(e.dst, []).append((e, e.src))
            order: list[Ref] = []
            used_edges: list[LineageEdge] = []
            seen = {ref}
            queue: deque[Ref] = deque([ref])
            while queue:
                cur = queue.popleft()
                for edge, nxt in adj.get(cur, []):
                    if edge not in used_edges:
                        used_edges.append(edge)
                    if nxt not in seen:
                        seen.add(nxt)
                        order.append(nxt)
                        queue.append(nxt)
            return order, used_edges

        upstream, up_edges = walk(forward=False)
        downstream, down_edges = walk(forward=True)
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
        )

    # ---- 属主与标签（乐观锁） -------------------------------------
    def update_owner_tags(
        self, entity_id: str, *, expected_revision: int,
        owner: str | object = _UNSET, add_tags: set[str] | None = None,
        remove_tags: set[str] | None = None, actor: str = "anonymous",
    ) -> tuple[Entity, int]:
        action = "update_owner_tags"
        add_tags = set(add_tags or ())
        remove_tags = set(remove_tags or ())
        if add_tags & remove_tags:
            self._audit(
                actor=actor, action=action, target=entity_id,
                status="validation_failed", reason_code=ErrorCode.VALIDATION_ERROR,
                detail={"message": "same tag appears in both add and remove",
                        "tags": sorted(add_tags & remove_tags)},
            )
            raise ServiceError(
                ErrorCode.VALIDATION_ERROR,
                "same tag appears in both add_tags and remove_tags")
        bad = [t for t in (add_tags | remove_tags) if not t or not t.strip()]
        if bad:
            self._audit(
                actor=actor, action=action, target=entity_id,
                status="validation_failed", reason_code=ErrorCode.VALIDATION_ERROR,
                detail={"message": "tag must be non-empty"},
            )
            raise ServiceError(
                ErrorCode.VALIDATION_ERROR, "tag must be non-empty")

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

            if owner is not _UNSET:
                entity.owner = None if owner is None else str(owner)
            entity.tags = (entity.tags | add_tags) - remove_tags
            entity.revision = rev_before + 1
            self.store.put_entity(entity)
            self._audit(
                actor=actor, action=action, target=entity_id, status="success",
                detail={"owner": entity.owner, "tags": sorted(entity.tags)},
                revision_before=rev_before, revision_after=entity.revision,
            )
            return entity, entity.revision

    # ---- 审计 -----------------------------------------------------
    def audit_log(self) -> list[AuditEntry]:
        return self.store.audit_log()
