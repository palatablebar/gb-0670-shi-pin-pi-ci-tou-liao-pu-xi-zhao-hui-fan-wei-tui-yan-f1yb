from fastapi import APIRouter, Body
from app.modules.reconcile.service import compare
from app.modules.reconcile import closing_service
from app.errors import TraceError
router=APIRouter(prefix="/api/reconcile",tags=["reconcile"])
@router.post("")
def run(payload: dict = Body(...)):
    try:
        edges=payload["edges"]; intake=payload["intake"]; issues=payload["issues"]; excluded=payload["excluded"]
        return compare(edges,intake,issues,excluded)
    except (KeyError,TypeError,AttributeError) as exc:
        raise TraceError("FIELDS", "字段缺失或类型错误，请检查输入") from exc


@router.post("/runs/{key}/close")
def close(key: str):
    return closing_service.close_run(key)


@router.get("/runs/{key}/closings")
def closings(key: str):
    return {"items": closing_service.history(key)}


@router.post("/runs/{key}/closings/{seq}/sign")
def sign(key: str, seq: int, payload: dict = Body(...)):
    version = payload.get("signed_version")
    if type(version) is not int:
        raise TraceError("VERSION", "签字版本必须为整数")
    note = payload.get("note", "")
    if not isinstance(note, str) or len(note) > 500:
        raise TraceError("NOTE", "签字依据最长500字")
    return closing_service.sign_run(key, seq, version, payload.get("state"), note)


@router.post("/runs/{key}/closings/{seq}/restore")
def restore(key: str, seq: int):
    return closing_service.restore_run(key, seq)
