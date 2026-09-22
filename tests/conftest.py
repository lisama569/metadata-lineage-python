"""测试夹具与“输入 + 判定依据”日志辅助。"""

from __future__ import annotations

import logging
import pprint

import pytest

from metadata_lineage.errors import MetadataError
from metadata_lineage.service import MetadataService

logger = logging.getLogger("metadata-lineage-tests")


@pytest.fixture
def service() -> MetadataService:
    return MetadataService()


def log_decision(title: str, payload, outcome) -> None:
    """在测试日志中打印输入与判定依据（caplog -v 时可见）。"""
    logger.warning("=== %s ===\n输入:\n%s\n判定: %s",
                   title, pprint.pformat(payload, width=100), outcome)


def expect_reason(fn, reason) -> MetadataError:
    with pytest.raises(MetadataError) as exc:
        fn()
    assert exc.value.reason is reason, (
        f"期望原因 {reason.name}，实际 {exc.value.reason.name}: {exc.value.message}"
    )
    return exc.value
