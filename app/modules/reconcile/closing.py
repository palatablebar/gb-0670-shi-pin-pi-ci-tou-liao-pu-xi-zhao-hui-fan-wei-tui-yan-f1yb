from collections import defaultdict
from decimal import Decimal, InvalidOperation
from app.reasons import REASONS

ZERO = Decimal("0")
# 隔离原因里属于"真实物料异常"的码；SUPERSEDED/DUPLICATE/VOID 属版本裁决，不判物料异常
MATERIAL_REASONS = {"MASS_GAP", "OVERDRAW", "ORPHAN", "SPEC_CONFLICT",
                    "BAD_DECIMAL", "NULL_VALUE", "NEGATIVE", "CYCLE"}


def _q(value):
    """台账数量严格十进制解析；无法解析时按 0 计并标记，绝不用浮点。"""
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _kind(lot):
    # R- 外购原料、F- 成品，其余为厂内中间批（在制只可能落在中间批上）
    if lot.startswith(("R-", "Ｒ-")):
        return "raw"
    if lot.startswith(("F-", "Ｆ-")):
        return "finished"
    return "wip"


def _material_issues(issues):
    """把隔离/排除记录收敛成 目标批 -> 原因码集合 + 行号，只保留物料异常。"""
    reasons = defaultdict(set)
    lines = defaultdict(list)
    for row in issues:
        code = row.get("reason")
        if code not in MATERIAL_REASONS:
            continue
        raw = row.get("raw")
        raw = raw if isinstance(raw, dict) else {}
        lot = raw.get("to_lot") or ""
        if not lot:
            continue
        reasons[lot].add(code)
        lines[lot].append(row.get("line", 0))
    return reasons, lines


def _declared(issues, lot):
    """异常批按原始行重算申报口径：投料、产出、损耗（坏数量按 0 计并标记）。"""
    intake = ZERO
    output = ZERO
    loss = ZERO
    unparsable = 0
    seen_output = set()
    for row in issues:
        raw = row.get("raw")
        raw = raw if isinstance(raw, dict) else {}
        if raw.get("to_lot") != lot:
            continue
        qty = _q(raw.get("quantity"))
        if qty is None:
            unparsable += 1
        else:
            intake += qty
        key = (raw.get("output_kg"), raw.get("loss_kg"))
        if key not in seen_output:
            seen_output.add(key)
            out = _q(raw.get("output_kg"))
            los = _q(raw.get("loss_kg"))
            if out is not None:
                output += out
            if los is not None:
                loss += los
    return intake, output, loss, unparsable


