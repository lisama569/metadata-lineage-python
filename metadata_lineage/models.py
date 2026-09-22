"""领域模型：实体、字段版本、血缘边、审计记录与错误类型。

骨架阶段仅定义类型；具体判定逻辑在后续步骤中逐个填充。
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow() -> datetime:
    """返回带时区的当前 UTC 时间。"""
    return datetime.now(timezone.utc)


def new_id() -> str:
    """生成一个稳定唯一标识（字段 UID / 边 ID 等）。"""
    return uuid.uuid4().hex


class ErrorCode(str, enum.Enum):
    """可区分的拒绝原因。"""

    ENTITY_NOT_FOUND = "ENTITY_NOT_FOUND"
    VERSION_NOT_FOUND = "VERSION_NOT_FOUND"
    FIELD_NOT_FOUND = "FIELD_NOT_FOUND"
    ENTITY_ALREADY_EXISTS = "ENTITY_ALREADY_EXISTS"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    BREAKING_FIELD_REMOVED = "BREAKING_FIELD_REMOVED"
    BREAKING_FIELD_RENAMED = "BREAKING_FIELD_RENAMED"
    BREAKING_TYPE_NARROWING = "BREAKING_TYPE_NARROWING"
    BREAKING_INCOMPATIBLE_TYPE = "BREAKING_INCOMPATIBLE_TYPE"
    BREAKING_NULLABILITY_NARROWED = "BREAKING_NULLABILITY_NARROWED"
    LINEAGE_DUPLICATE = "LINEAGE_DUPLICATE"
    LINEAGE_CYCLE = "LINEAGE_CYCLE"
    LINEAGE_ENDPOINT_NOT_FOUND = "LINEAGE_ENDPOINT_NOT_FOUND"
    LINEAGE_FIELD_NOT_FOUND = "LINEAGE_FIELD_NOT_FOUND"
    CONFLICT_REVISION = "CONFLICT_REVISION"
    VALIDATION_ERROR = "VALIDATION_ERROR"


class FieldType(str, enum.Enum):
    """支持的字段标量类型。"""

    BOOL = "BOOL"
    INT32 = "INT32"
    INT64 = "INT64"
    FLOAT64 = "FLOAT64"
    STRING = "STRING"
    BYTES = "BYTES"
    TIMESTAMP = "TIMESTAMP"
    DATE = "DATE"
    DECIMAL = "DECIMAL"


@dataclass(frozen=True)
class FieldSpec:
    """登记新版本时提交的字段描述。"""

    name: str
    type: FieldType
    nullable: bool = True
    uid: str | None = None  # 改名登记时用于把新旧名字锚定到同一字段


@dataclass
class FieldVersion:
    """字段在某个实体版本下的快照。"""

    uid: str
    name: str
    type: FieldType
    nullable: bool


@dataclass
class EntityVersion:
    """实体结构的一个不可变版本。"""

    entity_id: str
    version: int
    fields: list[FieldVersion]
    created_at: datetime
    created_by: str
    note: str = ""

    def field_by_name(self, name: str) -> FieldVersion | None:
        for f in self.fields:
            if f.name == name:
                return f
        return None

    def field_by_uid(self, uid: str) -> FieldVersion | None:
        for f in self.fields:
            if f.uid == uid:
                return f
        return None


@dataclass
class Entity:
    """数据实体聚合：身份信息 + 不可变结构版本链 + 属主标签元数据。"""

    entity_id: str
    name: str
    versions: list[EntityVersion] = field(default_factory=list)
    owner: str | None = None
    tags: set[str] = field(default_factory=set)
    revision: int = 0  # 属主/标签的乐观锁版本
    created_at: datetime = field(default_factory=utcnow)

    @property
    def latest_version(self) -> EntityVersion | None:
        return self.versions[-1] if self.versions else None


@dataclass(frozen=True)
class Ref:
    """血缘端点：实体 + 字段（精确到字段级）。"""

    entity_id: str
    field_uid: str


@dataclass
class LineageEdge:
    """有向血缘边：src.field -> dst.field。"""

    edge_id: str
    src: Ref
    dst: Ref
    created_at: datetime
    created_by: str
    active: bool = True  # 结构演进导致端点字段悬空时置为 False，不删除


@dataclass(frozen=True)
class BreakingChange:
    """一次不兼容变更的明细，用于可区分的拒绝原因。"""

    code: ErrorCode
    field_name: str | None
    detail: str


@dataclass(frozen=True)
class CompatibilityReport:
    """新版本相对旧版本的兼容性判定结论。"""

    compatible: bool
    breaking_changes: list[BreakingChange]
    # uid 分配结果：字段名 -> uid（新字段为新 uid，改名沿用旧 uid）
    uid_assignments: dict[str, str]


@dataclass(frozen=True)
class AuditEntry:
    """不可缺失的审计记录（成功与被拒绝的变更都记录）。"""

    audit_id: str
    at: datetime
    actor: str
    action: str
    target: str
    status: str  # "success" | "rejected" | "validation_failed"
    reason_code: str | None
    detail: dict[str, Any]
    revision_before: int | None
    revision_after: int | None


class ServiceError(Exception):
    """服务层拒绝异常，携带可区分错误码与明细。"""

    def __init__(self, code: ErrorCode, detail: str | None = None,
                 extra: dict[str, Any] | None = None) -> None:
        super().__init__(detail or code.value)
        self.code = code
        self.detail = detail
        self.extra = extra or {}
