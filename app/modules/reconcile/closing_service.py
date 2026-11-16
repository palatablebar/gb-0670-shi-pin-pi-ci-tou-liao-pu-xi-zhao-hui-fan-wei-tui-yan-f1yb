from app import store
from app.errors import TraceError
from app.modules.reconcile.closing import settle


def _snapshot(payload):
    """由运行结果生成确定性结账快照（不含时间戳，保证同内容重结幂等）。"""
    closing = settle(payload["edges"], payload["quarantine"], payload["excluded"])
    return {"run_id": payload["manifest"]["run_id"],
            "rule_version": payload["manifest"]["rule_version"],
            "source_sha256": payload["manifest"]["sha256"],
            "shift": {"input_rows": payload["reconcile"]["input_rows"],
                      "accepted": payload["reconcile"]["accepted"],
                      "isolated": payload["reconcile"]["isolated"],
                      "excluded": payload["reconcile"]["excluded"]},
            "accounts": closing["accounts"], "summary": closing["summary"]}


def close_run(key):
    payload = store.get(key)
    return store.save_closing(key, _snapshot(payload))


def sign_run(key, closing_seq, signed_version, state, note):
    store.get(key)
    return store.sign_closing(key, closing_seq, signed_version, state, note)


def restore_run(key, closing_seq):
    store.get(key)
    return store.restore_closing(key, closing_seq)


def history(key):
    store.get(key)
    return store.get_closings(key)
