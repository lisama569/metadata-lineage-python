"""可区分的拒绝原因与统一异常类型。"""

from __future__ import annotations

import enum


class RejectReason(enum.Enum):
    """所有可区分的拒绝/失败原因。"""

    # 结构演进相关
    FIELD_REMOVED = "FIELD_REMOVED"                # 隐式删除字段被拒绝
    FIELD_RENAMED = "FIELD_RENAMED"                # 隐式改名被拒绝（同类型一减一增）
    TYPE_NARROWED = "TYPE_NARROWED"                # 类型收窄被拒绝
    TYPE_INCOMPATIBLE = "TYPE_INCOMPATIBLE"        # 类型无关变更被拒绝

    # 血缘相关
    DUPLICATE_EDGE = "DUPLICATE_EDGE"              # 重复登记血缘
    CYCLE_DETECTED = "CYCLE_DETECTED"              # 登记会成环
    ENDPOINT_NOT_FOUND = "ENDPOINT_NOT_FOUND"      # 血缘端点（实体/字段）不存在
    ENDPOINT_RETIRED = "ENDPOINT_RETIRED"          # 血缘端点字段已退役

    # 并发与校验相关
    STALE_VERSION = "STALE_VERSION"                # 过期写入（乐观锁冲突）
    VALIDATION_FAILED = "VALIDATION_FAILED"        # 一般校验失败
    ENTITY_NOT_FOUND = "ENTITY_NOT_FOUND"          # 实体不存在
    VERSION_NOT_FOUND = "VERSION_NOT_FOUND"        # 版本不存在


class MetadataError(Exception):
    """携带可区分原因的业务异常。"""

    def __init__(self, reason: RejectReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message

    def __repr__(self) -> str:  # pragma: no cover
        return f"MetadataError({self.reason.name}: {self.message})"
