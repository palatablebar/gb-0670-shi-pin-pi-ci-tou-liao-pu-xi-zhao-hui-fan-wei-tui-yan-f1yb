import sqlite3, json
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from app.config import database_path
from app.errors import TraceError
@contextmanager
def connection():
    path = database_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, payload TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL DEFAULT 'pending', note TEXT NOT NULL DEFAULT '')")
        db.execute("CREATE TABLE IF NOT EXISTS closings (run_id TEXT NOT NULL, closing_seq INTEGER NOT NULL, signed_version INTEGER NOT NULL DEFAULT 0, sign_state TEXT NOT NULL DEFAULT 'unsigned', sign_note TEXT NOT NULL DEFAULT '', snapshot TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT '', PRIMARY KEY(run_id, closing_seq))")
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()
def save(payload):
    key = payload["manifest"]["run_id"]
    with connection() as db:
        db.execute("INSERT OR IGNORE INTO runs(id,payload) VALUES (?,?)", (key,json.dumps(payload,ensure_ascii=False)))
    return get(key)
def get(key):
    with connection() as db:
        row = db.execute("SELECT * FROM runs WHERE id=?", (key,)).fetchone()
    if row is None:
        raise TraceError("NOT_FOUND", "批次不存在，请重新选择", 404)
    item=json.loads(row["payload"])
    item["review"]={"version":row["version"], "state":row["state"], "note":row["note"]}
    return item
def history():
    with connection() as db:
        rows=db.execute("SELECT id,version,state,note FROM runs ORDER BY rowid DESC LIMIT 100").fetchall()
    return [dict(r) for r in rows]
def review(key, version, state, note):
    if state not in {"approved", "rejected"}:
        raise TraceError("REVIEW_STATE", "请选择通过或驳回")
    payload=get(key)
    if payload["quarantine"] and state == "approved" and len(note.strip()) < 4:
        raise TraceError("REVIEW_NOTE", "隔离批次通过需填写复核依据")
    with connection() as db:
        updated=db.execute("UPDATE runs SET version=version+1,state=?,note=? WHERE id=? AND version=?", (state,note,key,version)).rowcount
        if not updated:
            raise TraceError("STALE", "批次已更新，请刷新后复核", 409)
    return get(key)


def _closing_rows(db, key):
    return db.execute(
        "SELECT closing_seq,signed_version,sign_state,sign_note,snapshot,created_at "
        "FROM closings WHERE run_id=? ORDER BY closing_seq", (key,)).fetchall()


def get_closings(key):
    """返回该运行的全部结账版次；不做存在性校验，由调用方先 get(key)。"""
    with connection() as db:
        rows = _closing_rows(db, key)
    return [{"closing_seq": r["closing_seq"], "signed_version": r["signed_version"],
             "sign_state": r["sign_state"], "sign_note": r["sign_note"],
             "created_at": r["created_at"], "snapshot": json.loads(r["snapshot"])}
            for r in rows]


def latest_closing(key):
    rows = get_closings(key)
    return rows[-1] if rows else None


def save_closing(key, snapshot):
    """整事务追加一次结账。同内容重结不新增版次、不动旧结论；写入失败整体回滚。

    幂等键 = 快照规范 JSON 的 sha256，与最新一版相同则直接复用，保证
    同一班重跑/重结既不新增记录也不覆盖旧结论。
    """
    import hashlib
    canonical = json.dumps(snapshot, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    stamped = dict(snapshot)
    stamped["digest"] = digest
    with connection() as db:
        rows = _closing_rows(db, key)
        if rows:
            last = rows[-1]
            if json.loads(last["snapshot"]).get("digest") == digest:
                return {"closing_seq": last["closing_seq"],
                        "signed_version": last["signed_version"],
                        "sign_state": last["sign_state"], "sign_note": last["sign_note"],
                        "created_at": last["created_at"],
                        "snapshot": json.loads(last["snapshot"]), "reused": True}
        seq = (rows[-1]["closing_seq"] + 1) if rows else 1
        created = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT INTO closings(run_id,closing_seq,snapshot,created_at) VALUES(?,?,?,?)",
            (key, seq, json.dumps(stamped, ensure_ascii=False), created))
    result = latest_closing(key)
    result["reused"] = False
    return result


def sign_closing(key, closing_seq, signed_version, state, note):
    """对指定结账版次签字，乐观锁防旧版覆盖；不改动快照本身。"""
    if state not in {"signed", "waived"}:
        raise TraceError("SIGN_STATE", "签字状态须为签字确认或异常放行")
    if state == "signed" and len(note.strip()) < 4:
        raise TraceError("SIGN_NOTE", "结账签字需填写复核依据")
    with connection() as db:
        row = db.execute(
            "SELECT signed_version FROM closings WHERE run_id=? AND closing_seq=?",
            (key, closing_seq)).fetchone()
        if row is None:
            raise TraceError("CLOSING_NOT_FOUND", "结账版次不存在，请重新结账", 404)
        updated = db.execute(
            "UPDATE closings SET sign_state=?,sign_note=?,signed_version=signed_version+1 "
            "WHERE run_id=? AND closing_seq=? AND signed_version=?",
            (state, note, key, closing_seq, signed_version)).rowcount
        if not updated:
            raise TraceError("STALE", "该结账版已签字，请刷新后重试", 409)
    return latest_closing(key)


def restore_closing(key, closing_seq):
    """把某个历史结账版恢复为当前可签字结果：追加为新版次，旧版全部保留可追溯。"""
    with connection() as db:
        rows = _closing_rows(db, key)
        target = next((r for r in rows if r["closing_seq"] == closing_seq), None)
        if target is None:
            raise TraceError("CLOSING_NOT_FOUND", "待恢复的结账版不存在", 404)
        snapshot = json.loads(target["snapshot"])
        seq = rows[-1]["closing_seq"] + 1
        created = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT INTO closings(run_id,closing_seq,snapshot,created_at) VALUES(?,?,?,?)",
            (key, seq, json.dumps(snapshot, ensure_ascii=False), created))
    result = latest_closing(key)
    result["restored_from"] = closing_seq
    result["reused"] = False
    return result
