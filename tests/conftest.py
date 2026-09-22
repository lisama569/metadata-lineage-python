"""pytest 公共夹具与“输入/判定依据”日志辅助。"""

from __future__ import annotations

import logging

import pytest

from metadata_lineage.service import CatalogService
from metadata_lineage.storage import CatalogStore


def log_case(request, evidence: dict) -> None:
    """在测试日志中打印输入与判定依据。"""
    logging.getLogger("cases").warning(
        "CASE %s | INPUT/EVIDENCE: %s", request.node.name, evidence
    )


@pytest.fixture
def service() -> CatalogService:
    return CatalogService(CatalogStore())