def settle(edges, issues, excluded):
    """班次结账：逐批把结存拆成 待转在制 / 完工 / 库存，并把真实异常单列。

    有效批恒等式：投入 = 产出 + 损耗；产出 = 已用 + 结存（结存 >= 0）。
    待转在制只承认"中间批且账面守恒且未隔离"的结存，短缺/错投走异常批，不混入在制。
    """
    used = defaultdict(lambda: ZERO)
    groups = defaultdict(list)
    for row in edges:
        used[row["from_lot"]] += Decimal(row["kg"])
        groups[row["to_lot"]].append(row)

    issue_reasons, issue_lines = _material_issues(issues)

    # 真实异常批：凡带物料异常原因的批，一律按原始行重算申报口径，
    # 绝不复用有效账（隔离级联会使有效边为零，否则会算出虚假短缺/在制）。
    abnormal_lots = sorted(set(issue_reasons))
    normal_lots = [lot for lot in sorted(groups) if lot not in issue_reasons]

    accounts = []
    for lot in normal_lots:
        rows = groups[lot]
        intake = sum((Decimal(r["kg"]) for r in rows), ZERO)
        output = Decimal(rows[0]["output_kg"])
        loss = Decimal(rows[0]["loss_kg"])
        consumed = used[lot]
        balance_qty = output - consumed
        kind = _kind(lot)
        # 有效边批已通过守恒与超领校验；这里再守一次，防止负结存被当在制
        valid = intake == output + loss and balance_qty >= 0
        if not valid:
            # 理论上不会到这里（异常已在隔离阶段拦截），防御性兜底
            abnormal_lots.append(lot)
            issue_reasons.setdefault(lot, set()).add("MASS_GAP")
            continue
        if kind == "wip" and balance_qty > 0:
            state = "pending_transfer"          # 正常跨班在制，下一班领用
        elif balance_qty == 0:
            state = "closed"                    # 当场用完/领完，零结存
        elif kind == "finished":
            state = "finished_goods"            # 成品结存：完工待入库，不是在制
        else:
            state = "raw_stock"                 # 原料结存：库存，不是在制
        pending = balance_qty if state == "pending_transfer" else ZERO
        finished = balance_qty if state == "finished_goods" else ZERO
        stock = balance_qty if state == "raw_stock" else ZERO
        basis = ("投入%s = 产出%s + 损耗%s；产出%s = 已用%s + 结存%s"
                 % (intake, output, loss, output, consumed, balance_qty))
        accounts.append({
            "lot": lot, "kind": kind, "state": state,
            "input_kg": str(intake), "output_kg": str(output), "loss_kg": str(loss),
            "used_kg": str(consumed), "balance_kg": str(balance_qty),
            "pending_transfer_kg": str(pending), "finished_kg": str(finished),
            "raw_stock_kg": str(stock), "shortage_kg": "0",
            "reasons": [], "reason_text": [],
            "lines": sorted(r["line"] for r in rows),
            "basis": basis,
        })

    # 异常批逐批单列：短缺只对 MASS_GAP 有数量意义，其余原因给原因码不给误导性差额
    abnormal_accounts = []
    for lot in sorted(set(abnormal_lots)):
        # 只按该批的隔离行重算；VOID/SUPERSEDED 等排除行不属物料异常，不得并入
        lot_issues = [r for r in issues
                      if isinstance(r.get("raw"), dict) and r["raw"].get("to_lot") == lot]
        intake, output, loss, unparsable = _declared(lot_issues, lot)
        consumed = used.get(lot, ZERO)
        reason_codes = sorted(issue_reasons.get(lot, set()))
        if "MASS_GAP" in reason_codes:
            shortage = intake - output - loss
        else:
            shortage = ZERO
        basis = ("隔离批按原始行重算：申报投入%s、产出%s、损耗%s；原因 %s"
                 % (intake, output, loss, "/".join(reason_codes) or "无"))
        abnormal_accounts.append({
            "lot": lot, "kind": _kind(lot), "state": "abnormal",
            "input_kg": str(intake), "output_kg": str(output), "loss_kg": str(loss),
            "used_kg": str(consumed),
            "balance_kg": str(max(ZERO, output - consumed)),
            "pending_transfer_kg": "0", "finished_kg": "0", "raw_stock_kg": "0",
            "shortage_kg": str(shortage),
            "reasons": reason_codes,
            "reason_text": [REASONS.get(c, c) for c in reason_codes],
            "lines": sorted(set(issue_lines.get(lot, []))),
            "unparsable_lines": unparsable,
            "basis": basis,
        })

    all_accounts = sorted(accounts + abnormal_accounts, key=lambda a: a["lot"])
    summary = _summary(all_accounts)
    return {"accounts": all_accounts, "summary": summary}


def _summary(accounts):
    def total(field):
        return str(sum((Decimal(a[field]) for a in accounts), ZERO))

    pending = [a for a in accounts if a["state"] == "pending_transfer"]
    abnormal = [a for a in accounts if a["state"] == "abnormal"]
    return {
        "lot_total": len(accounts),
        "pending_lots": len(pending),
        "abnormal_lots": len(abnormal),
        "used_kg": total("used_kg"),
        "balance_kg": total("balance_kg"),
        "pending_transfer_kg": total("pending_transfer_kg"),
        "finished_kg": total("finished_kg"),
        "raw_stock_kg": total("raw_stock_kg"),
        "shortage_kg": total("shortage_kg"),
        "shift_clean": not abnormal,
    }
