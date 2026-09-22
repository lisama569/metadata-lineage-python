"""核心领域模型（全部不可变，便于并发与版本追溯）。"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import enum
from typing import Mapping


class FieldType(enum.Enum):
    """支持的字段标量类型，按数值宽度构成偏序（仅数字链允许隐式拓宽）。"""

    INT = "INT"
    BIGINT = "BIGINT"
    DOUBLE = "DOUBLE"
    STRING = "STRING"
    BOOL = "BOOL"
    DATE = "DATE"
    BINARY = "BINARY"


# 允许的类型拓宽方向：窄 -> 宽。其余任意两个不同类型之间一律视为不兼容。
WIDENING: dict[FieldType, frozenset[FieldType]] = {
    FieldType.INT: frozenset({FieldType.BIGINT, FieldType.DOUBLE}),
    FieldType.BIGINT: frozenset({FieldType.DOUBLE}),
    FieldType.DOUBLE: frozenset(),
    FieldType.STRING: frozenset(),
    FieldType.BOOL: frozenset(),
    FieldType.DATE: frozenset(),
    FieldType.BINARY: frozenset(),
}


@dataclasses.dataclass(frozen=True, slots=True)
class FieldDef:
    """一个版本内的字段定义。"""

    name: str
    type: FieldType


@dataclasses.dataclass(frozen=True, slots=True)
class EntityVersion:
    """实体的一次结构快照，版本号单调递增，永不覆盖。"""

    entity_id: str
    version: int
    fields: tuple[FieldDef, ...]
    created_at: str
    note: str = ""

    def field_map(self) -> Mapping[str, FieldDef]:
        return {f.name: f for f in self.fields}


@dataclasses.dataclass(frozen=True, slots=True)
class Entity:
    """实体（逻辑表/数据集）的元数据。"""

    entity_id: str
    current_version: int
    owner: str
    tags: frozenset[str]
    created_at: str
    # 元数据修订号：结构演进、退役/改名、属主/标签的每次成功变更都 +1，
    # 属主/标签的乐观锁以它为准，保证并发写入必有一方过期。
    revision: int = 1


class EdgeStatus(enum.Enum):
    """血缘边状态。字段显式退役时边置为 BROKEN 并保留，绝不静默删除。"""

    ACTIVE = "ACTIVE"
    BROKEN = "BROKEN"


@dataclasses.dataclass(frozen=True, slots=True)
class LineageEdge:
    """
    一条字段级血缘边：src -> dst 表示 src 字段产出 dst 字段。

    source/target 均为 (entity_id, field_name)。字段退役时 status 变 BROKEN，
    边本身保留以保证血缘语义明确稳定。
    """

    source: tuple[str, str]
    target: tuple[str, str]
    status: EdgeStatus = EdgeStatus.ACTIVE
    registered_at: str = ""

    def key(self) -> tuple[tuple[str, str], tuple[str, str]]:
        return (self.source, self.target)


@dataclasses.dataclass(frozen=True, slots=True)
class AuditRecord:
    """审计记录：任何成功或失败的变更都必须留痕。"""

    seq: int
    timestamp: str
    action: str
    actor: str
    entity_id: str | None
    detail: Mapping[str, object]
    success: bool
    reason: str | None  # RejectReason.name；成功时为 None


class UpdateOutcome(enum.Enum):
    """属主/标签更新的三种可区分结论。"""

    APPLIED = "APPLIED"          # 成功应用
    REJECTED_STALE = "REJECTED_STALE"   # 过期写入
    REJECTED_INVALID = "REJECTED_INVALID"  # 校验失败


@dataclasses.dataclass(frozen=True, slots=True)
class UpdateResult:
    """属主/标签更新的返回值。"""

    outcome: UpdateOutcome
    expected_version: int
    actual_version: int
    entity: Entity | None = None
    reason: str | None = None
    audit_seq: int | None = None


def utc_now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="microseconds")
