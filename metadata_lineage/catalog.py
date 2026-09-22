"""实体目录：登记、结构演进（兼容性判定）、显式退役/改名、属主与标签并发更新。"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from typing import Sequence

from .errors import MetadataError, RejectReason
from .models import (
    AuditRecord,
    Entity,
    EntityVersion,
    FieldDef,
    FieldType,
    UpdateOutcome,
    UpdateResult,
    WIDENING,
    utc_now,
)


def _norm_fields(fields: Iterable[FieldDef | tuple[str, FieldType | str]]) -> tuple[FieldDef, ...]:
    out: list[FieldDef] = []
    for f in fields:
        if isinstance(f, FieldDef):
            out.append(f)
        else:
            name, ftype = f
            out.append(FieldDef(name=name, type=FieldType(ftype)))
    return tuple(out)


class Catalog:
    """线程安全的内存目录。所有写操作在锁内完成并追加审计。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entities: dict[str, Entity] = {}
        self._versions: dict[str, list[EntityVersion]] = {}
        # 已显式退役的字段：entity_id -> {field_name: 在哪个版本退役}
        self._retired: dict[str, dict[str, int]] = {}
        # 改名别名：entity_id -> {旧名: 现名}
        self._renames: dict[str, dict[str, str]] = {}
        self._audit: list[AuditRecord] = []
        self._seq = 0

        # 字段退役/改名时需要联动血缘；由 wiring 层注入
        self._retire_hook = None
        self._rename_hook = None

    def set_lineage_hooks(self, on_retire, on_rename) -> None:
        self._retire_hook = on_retire
        self._rename_hook = on_rename

    # ------------------------------------------------------------------ 审计
    def _append_audit(
        self,
        action: str,
        actor: str,
        entity_id: str | None,
        detail: dict,
        success: bool,
        reason: str | None,
    ) -> AuditRecord:
        self._seq += 1
        rec = AuditRecord(
            seq=self._seq,
            timestamp=utc_now(),
            action=action,
            actor=actor,
            entity_id=entity_id,
            detail=dict(detail),
            success=success,
            reason=reason,
        )
        self._audit.append(rec)
        return rec

    def audit_log(self) -> list[AuditRecord]:
        """审计记录按序号返回（只增不改）。"""
        with self._lock:
            return list(self._audit)

    # ------------------------------------------------------------ 登记/查询
    def register_entity(
        self,
        entity_id: str,
        fields: Sequence[FieldDef | tuple[str, FieldType | str]],
        owner: str,
        actor: str = "system",
    ) -> EntityVersion:
        norm = _norm_fields(fields)
        with self._lock:
            if entity_id in self._entities:
                raise self._reject(
                    "register_entity", actor, entity_id,
                    {"entity_id": entity_id},
                    RejectReason.VALIDATION_FAILED,
                    f"entity already registered: {entity_id}",
                )
            self._validate_field_names(norm, actor, entity_id)
            if not isinstance(owner, str) or not owner.strip():
                raise self._reject(
                    "register_entity", actor, entity_id,
                    {"owner": owner},
                    RejectReason.VALIDATION_FAILED, "owner must be non-empty",
                )
            ver = EntityVersion(
                entity_id=entity_id,
                version=1,
                fields=norm,
                created_at=utc_now(),
                note="initial registration",
            )
            self._versions[entity_id] = [ver]
            self._entities[entity_id] = Entity(
                entity_id=entity_id,
                current_version=1,
                owner=owner,
                tags=frozenset(),
                created_at=ver.created_at,
            )
            self._append_audit(
                "register_entity", actor, entity_id,
                {"version": 1, "fields": [f.name for f in norm], "owner": owner},
                True, None,
            )
            return ver

    def _reject(self, action, actor, entity_id, detail, reason, message) -> MetadataError:
        """构造异常前先留痕，保证失败变更也有审计。"""
        self._append_audit(action, actor, entity_id, detail, False, reason.name)
        return MetadataError(reason, message)

    @staticmethod
    def _validate_field_names(fields: tuple[FieldDef, ...], actor, entity_id) -> None:
        seen: set[str] = set()
        for f in fields:
            if not f.name or not f.name.strip():
                raise MetadataError(RejectReason.VALIDATION_FAILED, "field name must be non-empty")
            if f.name in seen:
                raise MetadataError(
                    RejectReason.VALIDATION_FAILED, f"duplicate field name: {f.name}"
                )
            seen.add(f.name)

    def get_entity(self, entity_id: str) -> Entity:
        with self._lock:
            if entity_id not in self._entities:
                raise MetadataError(RejectReason.ENTITY_NOT_FOUND, f"entity not found: {entity_id}")
            return self._entities[entity_id]

    def get_version(self, entity_id: str, version: int | None = None) -> EntityVersion:
        with self._lock:
            versions = self._versions.get(entity_id)
            if versions is None:
                raise MetadataError(RejectReason.ENTITY_NOT_FOUND, f"entity not found: {entity_id}")
            if version is None:
                return versions[-1]
            for v in versions:
                if v.version == version:
                    return v
            raise MetadataError(
                RejectReason.VERSION_NOT_FOUND,
                f"version {version} not found for entity {entity_id}",
            )

    def list_versions(self, entity_id: str) -> list[EntityVersion]:
        with self._lock:
            if entity_id not in self._entities:
                raise MetadataError(RejectReason.ENTITY_NOT_FOUND, f"entity not found: {entity_id}")
            return list(self._versions[entity_id])

    # ------------------------------------------------------- 结构兼容与演进
    @staticmethod
    def check_compatibility(old: EntityVersion, new: EntityVersion) -> None:
        """
        纯函数式兼容性判定。规则：
          * 同名字段保留、类型不变：兼容
          * 类型仅沿 INT->BIGINT->DOUBLE 拓宽：兼容（可追溯版本记录）
          * 类型收窄：拒绝 TYPE_NARROWED
          * 其它不同类型：拒绝 TYPE_INCOMPATIBLE
          * 仅删除字段：拒绝 FIELD_REMOVED
          * 同数量的“一减一增且新字段类型与被删字段相同”：判定为隐式改名，
            拒绝 FIELD_RENAMED（显式改名必须走 rename_field）
          * 同时存在新增字段与删除字段（删除数 != 新增数，或新增字段无法
            与被删字段按同类型一一配对）：拒绝 FIELD_REMOVED
        """
        old_map = old.field_map()
        new_map = new.field_map()
        removed = [n for n in old_map if n not in new_map]
        added = [n for n in new_map if n not in old_map]

        # 1) 共有字段的类型检查
        for name in old_map:
            if name in new_map:
                ot, nt = old_map[name].type, new_map[name].type
                if ot is nt:
                    continue
                if nt in WIDENING[ot]:
                    continue
                if ot in WIDENING[nt]:
                    raise MetadataError(
                        RejectReason.TYPE_NARROWED,
                        f"field '{name}' type narrowed: {ot.name} -> {nt.name}",
                    )
                raise MetadataError(
                    RejectReason.TYPE_INCOMPATIBLE,
                    f"field '{name}' type incompatible: {ot.name} -> {nt.name}",
                )

        if not removed:
            return  # 只有新增（或无变化），兼容

        # 2) 有字段消失：判定是隐式改名还是删除
        unmatched_removed, unmatched_added = _pair_renames(removed, added, old_map, new_map)
        if not unmatched_removed and removed and not unmatched_added and len(removed) == len(added):
            pairs = list(zip(removed, added))
            raise MetadataError(
                RejectReason.FIELD_RENAMED,
                "possible implicit rename(s) "
                + ", ".join(f"{o}->{n}" for o, n in pairs)
                + "; use rename_field() for an explicit rename",
            )
        raise MetadataError(
            RejectReason.FIELD_REMOVED,
            "removed field(s): " + ", ".join(sorted(removed))
            + "; field removal requires retire_field()",
        )

    def evolve_schema(
        self,
        entity_id: str,
        fields: Sequence[FieldDef | tuple[str, FieldType | str]],
        actor: str = "system",
        note: str = "",
    ) -> EntityVersion:
        """提交新结构；不兼容变更抛 MetadataError，旧版本保持不变且全程可追溯。"""
        norm = _norm_fields(fields)
        with self._lock:
            if entity_id not in self._entities:
                raise self._reject(
                    "evolve_schema", actor, entity_id, {},
                    RejectReason.ENTITY_NOT_FOUND, f"entity not found: {entity_id}",
                )
            self._validate_field_names(norm, actor, entity_id)
            old = self._versions[entity_id][-1]
            new = EntityVersion(
                entity_id=entity_id,
                version=old.version + 1,
                fields=norm,
                created_at=utc_now(),
                note=note,
            )
            try:
                self.check_compatibility(old, new)
            except MetadataError as e:
                # check_compatibility 是静态判定，审计在这里统一留痕
                self._append_audit(
                    "evolve_schema", actor, entity_id,
                    {
                        "from_version": old.version,
                        "to_fields": [f.name for f in norm],
                        "rejected_fields": [f.name for f in norm],
                    },
                    False, e.reason.name,
                )
                raise
            self._versions[entity_id].append(new)
            ent = self._entities[entity_id]
            self._entities[entity_id] = dataclasses_replace(
                ent, current_version=new.version, revision=ent.revision + 1
            )
            self._append_audit(
                "evolve_schema", actor, entity_id,
                {"from_version": old.version, "to_version": new.version,
                 "fields": [f.name for f in norm], "note": note},
                True, None,
            )
            return new

    def _latest_version(self, entity_id: str) -> EntityVersion:
        return self._versions[entity_id][-1]

    def retire_field(self, entity_id: str, field_name: str, actor: str = "system") -> EntityVersion:
        """
        显式退役字段——删除字段的唯一合法路径。

        语义：产生一个新版本，该字段从结构中移除并记录在退役清单中；
        血缘中引用该字段的边被置为 BROKEN 并保留（不静默删除、不错连）。
        """
        with self._lock:
            if entity_id not in self._entities:
                raise self._reject(
                    "retire_field", actor, entity_id, {"field": field_name},
                    RejectReason.ENTITY_NOT_FOUND, f"entity not found: {entity_id}",
                )
            old = self._latest_version(entity_id)
            old_map = old.field_map()
            if field_name not in old_map:
                if field_name in self._retired.get(entity_id, {}):
                    raise self._reject(
                        "retire_field", actor, entity_id, {"field": field_name},
                        RejectReason.ENDPOINT_RETIRED,
                        f"field already retired: {entity_id}.{field_name}",
                    )
                raise self._reject(
                    "retire_field", actor, entity_id, {"field": field_name},
                    RejectReason.ENDPOINT_NOT_FOUND,
                    f"field not found: {entity_id}.{field_name}",
                )
            new_fields = tuple(f for f in old.fields if f.name != field_name)
            new = EntityVersion(
                entity_id=entity_id,
                version=old.version + 1,
                fields=new_fields,
                created_at=utc_now(),
                note=f"retire field {field_name}",
            )
            self._versions[entity_id].append(new)
            self._retired.setdefault(entity_id, {})[field_name] = new.version
            self._entities[entity_id] = dataclasses_replace(
                self._entities[entity_id], current_version=new.version,
                revision=self._entities[entity_id].revision + 1,
            )
            broken: list = []
            if self._retire_hook is not None:
                broken = self._retire_hook(entity_id, field_name)
            self._append_audit(
                "retire_field", actor, entity_id,
                {"from_version": old.version, "to_version": new.version,
                 "field": field_name,
                 "broken_edges": [_edge_key(e) for e in broken]},
                True, None,
            )
            return new

    def rename_field(
        self, entity_id: str, old_name: str, new_name: str, actor: str = "system"
    ) -> EntityVersion:
        """
        显式改名——字段改名的唯一合法路径。

        语义：产生一个新版本，字段名替换、类型保持不变；血缘边端点被原子
        改写（不错连、不丢失），并记录旧名->新名别名。
        """
        with self._lock:
            if entity_id not in self._entities:
                raise self._reject(
                    "rename_field", actor, entity_id,
                    {"from": old_name, "to": new_name},
                    RejectReason.ENTITY_NOT_FOUND, f"entity not found: {entity_id}",
                )
            if not new_name or not new_name.strip():
                raise self._reject(
                    "rename_field", actor, entity_id,
                    {"from": old_name, "to": new_name},
                    RejectReason.VALIDATION_FAILED, "new field name must be non-empty",
                )
            old = self._latest_version(entity_id)
            old_map = old.field_map()
            if old_name not in old_map:
                raise self._reject(
                    "rename_field", actor, entity_id,
                    {"from": old_name, "to": new_name},
                    RejectReason.ENDPOINT_NOT_FOUND,
                    f"field not found: {entity_id}.{old_name}",
                )
            if new_name in old_map:
                raise self._reject(
                    "rename_field", actor, entity_id,
                    {"from": old_name, "to": new_name},
                    RejectReason.VALIDATION_FAILED,
                    f"target field name already exists: {entity_id}.{new_name}",
                )
            new_fields = tuple(
                f if f.name != old_name else FieldDef(new_name, f.type)
                for f in old.fields
            )
            new = EntityVersion(
                entity_id=entity_id,
                version=old.version + 1,
                fields=new_fields,
                created_at=utc_now(),
                note=f"rename field {old_name} -> {new_name}",
            )
            self._versions[entity_id].append(new)
            self._renames.setdefault(entity_id, {})[old_name] = new_name
            self._entities[entity_id] = dataclasses_replace(
                self._entities[entity_id], current_version=new.version,
                revision=self._entities[entity_id].revision + 1,
            )
            moved: list = []
            if self._rename_hook is not None:
                moved = self._rename_hook(entity_id, old_name, new_name)
            self._append_audit(
                "rename_field", actor, entity_id,
                {"from_version": old.version, "to_version": new.version,
                 "from_field": old_name, "to_field": new_name,
                 "moved_edges": [_edge_key(e) for e in moved]},
                True, None,
            )
            return new

    def retired_fields(self, entity_id: str) -> dict[str, int]:
        """已退役字段 -> 退役版本。"""
        with self._lock:
            if entity_id not in self._entities:
                raise MetadataError(RejectReason.ENTITY_NOT_FOUND, f"entity not found: {entity_id}")
            return dict(self._retired.get(entity_id, {}))

    def current_field_name(self, entity_id: str, field_name: str) -> str:
        """沿改名别名链解析字段当前名。"""
        seen: set[str] = set()
        aliases = self._renames.get(entity_id, {})
        while field_name in aliases and field_name not in seen:
            seen.add(field_name)
            field_name = aliases[field_name]
        return field_name

    # --------------------------------------------------- 属主/标签并发更新
    def update_owner(
        self,
        entity_id: str,
        owner: str,
        expected_version: int,
        actor: str = "system",
    ) -> UpdateResult:
        """
        乐观锁更新属主。

        * expected_version 必须等于当前结构版本，否则返回 REJECTED_STALE
          （过期写入，不抛异常，与成功/校验失败可区分）；
        * owner 为空字符串等校验问题返回 REJECTED_INVALID；
        * 成功返回 APPLIED。三种结论均追加审计记录。
        """
        with self._lock:
            found = self._entities.get(entity_id)
            if found is None:
                rec = self._append_audit(
                    "update_owner", actor, entity_id,
                    {"owner": owner, "expected_version": expected_version},
                    False, RejectReason.ENTITY_NOT_FOUND.name,
                )
                raise MetadataError(RejectReason.ENTITY_NOT_FOUND, f"entity not found: {entity_id}")
            actual = found.revision
            if expected_version != actual:
                rec = self._append_audit(
                    "update_owner", actor, entity_id,
                    {"owner": owner, "expected_version": expected_version,
                     "actual_version": actual},
                    False, RejectReason.STALE_VERSION.name,
                )
                return UpdateResult(
                    outcome=UpdateOutcome.REJECTED_STALE,
                    expected_version=expected_version,
                    actual_version=actual,
                    reason=RejectReason.STALE_VERSION.name,
                    audit_seq=rec.seq,
                )
            if not isinstance(owner, str) or not owner.strip():
                rec = self._append_audit(
                    "update_owner", actor, entity_id,
                    {"owner": owner, "expected_version": expected_version},
                    False, RejectReason.VALIDATION_FAILED.name,
                )
                return UpdateResult(
                    outcome=UpdateOutcome.REJECTED_INVALID,
                    expected_version=expected_version,
                    actual_version=actual,
                    reason=RejectReason.VALIDATION_FAILED.name,
                    audit_seq=rec.seq,
                )
            updated = dataclasses_replace(found, owner=owner, revision=found.revision + 1)
            self._entities[entity_id] = updated
            rec = self._append_audit(
                "update_owner", actor, entity_id,
                {"owner": owner, "expected_version": expected_version},
                True, None,
            )
            return UpdateResult(
                outcome=UpdateOutcome.APPLIED,
                expected_version=expected_version,
                actual_version=actual,
                entity=updated,
                audit_seq=rec.seq,
            )

    def update_tags(
        self,
        entity_id: str,
        add: Iterable[str] = (),
        remove: Iterable[str] = (),
        expected_version: int | None = None,
        actor: str = "system",
    ) -> UpdateResult:
        """
        乐观锁更新标签（add/remove 在同一版本上原子应用）。

        expected_version=None 表示无条件写入（不参与过期判定）；
        传入版本号时过期返回 REJECTED_STALE。标签不得为空字符串，
        add 与 remove 交集非空返回 REJECTED_INVALID。
        """
        add_set = {t for t in add}
        remove_set = {t for t in remove}
        with self._lock:
            found = self._entities.get(entity_id)
            if found is None:
                self._append_audit(
                    "update_tags", actor, entity_id,
                    {"add": sorted(add_set), "remove": sorted(remove_set),
                     "expected_version": expected_version},
                    False, RejectReason.ENTITY_NOT_FOUND.name,
                )
                raise MetadataError(RejectReason.ENTITY_NOT_FOUND, f"entity not found: {entity_id}")
            actual = found.revision
            invalid_tags = [t for t in add_set | remove_set if not isinstance(t, str) or not t.strip()]
            conflict = add_set & remove_set
            if invalid_tags or conflict:
                rec = self._append_audit(
                    "update_tags", actor, entity_id,
                    {"add": sorted(add_set), "remove": sorted(remove_set),
                     "expected_version": expected_version,
                     "invalid_tags": invalid_tags, "conflict_tags": sorted(conflict)},
                    False, RejectReason.VALIDATION_FAILED.name,
                )
                return UpdateResult(
                    outcome=UpdateOutcome.REJECTED_INVALID,
                    expected_version=expected_version if expected_version is not None else actual,
                    actual_version=actual,
                    reason=RejectReason.VALIDATION_FAILED.name,
                    audit_seq=rec.seq,
                )
            if expected_version is not None and expected_version != actual:
                rec = self._append_audit(
                    "update_tags", actor, entity_id,
                    {"add": sorted(add_set), "remove": sorted(remove_set),
                     "expected_version": expected_version, "actual_version": actual},
                    False, RejectReason.STALE_VERSION.name,
                )
                return UpdateResult(
                    outcome=UpdateOutcome.REJECTED_STALE,
                    expected_version=expected_version,
                    actual_version=actual,
                    reason=RejectReason.STALE_VERSION.name,
                    audit_seq=rec.seq,
                )
            new_tags = (found.tags | add_set) - remove_set
            updated = dataclasses_replace(found, tags=frozenset(new_tags), revision=found.revision + 1)
            self._entities[entity_id] = updated
            rec = self._append_audit(
                "update_tags", actor, entity_id,
                {"add": sorted(add_set), "remove": sorted(remove_set),
                 "expected_version": expected_version,
                 "tags": sorted(updated.tags)},
                True, None,
            )
            return UpdateResult(
                outcome=UpdateOutcome.APPLIED,
                expected_version=expected_version if expected_version is not None else actual,
                actual_version=actual,
                entity=updated,
                audit_seq=rec.seq,
            )


def _pair_renames(removed, added, old_map, new_map) -> tuple[list[str], list[str]]:
    """
    按“同类型”把消失字段与新增字段一一配对（启发式隐式改名识别）。
    返回 (未配对的删除, 未配对的新增)。
    """
    added_left = list(added)
    unmatched_removed: list[str] = []
    for r in removed:
        match_idx = next(
            (i for i, a in enumerate(added_left) if new_map[a].type is old_map[r].type),
            None,
        )
        if match_idx is None:
            unmatched_removed.append(r)
        else:
            added_left.pop(match_idx)
    return unmatched_removed, added_left


def _edge_key(edge) -> dict[str, str]:
    return {"source": f"{edge.source[0]}.{edge.source[1]}",
            "target": f"{edge.target[0]}.{edge.target[1]}",
            "status": edge.status.name}


def dataclasses_replace(ent: Entity, **kw) -> Entity:
    import dataclasses as _dc

    return _dc.replace(ent, **kw)
