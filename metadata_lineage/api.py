"""FastAPI HTTP 适配层：请求/响应模型与错误码映射。

错误响应统一形如：
    {"error": {"code": "<ERROR_CODE>", "detail": "...", "extra": {...}}}
冲突（409）、校验失败（422）与未找到（404）可通过 code 明确区分。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .models import ErrorCode, FieldSpec, FieldType, Ref, ServiceError
from .service import _UNSET, CatalogService, OwnerTagsUpdate

_CONFLICT = {
    ErrorCode.ENTITY_ALREADY_EXISTS,
    ErrorCode.VERSION_CONFLICT,
    ErrorCode.BREAKING_FIELD_REMOVED,
    ErrorCode.BREAKING_FIELD_RENAMED,
    ErrorCode.BREAKING_TYPE_NARROWING,
    ErrorCode.BREAKING_INCOMPATIBLE_TYPE,
    ErrorCode.BREAKING_NULLABILITY_NARROWED,
    ErrorCode.LINEAGE_DUPLICATE,
    ErrorCode.LINEAGE_CYCLE,
    ErrorCode.CONFLICT_REVISION,
}
_NOT_FOUND = {
    ErrorCode.ENTITY_NOT_FOUND,
    ErrorCode.VERSION_NOT_FOUND,
    ErrorCode.FIELD_NOT_FOUND,
    ErrorCode.LINEAGE_ENDPOINT_NOT_FOUND,
    ErrorCode.LINEAGE_FIELD_NOT_FOUND,
}


class FieldIn(BaseModel):
    name: str
    type: FieldType
    nullable: bool = True
    uid: str | None = None

    def to_spec(self) -> FieldSpec:
        return FieldSpec(name=self.name, type=self.type,
                         nullable=self.nullable, uid=self.uid)


class RegisterEntityIn(BaseModel):
    name: str
    fields: list[FieldIn]
    actor: str = "anonymous"
    note: str = ""


class EvolveEntityIn(BaseModel):
    fields: list[FieldIn]
    actor: str = "anonymous"
    note: str = ""
    allow_breaking: bool = False


class RefIn(BaseModel):
    entity_id: str
    field_name: str
    version: int | None = None


class LineageIn(BaseModel):
    src: RefIn
    dst: RefIn
    actor: str = "anonymous"


class OwnerTagsIn(BaseModel):
    """属主/标签更新。

    语义约定（“没提到”≠“清空”）：
    - owner 字段不出现 → 属主保持原值；显式 "owner": null → 清空属主；
    - set_tags 出现 → 标签整体替换为该集合（与 add/remove 互斥）；
    - set_tags 不出现 → 按 add_tags/remove_tags 做增量改动；
    - 三者都不出现 → 标签保持原值。
    """

    expected_revision: int
    owner: str | None = None
    set_tags: list[str] | None = None
    add_tags: list[str] = Field(default_factory=list)
    remove_tags: list[str] = Field(default_factory=list)
    actor: str = "anonymous"

    def owner_arg(self) -> object:
        """区分“未提供 owner”（_UNSET，保持原值）与显式 null（清空）。"""
        return self.owner if "owner" in self.model_fields_set else _UNSET

    def set_tags_arg(self) -> object:
        return (set(self.set_tags) if self.set_tags is not None
                and "set_tags" in self.model_fields_set else _UNSET)


class OwnerTagsBatchItemIn(OwnerTagsIn):
    entity_id: str


class OwnerTagsBatchIn(BaseModel):
    items: list[OwnerTagsBatchItemIn]


def _field_out(f: Any) -> dict[str, Any]:
    return {"uid": f.uid, "name": f.name, "type": f.type.value,
            "nullable": f.nullable}


def _version_out(v: Any) -> dict[str, Any]:
    return {
        "version": v.version,
        "fields": [_field_out(f) for f in v.fields],
        "created_at": v.created_at.isoformat(),
        "created_by": v.created_by,
        "note": v.note,
    }


def _entity_out(e: Any) -> dict[str, Any]:
    return {
        "entity_id": e.entity_id,
        "name": e.name,
        "owner": e.owner,
        "tags": sorted(e.tags),
        "revision": e.revision,
        "latest_version": e.latest_version.version if e.latest_version else None,
        "versions": [_version_out(v) for v in e.versions],
    }


def _ref_out(r: Ref) -> dict[str, Any]:
    return {"entity_id": r.entity_id, "field_uid": r.field_uid}


def _edge_out(e: Any) -> dict[str, Any]:
    return {
        "edge_id": e.edge_id,
        "src": _ref_out(e.src),
        "dst": _ref_out(e.dst),
        "active": e.active,
        "created_at": e.created_at.isoformat(),
        "created_by": e.created_by,
    }


def create_app(service: CatalogService | None = None) -> FastAPI:
    app = FastAPI(title="metadata-lineage", version="0.1.0")
    app.state.service = service or CatalogService()

    @app.exception_handler(ServiceError)
    async def _service_error_handler(_request: Request, exc: ServiceError):
        if exc.code in _NOT_FOUND:
            status = 404
        elif exc.code in _CONFLICT:
            status = 409
        elif exc.code == ErrorCode.VALIDATION_ERROR:
            status = 422
        else:
            status = 400
        return JSONResponse(
            status_code=status,
            content={"error": {
                "code": exc.code.value,
                "detail": exc.detail or exc.code.value,
                "extra": exc.extra,
            }},
        )

    def svc() -> CatalogService:
        return app.state.service

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/entities/{entity_id}", status_code=201)
    def register_entity(entity_id: str, body: RegisterEntityIn):
        entity = svc().register_entity(
            entity_id=entity_id, name=body.name,
            fields=[f.to_spec() for f in body.fields],
            actor=body.actor, note=body.note,
        )
        return _entity_out(entity)

    @app.post("/entities/{entity_id}/versions", status_code=201)
    def evolve_entity(entity_id: str, body: EvolveEntityIn):
        entity, report = svc().evolve_entity(
            entity_id=entity_id,
            fields=[f.to_spec() for f in body.fields],
            actor=body.actor, note=body.note,
            allow_breaking=body.allow_breaking,
        )
        return {
            "entity": _entity_out(entity),
            "compatibility": {
                "compatible": report.compatible,
                "breaking_changes": [
                    {"code": c.code.value, "field": c.field_name,
                     "detail": c.detail}
                    for c in report.breaking_changes
                ],
            },
        }

    @app.get("/entities/{entity_id}")
    def get_entity(entity_id: str):
        return _entity_out(svc().get_entity(entity_id))

    @app.post("/lineage", status_code=201)
    def register_lineage(body: LineageIn):
        src = svc().resolve_ref(body.src.entity_id, body.src.field_name,
                                body.src.version)
        dst = svc().resolve_ref(body.dst.entity_id, body.dst.field_name,
                                body.dst.version)
        edge = svc().register_lineage(src, dst, body.actor)
        return _edge_out(edge)

    @app.get("/lineage/impact")
    def impact(entity_id: str, field_name: str, version: int | None = None,
               max_depth: int | None = None, max_nodes: int | None = None):
        ref = svc().resolve_ref(entity_id, field_name, version)
        result = svc().impact(ref, max_depth=max_depth, max_nodes=max_nodes)
        return {
            "ref": _ref_out(ref),
            "upstream": [_ref_out(r) for r in result.upstream],
            "downstream": [_ref_out(r) for r in result.downstream],
            "active_edges": [_edge_out(e) for e in result.active_edges],
            "dangling_edges": [_edge_out(e) for e in result.dangling_edges],
            "truncated": result.truncated,
        }

    @app.put("/entities/{entity_id}/owner-tags")
    def update_owner_tags(entity_id: str, body: OwnerTagsIn):
        entity, revision = svc().update_owner_tags(
            entity_id,
            expected_revision=body.expected_revision,
            owner=body.owner_arg(),
            set_tags=body.set_tags_arg(),
            add_tags=set(body.add_tags),
            remove_tags=set(body.remove_tags),
            actor=body.actor,
        )
        return _entity_out(entity) | {"revision": revision}

    @app.post("/owner-tags/batch")
    def batch_update_owner_tags(body: OwnerTagsBatchIn):
        """批量原子更新：整批要么全部生效、要么全部不落库。

        始终返回 200，逐条结论在 results 中；applied=false 表示整批未落库。
        """
        result = svc().batch_update_owner_tags([
            OwnerTagsUpdate(
                entity_id=item.entity_id,
                expected_revision=item.expected_revision,
                owner=item.owner_arg(),
                set_tags=item.set_tags_arg(),
                add_tags=frozenset(item.add_tags),
                remove_tags=frozenset(item.remove_tags),
                actor=item.actor,
            )
            for item in body.items
        ])
        return {
            "applied": result.applied,
            "results": [
                {
                    "entity_id": r.entity_id,
                    "status": r.status,
                    "reason_code": r.reason_code,
                    "revision": r.revision,
                    "detail": r.detail,
                }
                for r in result.results
            ],
        }

    @app.get("/audit")
    def audit_log(limit: int | None = None):
        entries = svc().audit_log()
        if limit is not None:
            entries = entries[-limit:]
        return [{
            "audit_id": a.audit_id,
            "at": a.at.isoformat(),
            "actor": a.actor,
            "action": a.target and a.action,
            "target": a.target,
            "status": a.status,
            "reason_code": a.reason_code,
            "detail": a.detail,
            "revision_before": a.revision_before,
            "revision_after": a.revision_after,
        } for a in entries]

    return app


app = create_app()
