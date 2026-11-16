from decimal import Decimal
from app.modules.reconcile.closing import settle
from app.reasons import issue


def _edge(line, frm, to, kg, output, loss):
    return {"line": line, "event_id": "E%s" % line, "from_lot": frm, "to_lot": to,
            "kg": str(kg), "output_kg": str(output), "loss_kg": str(loss),
            "status": "active", "revision": 1, "raw": {}}


def test_pending_transfer_is_wip_balance_not_anomaly():
    # M-0001 产出95，仅领用48，余47 为中间批跨班在制；账面守恒
    edges = [_edge(1, "R-0001", "M-0001", 100, 95, 5),
             _edge(2, "M-0001", "F-0001", 48, 47, 1)]
    r = settle(edges, [], [])
    m = next(a for a in r["accounts"] if a["lot"] == "M-0001")
    assert m["state"] == "pending_transfer"
    assert Decimal(m["used_kg"]) == Decimal("48")
    assert Decimal(m["balance_kg"]) == Decimal("47")
    assert Decimal(m["pending_transfer_kg"]) == Decimal("47")
    assert Decimal(m["shortage_kg"]) == 0
    assert r["summary"]["shift_clean"]
    assert r["summary"]["pending_lots"] == 1


def test_fully_consumed_batch_closes_zero():
    edges = [_edge(1, "R-0001", "M-0002", 50, 48, 2),
             _edge(2, "M-0002", "F-0002", 48, 47, 1)]
    r = settle(edges, [], [])
    m = next(a for a in r["accounts"] if a["lot"] == "M-0002")
    assert m["state"] == "closed"
    assert Decimal(m["balance_kg"]) == 0
    assert Decimal(m["pending_transfer_kg"]) == 0


def test_finished_and_raw_balance_are_not_wip():
    edges = [_edge(1, "R-0001", "M-0003", 60, 58, 2),
             _edge(2, "M-0003", "F-0003", 40, 39, 1)]
    r = settle(edges, [], [])
    f = next(a for a in r["accounts"] if a["lot"] == "F-0003")
    assert f["state"] == "finished_goods"
    assert Decimal(f["finished_kg"]) == Decimal(f["balance_kg"])
    assert Decimal(f["pending_transfer_kg"]) == 0
    # 原料批不在 to_lot 组内，不产生账户（原料库存由其自身产出账决定）
    assert all(a["lot"] != "R-0001" for a in r["accounts"])


def test_mass_gap_shortage_is_abnormal_not_hidden_in_wip():
    # 投入30，产出29损耗0 => 短缺1；该批被隔离，不能算在制
    row = {"line": 9, "raw": {"event_id": "E9", "from_lot": "R-0200", "to_lot": "M-0900",
                              "quantity": "30", "output_kg": "29", "loss_kg": "0"}}
    issues = [issue(row, "MASS_GAP")]
    r = settle([], issues, [])
    a = next(x for x in r["accounts"] if x["lot"] == "M-0900")
    assert a["state"] == "abnormal"
    assert "MASS_GAP" in a["reasons"]
    assert Decimal(a["shortage_kg"]) == Decimal("1")
    assert Decimal(a["pending_transfer_kg"]) == 0
    assert not r["summary"]["shift_clean"]
    assert r["summary"]["abnormal_lots"] == 1


def test_overdraw_batch_flagged_with_reason_and_lines():
    row = {"line": 12, "raw": {"to_lot": "M-0700", "quantity": "10",
                               "output_kg": "10", "loss_kg": "0"}}
    r = settle([], [issue(row, "OVERDRAW")], [])
    a = next(x for x in r["accounts"] if x["lot"] == "M-0700")
    assert a["state"] == "abnormal"
    assert a["reasons"] == ["OVERDRAW"]
    assert 12 in a["lines"]


def test_unparsable_quantity_counted_and_not_crashing():
    row = {"line": 3, "raw": {"to_lot": "M-0800", "quantity": "N/A",
                              "output_kg": "0", "loss_kg": "0"}}
    r = settle([], [issue(row, "NULL_VALUE")], [])
    a = next(x for x in r["accounts"] if x["lot"] == "M-0800")
    assert a["state"] == "abnormal"
    assert a["unparsable_lines"] == 1
    assert Decimal(a["input_kg"]) == 0


def test_deterministic_and_decimal_exact():
    edges = [_edge(1, "R-1", "M-1", "80.01", "80.00", "0.01")]
    a = settle(edges, [], [])
    b = settle(edges, [], [])
    assert a == b
    acc = a["accounts"][0]
    assert Decimal(acc["input_kg"]) == Decimal("80.01")


def test_version_decisions_are_not_material_abnormal():
    # SUPERSEDED / VOID / DUPLICATE 不应当成物料异常批
    row = {"line": 5, "raw": {"to_lot": "M-0600", "quantity": "10",
                              "output_kg": "9", "loss_kg": "1"}}
    r = settle([], [], [issue(row, "VOID"), issue(row, "SUPERSEDED")])
    assert r["accounts"] == []
    assert r["summary"]["shift_clean"]
