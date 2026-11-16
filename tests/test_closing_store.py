import pytest
from app import store
from app.errors import TraceError
from app.modules.reconcile import closing_service


def test_close_is_idempotent_same_run(result):
    key = result["manifest"]["run_id"]
    a = closing_service.close_run(key)
    b = closing_service.close_run(key)
    assert a["closing_seq"] == b["closing_seq"]
    assert b["reused"] is True
    assert len(store.get_closings(key)) == 1


def test_rerun_same_source_adds_no_new_closing(result):
    key = result["manifest"]["run_id"]
    first = closing_service.close_run(key)
    # 同一班按原字节重跑，run_id 内容寻址相同，结账结论也不新增
    again = closing_service.close_run(key)
    assert again["closing_seq"] == first["closing_seq"]
    assert store.latest_closing(key)["snapshot"]["digest"] == first["snapshot"]["digest"]


def test_sign_uses_optimistic_lock(result):
    key = result["manifest"]["run_id"]
    item = closing_service.close_run(key)
    seq = item["closing_seq"]
    signed = store.sign_closing(key, seq, 0, "signed", "账实核对一致")
    assert signed["sign_state"] == "signed"
    assert signed["signed_version"] == 1
    with pytest.raises(TraceError) as e:
        store.sign_closing(key, seq, 0, "waived", "旧版重复提交")
    assert e.value.status == 409
    # 旧结论未被覆盖
    assert store.latest_closing(key)["sign_state"] == "signed"


def test_sign_requires_note(result):
    key = result["manifest"]["run_id"]
    item = closing_service.close_run(key)
    with pytest.raises(TraceError) as e:
        store.sign_closing(key, item["closing_seq"], 0, "signed", "短")
    assert e.value.code == "SIGN_NOTE"


def test_restore_appends_keeps_history(result):
    key = result["manifest"]["run_id"]
    first = closing_service.close_run(key)
    # 手工制造第二版（不同快照）后恢复第一版
    snap2 = dict(first["snapshot"])
    snap2["summary"] = dict(snap2["summary"], lot_total=999)
    second = store.save_closing(key, snap2)
    assert second["closing_seq"] == 2
    restored = closing_service.restore_run(key, first["closing_seq"])
    assert restored["closing_seq"] == 3
    assert restored["restored_from"] == 1
    assert restored["snapshot"]["summary"]["lot_total"] == first["snapshot"]["summary"]["lot_total"]
    # 三版全部保留，旧版仍可查
    seqs = [c["closing_seq"] for c in store.get_closings(key)]
    assert seqs == [1, 2, 3]


def test_failed_sign_rolls_back_keeping_signable_version(result):
    key = result["manifest"]["run_id"]
    item = closing_service.close_run(key)
    seq = item["closing_seq"]
    store.sign_closing(key, seq, 0, "signed", "首版签字依据")
    # 用陈旧版本号签字触发失败：事务回滚，旧版可签字结果保持不变
    with pytest.raises(TraceError):
        store.sign_closing(key, seq, 0, "waived", "陈旧提交应失败")
    latest = store.latest_closing(key)
    assert latest["sign_state"] == "signed"
    assert latest["signed_version"] == 1


def test_reclose_after_restore_reuses_without_extra_record(result):
    key = result["manifest"]["run_id"]
    first = closing_service.close_run(key)
    snap2 = dict(first["snapshot"])
    snap2["summary"] = dict(snap2["summary"], lot_total=1)
    store.save_closing(key, snap2)
    restored = closing_service.restore_run(key, first["closing_seq"])
    assert restored["closing_seq"] == 3
    again = closing_service.close_run(key)
    assert again["closing_seq"] == 3 and again["reused"] is True
    assert [c["closing_seq"] for c in store.get_closings(key)] == [1, 2, 3]


def test_close_api_flow(client, result):
    key = result["manifest"]["run_id"]
    r = client.post(f"/api/reconcile/runs/{key}/close")
    assert r.status_code == 200
    seq = r.json()["closing_seq"]
    again = client.post(f"/api/reconcile/runs/{key}/close")
    assert again.json()["reused"] is True
    hist = client.get(f"/api/reconcile/runs/{key}/closings").json()["items"]
    assert len(hist) == 1
    bad = client.post(f"/api/reconcile/runs/{key}/closings/{seq}/sign",
                      json={"signed_version": 0, "state": "signed", "note": "短"})
    assert bad.status_code == 422
    ok = client.post(f"/api/reconcile/runs/{key}/closings/{seq}/sign",
                     json={"signed_version": 0, "state": "signed", "note": "账实一致通过"})
    assert ok.status_code == 200 and ok.json()["sign_state"] == "signed"
    stale = client.post(f"/api/reconcile/runs/{key}/closings/{seq}/sign",
                        json={"signed_version": 0, "state": "waived", "note": "旧版重放"})
    assert stale.status_code == 409


def test_reconcile_payload_carries_closing_breakdown(client):
    d = client.post("/api/runs", json={"sample": "clean"}).json()
    closing = d["reconcile"]["closing"]
    assert "accounts" in closing and "summary" in closing
    for a in closing["accounts"]:
        # 每个账户四量自洽：结存 = 待转 + 完工 + 库存
        from decimal import Decimal
        bal = Decimal(a["balance_kg"])
        parts = Decimal(a["pending_transfer_kg"]) + Decimal(a["finished_kg"]) + Decimal(a["raw_stock_kg"])
        if a["state"] != "abnormal":
            assert bal == parts
