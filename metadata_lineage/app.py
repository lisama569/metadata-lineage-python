"""FastAPI 装配层（HTTP 入口）。

所有业务拒绝（结构不兼容、环路、过期写入等）统一映射为 409，
响应体携带可区分的 reason 编码；普通校验/不存在映射为 400/404。
过期写入不使用 4xx 的模糊语义——同样 409 但 reason=STALE_VERSION。
"""

from fastapi import FastAPI
from pydantic import BaseModel, Field

from .errors import MetadataError, RejectReason
from .service import MetadataService

_NOT_FOUND = {RejectReason.ENTITY_NOT_FOUND, RejectReason.VERSION_NOT_FOUND,
              RejectReason.ENDPOINT_NOT_FOUND}
_BAD_REQUEST = {RejectReason.VALIDATION_FAILED}


def create_app(service: MetadataService | None = None) -> FastAPI:
    service = service or MetadataService()
    app = FastAPI(title="metadata-lineage")
    app.state.service = service

    class FieldIn(BaseModel):
        name: str
        type: str

    class RegisterIn(BaseModel):
        entity_id: str
        owner: str
        fields: list[FieldIn]

    class EvolveIn(BaseModel):
        fields: list[FieldIn]
        note: str = ""
        actor: str = "system"

    class FieldIn_(BaseModel):
        field_name: str
        actor: str = "system"

    class RenameIn(BaseModel):
        old_name: str
        new_name: str
        actor: str = "system"

    class OwnerIn(BaseModel):
        owner: str
        expected_revision: int
        actor: str = "system"

    class TagsIn(BaseModel):
        add: list[str] = Field(default_factory=list)
        remove: list[str] = Field(default_factory=list)
        expected_revision: int | None = None
        actor: str = "system"

    class EdgeIn(BaseModel):
        source_entity: str
        source_field: str
        target_entity: str
        target_field: str

    def _ver(v):
        return {"entity_id": v.entity_id, "version": v.version,
                "note": v.note, "created_at": v.created_at,
                "fields": [{"name": f.name, "type": f.type.name} for f in v.fields]}

    def _ent(e):
        return {"entity_id": e.entity_id, "current_version": e.current_version,
                "revision": e.revision, "owner": e.owner,
                "tags": sorted(e.tags), "created_at": e.created_at}

    def _edge(e):
        return {"source": {"entity": e.source[0], "field": e.source[1]},
                "target": {"entity": e.target[0], "field": e.target[1]},
                "status": e.status.name, "registered_at": e.registered_at}

    def _audit(a):
        return {"seq": a.seq, "timestamp": a.timestamp, "action": a.action,
                "actor": a.actor, "entity_id": a.entity_id,
                "success": a.success, "reason": a.reason, "detail": dict(a.detail)}

    @app.exception_handler(MetadataError)
    async def metadata_error_handler(_request, exc: MetadataError):
        from fastapi.responses import JSONResponse

        status = 404 if exc.reason in _NOT_FOUND else (
            400 if exc.reason in _BAD_REQUEST else 409
        )
        return JSONResponse(
            status_code=status,
            content={"error": exc.reason.name, "message": exc.message},
        )

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/entities")
    def register(body: RegisterIn):
        v = service.catalog.register_entity(
            body.entity_id, [(f.name, f.type) for f in body.fields], body.owner, "http",
        )
        return _ver(v)

    @app.get("/entities/{entity_id}")
    def get_entity(entity_id: str):
        return _ent(service.catalog.get_entity(entity_id))

    @app.get("/entities/{entity_id}/versions")
    def versions(entity_id: str):
        return [_ver(v) for v in service.catalog.list_versions(entity_id)]

    @app.post("/entities/{entity_id}/evolve")
    def evolve(entity_id: str, body: EvolveIn):
        v = service.catalog.evolve_schema(
            entity_id, [(f.name, f.type) for f in body.fields], body.actor, body.note,
        )
        return _ver(v)

    @app.post("/entities/{entity_id}/retire-field")
    def retire(entity_id: str, body: FieldIn_):
        v = service.catalog.retire_field(entity_id, body.field_name, body.actor)
        return _ver(v)

    @app.post("/entities/{entity_id}/rename-field")
    def rename(entity_id: str, body: RenameIn):
        v = service.catalog.rename_field(entity_id, body.old_name, body.new_name, body.actor)
        return _ver(v)

    @app.put("/entities/{entity_id}/owner")
    def put_owner(entity_id: str, body: OwnerIn):
        r = service.catalog.update_owner(
            entity_id, body.owner, body.expected_revision, body.actor,
        )
        return {"outcome": r.outcome.name,
                "expected_revision": r.expected_version,
                "actual_revision": r.actual_version,
                "reason": r.reason,
                "entity": _ent(r.entity) if r.entity else None,
                "audit_seq": r.audit_seq}

    @app.put("/entities/{entity_id}/tags")
    def put_tags(entity_id: str, body: TagsIn):
        r = service.catalog.update_tags(
            entity_id, body.add, body.remove, body.expected_revision, body.actor,
        )
        return {"outcome": r.outcome.name,
                "expected_revision": r.expected_version,
                "actual_revision": r.actual_version,
                "reason": r.reason,
                "entity": _ent(r.entity) if r.entity else None,
                "audit_seq": r.audit_seq}

    @app.post("/lineage/edges")
    def add_edge(body: EdgeIn):
        e = service.lineage.add_edge(
            (body.source_entity, body.source_field),
            (body.target_entity, body.target_field),
        )
        return _edge(e)

    @app.get("/lineage/edges")
    def list_edges(include_broken: bool = True):
        return [_edge(e) for e in service.lineage.edges(include_broken)]

    @app.get("/lineage/upstream/{entity_id}")
    def upstream(entity_id: str, field: str | None = None):
        return [{"entity": e[0], "field": e[1]}
                for e in sorted(service.lineage.upstream(entity_id, field))]

    @app.get("/lineage/downstream/{entity_id}")
    def downstream(entity_id: str, field: str | None = None):
        return [{"entity": e[0], "field": e[1]}
                for e in sorted(service.lineage.downstream(entity_id, field))]

    @app.get("/audit")
    def audit():
        return [_audit(a) for a in service.catalog.audit_log()]

    return app


app = create_app()
