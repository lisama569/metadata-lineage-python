"""属主/标签细粒度更新语义：

- 只改标签时属主不动，只改属主时标签不动（两边互不干扰）；
- owner 显式 None = 清空属主；字段缺省 = 未提及，保持原值；
- replace_tags = 整体替换标签集合（含空集合清空），不得与 add/remove 混用。
"""

from __future__ import annotations

import pytest

from metadata_lineage.models import ErrorCode, FieldSpec, FieldType, ServiceError
from tests.conftest import log_case


def _entity(service, owner="bob", tags=("pii", "finance")):
    service.register_entity("e1", "orders",
                            [FieldSpec("id", FieldType.INT64)], actor="alice")
    service.update_owner_tags("e1", expected_revision=0, owner=owner,
                              add_tags=set(tags), actor="bob")


def test_tags_only_change_leaves_owner_untouched(service, request):
    _entity(service)
    entity, rev = service.update_owner_tags(
        "e1", expected_revision=1, replace_tags={"a", "b"}, actor="carol")
    log_case(request, {"input": "replace_tags={'a','b'} (owner not mentioned)",
                       "owner": entity.owner, "tags": sorted(entity.tags)})
    assert entity.owner == "bob"          # 属主原样保留
    assert entity.tags == {"a", "b"}
    assert rev == 2


def test_owner_only_change_leaves_tags_untouched(service, request):
    _entity(service)
    entity, rev = service.update_owner_tags(
        "e1", expected_revision=1, owner="carol", actor="carol")
    log_case(request, {"input": "owner='carol' (tags not mentioned)",
                       "owner": entity.owner, "tags": sorted(entity.tags)})
    assert entity.owner == "carol"
    assert entity.tags == {"pii", "finance"}
    assert rev == 2


def test_explicit_none_clears_owner_but_keeps_tags(service, request):
    _entity(service)
    entity, rev = service.update_owner_tags(
        "e1", expected_revision=1, owner=None, actor="carol")
    log_case(request, {"input": "owner=None explicitly",
                       "owner": entity.owner, "tags": sorted(entity.tags)})
    assert entity.owner is None           # 显式清空
    assert entity.tags == {"pii", "finance"}  # 标签不受影响
    assert rev == 2


def test_replace_tags_with_empty_set_clears_tags(service, request):
    _entity(service)
    entity, rev = service.update_owner_tags(
        "e1", expected_revision=1, replace_tags=set(), actor="carol")
    log_case(request, {"input": "replace_tags=set()",
                       "owner": entity.owner, "tags": sorted(entity.tags)})
    assert entity.tags == set()
    assert entity.owner == "bob"


def test_incremental_add_remove_still_works(service, request):
    _entity(service)
    entity, rev = service.update_owner_tags(
        "e1", expected_revision=1,
        add_tags={"x"}, remove_tags={"pii"}, actor="carol")
    log_case(request, {"input": "add x / remove pii",
                       "tags": sorted(entity.tags)})
    assert entity.tags == {"finance", "x"}
    assert entity.owner == "bob"


def test_replace_tags_conflicts_with_add_remove(service, request):
    _entity(service)
    with pytest.raises(ServiceError) as exc:
        service.update_owner_tags(
            "e1", expected_revision=1, replace_tags={"z"}, add_tags={"y"},
            actor="carol")
    log_case(request, {"rejected_code": exc.value.code.value})
    assert exc.value.code is ErrorCode.VALIDATION_ERROR
    # 校验失败不推进 revision，原值不变
    after = service.get_entity("e1")
    assert after.revision == 1
    assert after.tags == {"pii", "finance"}


def test_null_replace_tags_rejected(service, request):
    _entity(service)
    with pytest.raises(ServiceError) as exc:
        service.update_owner_tags(
            "e1", expected_revision=1, replace_tags=None, actor="carol")
    log_case(request, {"rejected_code": exc.value.code.value})
    assert exc.value.code is ErrorCode.VALIDATION_ERROR
    assert service.get_entity("e1").revision == 1


def test_blank_owner_rejected(service, request):
    _entity(service)
    with pytest.raises(ServiceError) as exc:
        service.update_owner_tags(
            "e1", expected_revision=1, owner="   ", actor="carol")
    log_case(request, {"rejected_code": exc.value.code.value})
    assert exc.value.code is ErrorCode.VALIDATION_ERROR
    assert service.get_entity("e1").owner == "bob"


def test_audit_records_owner_and_tag_modes(service, request):
    _entity(service)
    service.update_owner_tags("e1", expected_revision=1, owner=None,
                              actor="carol")
    service.update_owner_tags("e1", expected_revision=2, replace_tags={"z"},
                              actor="carol")
    audits = [a for a in service.audit_log()
              if a.action == "update_owner_tags" and a.status == "success"]
    modes = [(a.detail["owner_mode"], a.detail["tag_mode"]) for a in audits]
    log_case(request, {"audit_modes": modes})
    assert ("set", "add_remove") in modes       # 初始化
    assert ("cleared", "unchanged") in modes    # 只清属主
    assert ("unchanged", "replace") in modes    # 只整体换标签


# ---- HTTP 层：字段缺省 vs 显式 null ----
def _client_with_entity():
    from fastapi.testclient import TestClient
    from metadata_lineage.api import create_app
    from metadata_lineage.service import CatalogService
    from metadata_lineage.storage import CatalogStore

    service = CatalogService(CatalogStore())
    service.register_entity("e1", "orders",
                            [FieldSpec("id", FieldType.INT64)], actor="alice")
    service.update_owner_tags("e1", expected_revision=0, owner="bob",
                              add_tags={"pii"}, actor="bob")
    return TestClient(create_app(service))


def test_http_owner_omitted_means_unchanged_null_means_clear(request):
    client = _client_with_entity()
    # 请求体不含 owner：属主保持 bob
    r1 = client.put("/entities/e1/owner-tags",
                    json={"expected_revision": 1, "replace_tags": ["a"]})
    assert r1.status_code == 200
    assert r1.json()["owner"] == "bob"
    assert r1.json()["tags"] == ["a"]
    # 显式 owner=null：清空属主
    r2 = client.put("/entities/e1/owner-tags",
                    json={"expected_revision": 2, "owner": None})
    body2 = r2.json()
    log_case(request, {"r1": {"owner": "bob(kept)", "tags": ["a"]},
                       "r2": {"owner": body2["owner"], "tags": body2["tags"]}})
    assert r2.status_code == 200
    assert body2["owner"] is None
    assert body2["tags"] == ["a"]          # 标签未提及 -> 原样


def test_http_replace_tags_empty_list_clears_tags():
    client = _client_with_entity()
    r = client.put("/entities/e1/owner-tags",
                   json={"expected_revision": 1, "replace_tags": []})
    assert r.status_code == 200
    assert r.json()["tags"] == []
    assert r.json()["owner"] == "bob"


def test_http_replace_tags_null_is_422(request):
    client = _client_with_entity()
    r = client.put("/entities/e1/owner-tags",
                   json={"expected_revision": 1, "replace_tags": None})
    log_case(request, {"status": r.status_code, "body": r.json()})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
