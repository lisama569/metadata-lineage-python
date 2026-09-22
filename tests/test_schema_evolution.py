"""结构版本演进：兼容变更追溯 + 三类不兼容变更的可区分拒绝原因。"""

from __future__ import annotations

import pytest

from metadata_lineage.models import ErrorCode, FieldSpec, FieldType, ServiceError
from tests.conftest import log_case


def _base_fields():
    return [
        FieldSpec("id", FieldType.INT64, nullable=False),
        FieldSpec("amount", FieldType.INT32, nullable=True),
        FieldSpec("name", FieldType.STRING, nullable=True),
    ]


def _entity(service):
    return service.register_entity("e1", "orders", _base_fields(), actor="alice")


def test_compatible_evolution_adds_field_and_keeps_versions(service, request):
    _entity(service)
    new_fields = _base_fields() + [FieldSpec("remark", FieldType.STRING, True)]
    entity, report = service.evolve_entity("e1", new_fields, actor="bob",
                                           note="add remark")
    log_case(request, {
        "input": [f"{f.name}:{f.type.value}" for f in new_fields],
        "compatible": report.compatible,
        "version_count": len(entity.versions),
        "uids_stable": [
            entity.versions[0].field_by_name(n).uid
            == entity.versions[1].field_by_name(n).uid
            for n in ("id", "amount", "name")
        ],
    })
    assert report.compatible is True
    assert len(entity.versions) == 2
    # 旧字段 uid 保持稳定，版本可追溯
    for name in ("id", "amount", "name"):
        assert (entity.versions[0].field_by_name(name).uid
                == entity.versions[1].field_by_name(name).uid)
    assert entity.versions[0].created_by == "alice"
    assert entity.versions[1].created_by == "bob"


def test_type_widening_is_compatible(service, request):
    _entity(service)
    fields = [
        FieldSpec("id", FieldType.INT64, nullable=False),
        FieldSpec("amount", FieldType.INT64, nullable=True),  # INT32 -> INT64
        FieldSpec("name", FieldType.STRING, nullable=True),
    ]
    _, report = service.evolve_entity("e1", fields, actor="bob")
    log_case(request, {"input": "amount INT32->INT64",
                       "compatible": report.compatible,
                       "breaking": report.breaking_changes})
    assert report.compatible is True


def test_removed_field_rejected_with_distinct_code(service, request):
    _entity(service)
    fields = [
        FieldSpec("id", FieldType.INT64, nullable=False),
        FieldSpec("name", FieldType.STRING, nullable=True),
    ]  # amount 被删除
    with pytest.raises(ServiceError) as exc:
        service.evolve_entity("e1", fields, actor="bob")
    log_case(request, {
        "input": [f.name for f in fields],
        "rejected_code": exc.value.code.value,
        "detail": exc.value.detail,
    })
    assert exc.value.code is ErrorCode.BREAKING_FIELD_REMOVED
    assert "amount" in exc.value.detail


def test_rename_rejected_with_distinct_code(service, request):
    _entity(service)
    old_uid = service.get_entity("e1").latest_version.field_by_name("amount").uid
    fields = [
        FieldSpec("id", FieldType.INT64, nullable=False),
        FieldSpec("total_amount", FieldType.INT32, True, uid=old_uid),  # 显式改名
        FieldSpec("name", FieldType.STRING, nullable=True),
    ]
    with pytest.raises(ServiceError) as exc:
        service.evolve_entity("e1", fields, actor="bob")
    log_case(request, {
        "input": {"old": "amount", "new": "total_amount", "uid": old_uid},
        "rejected_code": exc.value.code.value,
        "detail": exc.value.detail,
    })
    assert exc.value.code is ErrorCode.BREAKING_FIELD_RENAMED


def test_type_narrowing_rejected_with_distinct_code(service, request):
    _entity(service)
    fields = [
        FieldSpec("id", FieldType.INT32, nullable=False),  # INT64 -> INT32
        FieldSpec("amount", FieldType.INT32, nullable=True),
        FieldSpec("name", FieldType.STRING, nullable=True),
    ]
    with pytest.raises(ServiceError) as exc:
        service.evolve_entity("e1", fields, actor="bob")
    log_case(request, {"input": "id INT64->INT32",
                       "rejected_code": exc.value.code.value})
    assert exc.value.code is ErrorCode.BREAKING_TYPE_NARROWING


def test_unrelated_type_change_rejected_with_distinct_code(service, request):
    _entity(service)
    fields = [
        FieldSpec("id", FieldType.INT64, nullable=False),
        FieldSpec("amount", FieldType.BOOL, True),  # INT32 -> BOOL 互不相容
        FieldSpec("name", FieldType.STRING, nullable=True),
    ]
    with pytest.raises(ServiceError) as exc:
        service.evolve_entity("e1", fields, actor="bob")
    log_case(request, {"input": "amount INT32->BOOL",
                       "rejected_code": exc.value.code.value})
    assert exc.value.code is ErrorCode.BREAKING_INCOMPATIBLE_TYPE


def test_rejected_evolution_does_not_create_version(service, request):
    _entity(service)
    fields = [FieldSpec("id", FieldType.INT64, nullable=False)]
    with pytest.raises(ServiceError):
        service.evolve_entity("e1", fields, actor="bob")
    entity = service.get_entity("e1")
    log_case(request, {"version_count_after_reject": len(entity.versions)})
    assert len(entity.versions) == 1


def test_non_nullable_new_field_rejected(service, request):
    _entity(service)
    fields = _base_fields() + [FieldSpec("required_remark", FieldType.STRING, False)]
    with pytest.raises(ServiceError) as exc:
        service.evolve_entity("e1", fields, actor="bob")
    log_case(request, {"rejected_code": exc.value.code.value,
                       "detail": exc.value.detail})
    assert exc.value.code is ErrorCode.BREAKING_NULLABILITY_NARROWED
