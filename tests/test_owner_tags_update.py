"""属主/标签细粒度更新：整体替换、显式清空、两边互不干扰、未提到即保持。"""

from __future__ import annotations

import pytest

from metadata_lineage.models import ErrorCode, FieldSpec, FieldType, ServiceError
from tests.conftest import log_case


def _entity(service):
    service.register_entity(
        "e1", "orders", [FieldSpec("id", FieldType.INT64)], actor="alice")
    # 初始状态：owner="bob", tags={"pii", "core"}，revision=1
    service.update_owner_tags(
        "e1", expected_revision=0, owner="bob",
        add_tags={"pii", "core"}, actor="alice")


def test_update_tags_only_leaves_owner_untouched(service, request):
    _entity(service)
    entity, rev = service.update_owner_tags(
        "e1", expected_revision=1, add_tags={"critical"},
        remove_tags={"core"}, actor="carol")
    log_case(request, {"input": "add={critical}, remove={core}, owner 未提到",
                       "owner": entity.owner, "tags": sorted(entity.tags),
                       "revision": rev})
    assert rev == 2
    assert entity.owner == "bob"  # 属主原样不动
    assert entity.tags == {"pii", "critical"}


def test_update_owner_only_leaves_tags_untouched(service, request):
    _entity(service)
    entity, rev = service.update_owner_tags(
        "e1", expected_revision=1, owner="dave", actor="carol")
    log_case(request, {"input": "owner=dave, tags 未提到",
                       "owner": entity.owner, "tags": sorted(entity.tags),
                       "revision": rev})
    assert rev == 2
    assert entity.owner == "dave"
    assert entity.tags == {"pii", "core"}  # 标签原样不动


def test_explicit_owner_clear(service, request):
    """owner=None 是显式清空，不是“没填”。"""
    _entity(service)
    entity, rev = service.update_owner_tags(
        "e1", expected_revision=1, owner=None, actor="carol")
    log_case(request, {"input": "owner=None（显式清空）",
                       "owner": entity.owner, "tags": sorted(entity.tags),
                       "revision": rev})
    assert rev == 2
    assert entity.owner is None
    assert entity.tags == {"pii", "core"}  # 标签不受影响


def test_omitted_owner_keeps_value(service, request):
    """完全不提 owner：保持原值（与显式清空区分）。"""
    _entity(service)
    entity, _ = service.update_owner_tags(
        "e1", expected_revision=1, add_tags={"x"}, actor="carol")
    log_case(request, {"input": "仅 add_tags={x}", "owner": entity.owner})
    assert entity.owner == "bob"


def test_set_tags_full_replace(service, request):
    """set_tags 整体替换：新集合完全取代旧集合。"""
    _entity(service)
    entity, rev = service.update_owner_tags(
        "e1", expected_revision=1, set_tags={"a", "b"}, actor="carol")
    log_case(request, {"input": "set_tags={a,b}（整体替换）",
                       "tags": sorted(entity.tags), "owner": entity.owner,
                       "revision": rev})
    assert rev == 2
    assert entity.tags == {"a", "b"}  # 旧标签 pii/core 被替换掉
    assert entity.owner == "bob"  # 属主不受影响


def test_set_tags_empty_clears_all(service, request):
    """set_tags=空集合：显式清空全部标签。"""
    _entity(service)
    entity, _ = service.update_owner_tags(
        "e1", expected_revision=1, set_tags=set(), actor="carol")
    log_case(request, {"input": "set_tags=∅（显式清空标签）",
                       "tags": sorted(entity.tags)})
    assert entity.tags == set()


def test_set_tags_rejects_mix_with_incremental(service, request):
    """整体替换与增量改动互斥：同批给出即校验失败，且不落库。"""
    _entity(service)
    with pytest.raises(ServiceError) as exc:
        service.update_owner_tags(
            "e1", expected_revision=1, set_tags={"a"},
            add_tags={"b"}, actor="carol")
    log_case(request, {"input": "set_tags={a} + add_tags={b}",
                       "rejected_code": exc.value.code.value})
    assert exc.value.code is ErrorCode.VALIDATION_ERROR
    entity = service.get_entity("e1")
    assert entity.tags == {"pii", "core"}  # 未生效
    assert entity.revision == 1  # revision 不推进


def test_invalid_owner_rejected(service, request):
    _entity(service)
    with pytest.raises(ServiceError) as exc:
        service.update_owner_tags(
            "e1", expected_revision=1, owner="   ", actor="carol")
    log_case(request, {"input": "owner='   '（空白字符串）",
                       "rejected_code": exc.value.code.value})
    assert exc.value.code is ErrorCode.VALIDATION_ERROR
    assert service.get_entity("e1").owner == "bob"
