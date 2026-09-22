"""装配层：把 Catalog（结构事实来源）与 LineageGraph 绑定为一致服务。"""

from __future__ import annotations

from .catalog import Catalog
from .errors import MetadataError, RejectReason
from .lineage import LineageGraph


class MetadataService:
    """对外门面。Catalog 与 LineageGraph 的所有联动在此接线。"""

    def __init__(self) -> None:
        self.catalog = Catalog()
        self.lineage = LineageGraph()
        self.catalog.set_lineage_hooks(
            on_retire=self.lineage.on_field_retired,
            on_rename=self.lineage.on_field_renamed,
        )
        self.lineage.set_endpoint_resolver(self._resolve_endpoint)

    def _resolve_endpoint(self, ep: tuple[str, str]) -> None:
        """血缘登记时的端点校验：存在且未退役（改名旧名显式拒绝，不静默跟随）。"""
        entity_id, field_name = ep
        retired = self.catalog.retired_fields(entity_id)  # 实体不存在抛 ENTITY_NOT_FOUND
        if field_name in retired:
            raise MetadataError(
                RejectReason.ENDPOINT_RETIRED,
                f"field has been retired: {entity_id}.{field_name}",
            )
        latest = self.catalog.get_version(entity_id)
        if field_name not in latest.field_map():
            current = self.catalog.current_field_name(entity_id, field_name)
            if current != field_name:
                raise MetadataError(
                    RejectReason.ENDPOINT_NOT_FOUND,
                    f"field renamed: use {entity_id}.{current} instead of "
                    f"{entity_id}.{field_name}",
                )
            raise MetadataError(
                RejectReason.ENDPOINT_NOT_FOUND,
                f"field not found: {entity_id}.{field_name}",
            )
