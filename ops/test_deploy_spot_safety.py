#!/usr/bin/env python3
"""Offline S1-S22 and compatibility checks for deploy Spot safety."""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

from deploy_spot_safety import (
    FINAL_SPOT_CRITICAL_FILES,
    PREVENTIVE_SPOT_CLOSE_PATH,
    SPOT_CRITICAL_FILES,
    check_spot_runtime_compatibility,
    evaluate_pre_cutover_safety,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_COMMIT = "44fe2268deae2dd8c2d0ec05888ca0c1d82d5133"
CANDIDATE_COMMIT = "2fb2f5a7dd5afa5735ebb0492b0daab770f566ad"
V16_GATE_COMMIT = "2b210e7"
V17_FINAL_COMMIT = "a242acb9b747b15cecdcd3fb6b52c1e761451b50"


def _position(symbol="ETHUSDT", quantity="1", order_list_id=101, trade_id=None):
    return {
        "id": trade_id or f"long_{symbol}_fixture",
        "direction": "long",
        "symbol": symbol,
        "quantity": quantity,
        "oco_order_list_id": str(order_list_id),
    }


def _orders(symbol="ETHUSDT", quantity="1", order_list_id=101):
    common = {
        "symbol": symbol,
        "orderListId": order_list_id,
        "status": "NEW",
        "side": "SELL",
        "origQty": quantity,
    }
    return [
        {**common, "orderId": order_list_id * 10 + 1, "type": "LIMIT_MAKER"},
        {**common, "orderId": order_list_id * 10 + 2, "type": "STOP_LOSS_LIMIT"},
    ]


def _account(*balances):
    return {
        "balances": [
            {"asset": asset, "free": str(free), "locked": str(locked)}
            for asset, free, locked in balances
        ]
    }


def _reconciliation():
    return {
        "managed_count": 0,
        "orphan_count": 0,
        "unmanaged_count": 0,
        "unprotected_count": 0,
        "desynced_count": 0,
        "aligned": True,
        "status": "ALINEADO",
    }


def _base(current_root, candidate_root):
    return {
        "local_state": {"positions": []},
        "bot_state": {"positions": {"short": {"reconciliation": _reconciliation()}}},
        "exchange_positions": [],
        "futures_orders": [],
        "spot_orders": [],
        "spot_account": _account(),
        "spot_filters": {},
        "current_root": current_root,
        "candidate_root": candidate_root,
    }


def _managed_case(current_root, candidate_root, *, two=False):
    case = _base(current_root, candidate_root)
    positions = [_position()]
    orders = _orders()
    balances = [("ETH", "0", "1")]
    filters = {"ETHUSDT": {"step_size": "0.001"}}
    if two:
        positions.append(_position("BTCUSDT", "0.002", 202))
        orders.extend(_orders("BTCUSDT", "0.002", 202))
        balances.append(("BTC", "0", "0.002"))
        filters["BTCUSDT"] = {"step_size": "0.00001"}
    case.update(
        local_state={"positions": positions},
        spot_orders=orders,
        spot_account=_account(*balances),
        spot_filters=filters,
    )
    return case


def _write_runtime(root, *, alert_variant=False):
    for relative in SPOT_CRITICAL_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("CONTRACT = 'stable'\n", encoding="utf-8")
    cycle = root / "trading/orchestration/cycle_runner.py"
    cycle.parent.mkdir(parents=True, exist_ok=True)
    if alert_variant:
        cycle.write_text(
            "import utils\n"
            "PREVENTIVE_BTC_RISE_CLOSE_SHORTS_EVENT = 'rise'\n"
            "PREVENTIVE_BTC_FALL_CLOSE_LONGS_EVENT = 'fall'\n"
            "def sync_preventive_telegram_alert(a, b, c):\n    return None\n"
            "class CycleRunner:\n"
            "    def run(self):\n"
            "        sync_preventive_telegram_alert(False, False, '')\n"
            "        return 1\n",
            encoding="utf-8",
        )
    else:
        cycle.write_text(
            "import utils\nclass CycleRunner:\n    def run(self):\n"
            "        utils.send_alert(close_reason)\n        return 1\n",
            encoding="utf-8",
        )
    utils = root / "trading/utils.py"
    if alert_variant:
        utils.write_text(
            "def keep():\n    return 1\n"
            "def send_alert(message, event_key=None):\n    return event_key\n"
            "def rearm_telegram_alert_event(event_key):\n    return True\n",
            encoding="utf-8",
        )
    else:
        utils.write_text(
            "def keep():\n    return 1\ndef send_alert(message):\n    return None\n",
            encoding="utf-8",
        )


def _materialize_commit_runtime(root, commit):
    paths = dict.fromkeys((
        *SPOT_CRITICAL_FILES,
        *FINAL_SPOT_CRITICAL_FILES,
        "trading/orchestration/cycle_runner.py",
        "trading/utils.py",
        PREVENTIVE_SPOT_CLOSE_PATH,
        "VERSION",
    ))
    for relative in paths:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "show", f"{commit}:{relative}"],
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            if relative in FINAL_SPOT_CRITICAL_FILES and commit != V17_FINAL_COMMIT:
                continue
            raise AssertionError(
                f"cannot materialize {commit}:{relative}: "
                f"{result.stderr.decode('utf-8', errors='replace')}"
            )
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(result.stdout)


def _copy_runtime(source, target):
    shutil.copytree(source, target)
    return target


def _append_semantic_change(path, label):
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n{label} = True\n")


def _expect_incompatible(label, current, candidate, expected_path=None):
    result = check_spot_runtime_compatibility(current, candidate)
    if result["compatible"]:
        raise AssertionError(f"{label} unexpectedly compatible: {result}")
    if expected_path and expected_path not in result["changed_critical_paths"]:
        raise AssertionError(f"{label} missing changed path {expected_path}: {result}")


def _replace_once(path, before, after):
    source = path.read_text(encoding="utf-8")
    if source.count(before) != 1:
        raise AssertionError(f"mutation marker not unique in {path}: {before!r}")
    path.write_text(source.replace(before, after, 1), encoding="utf-8")


def _run_v17_gate_contracts(temp_root, real_current):
    v16 = temp_root / "g17-v16"
    final = temp_root / "g17-final"
    _materialize_commit_runtime(v16, V16_GATE_COMMIT)
    _materialize_commit_runtime(final, V17_FINAL_COMMIT)

    v16_result = check_spot_runtime_compatibility(real_current, v16)
    if not v16_result["compatible"]:
        raise AssertionError(f"G17-P1 v1.6 regression: {v16_result}")
    print("[PASS] G17-P1 direct v1.5 to audited v1.6 remains compatible")

    result = check_spot_runtime_compatibility(real_current, final)
    if (not result["compatible"] or result["changed_critical_paths"] or result["errors"]
            or result.get("audited_transitions") != [
                "v1.6_preventive_spot", "v1.7_spot_quantity_and_recovery",
            ]):
        raise AssertionError(f"G17-P2 final direct transition: {result}")
    print("[PASS] G17-P2 direct v1.5 to final v1.7 is compatible")
    for number, contract in enumerate((
        "A_preventive_spot", "B_canonical_quantity", "C_partial_safety",
        "E_entry_recovery", "D_lifecycle_lock",
    ), start=3):
        if result["contracts"].get(contract) is not True:
            raise AssertionError(f"G17-P{number} missing {contract}: {result}")
        print(f"[PASS] G17-P{number} {contract} is structurally certified")

    formatting = _copy_runtime(final, temp_root / "g17-formatting")
    _replace_once(
        formatting / "trading/longs.py",
        "import entry_spot_recovery\n",
        "import    entry_spot_recovery  # audited formatting\n",
    )
    with (formatting / "trading/partial_spot_long.py").open("a", encoding="utf-8") as handle:
        handle.write("\n# formatting-only comment\n")
    if not check_spot_runtime_compatibility(real_current, formatting)["compatible"]:
        raise AssertionError("G17-P8 AST-normalized formatting changed compatibility")
    print("[PASS] G17-P8 comments and formatting preserve AST contracts")

    equivalent = _copy_runtime(final, temp_root / "g17-equivalent-tree")
    if (equivalent / ".git").exists():
        raise AssertionError("G17-P9 fixture unexpectedly includes Git history")
    if not check_spot_runtime_compatibility(real_current, equivalent)["compatible"]:
        raise AssertionError("G17-P9 equivalent final tree was rejected")
    print("[PASS] G17-P9 equivalent final tree is independent of commit ordering")

    changes = (
        (1, "trading/longs.py", "qty_payload = format_decimal_quantity(qty_decimal)",
         "qty_payload = str(qty_decimal)"),
        (2, "trading/longs.py", "qty_payload = format_decimal_quantity(qty_decimal)",
         "qty_payload = str(float(qty_decimal))"),
        (3, "trading/quantity_integrity.py", 'text = format(quantity, "f")',
         'text = str(quantity)'),
        (4, "trading/partial_spot_long.py", "if snapshot_error:",
         "if False and snapshot_error:"),
        (5, "trading/partial_spot_long.py", "excess_inventory = before['total'] - managed",
         "managed = before['total']\n    excess_inventory = before['total'] - managed"),
        (6, "trading/orchestration/cycle_runner.py",
         "if is_spot_long_recovery_pending(pos):\n                self._reconcile_pending_spot_long",
         "if False and is_spot_long_recovery_pending(pos):\n                self._reconcile_pending_spot_long"),
        (7, "trading/longs.py",
         "if is_spot_long_recovery_pending(pos):\n        return 'deferred_recovery_pending', None, 0\n    sym   = pos['symbol']\n    entry = pos['entry_price']",
         "if False and is_spot_long_recovery_pending(pos):\n        return 'deferred_recovery_pending', None, 0\n    sym   = pos['symbol']\n    entry = pos['entry_price']"),
        (8, "trading/preventive_spot_close.py", "if is_spot_long_recovery_pending(pos):",
         "if False and is_spot_long_recovery_pending(pos):"),
        (9, "trading/sl_guardian.py", "if is_spot_long_recovery_pending(pos):",
         "if False and is_spot_long_recovery_pending(pos):"),
        (11, "trading/orchestration/cycle_runner.py",
         "entry_spot_recovery.reconcile_pending_entry_spot_long(self.binance, pos)",
         "partial_spot_long.reconcile_pending_partial_long_spot(self.binance, pos)"),
        (12, "trading/orchestration/cycle_runner.py",
         "result = {'status': f'UNKNOWN_RECOVERY_TYPE:{kind}', 'confirmed_execution': False}",
         "result = entry_spot_recovery.reconcile_pending_entry_spot_long(self.binance, pos)"),
        (13, "trading/orchestration/audit_pipeline.py",
         "if is_spot_long_recovery_pending(position):",
         "if False and is_spot_long_recovery_pending(position):"),
        (14, "trading/partial_spot_long.py", "order = _create_order(client, sell_payload)",
         "order = _create_order(client, sell_payload)\n        _create_order(client, sell_payload)"),
        (15, "trading/orchestration/cycle_runner.py", "import preventive_spot_close\n", ""),
        (16, "trading/preventive_spot_close.py",
         "'confirmed_close': status in FINAL_CLOSE_STATUSES,",
         "'confirmed_close': True,"),
        (17, "trading/orchestration/cycle_runner.py", "state = utils.load_state()",
         "state = utils.load_state()\n        state['strategy_score_override'] = 1"),
        (18, "trading/quantity_integrity.py", "rounding=ROUND_DOWN) * step_size",
         "rounding=__import__('decimal').ROUND_UP) * step_size"),
        (22, "trading/auto_loop.py",
         "'symbol': symbol, 'side': 'SELL', 'type': 'MARKET',\n            'quantity': format_decimal_quantity(qty),",
         "'symbol': symbol, 'side': 'SELL', 'type': 'MARKET',\n            'quantity': str(float(qty)),"),
    )
    for number, relative, before, after in changes:
        candidate = _copy_runtime(final, temp_root / f"g17-n{number}")
        _replace_once(candidate / relative, before, after)
        _expect_incompatible(f"G17-N{number}", real_current, candidate, relative)
        print(f"[PASS] G17-N{number} changed {relative} is incompatible")

    special = {
        10: ("trading/entry_spot_recovery.py", "remove"),
        19: ("trading/partial_spot_long.py", "parse"),
        20: ("trading/quantity_integrity.py", "remove"),
        21: ("trading/orchestration/unaudited_spot_builder.py", "add"),
    }
    for number, (relative, action) in special.items():
        candidate = _copy_runtime(final, temp_root / f"g17-n{number}")
        path = candidate / relative
        if action == "remove":
            path.unlink()
        elif action == "parse":
            path.write_text("def invalid(:\n", encoding="utf-8")
        else:
            path.write_text("def send_spot_order():\n    return True\n", encoding="utf-8")
        outcome = check_spot_runtime_compatibility(real_current, candidate)
        if outcome["compatible"] or not (
            relative in outcome["changed_critical_paths"]
            or any(relative in error for error in outcome["errors"])
        ):
            raise AssertionError(f"G17-N{number} did not fail closed: {outcome}")
        print(f"[PASS] G17-N{number} {action} {relative} is incompatible")


def _expect(number, label, case, expected, status=None):
    result = evaluate_pre_cutover_safety(**case)
    if result["safe"] is not expected:
        raise AssertionError(f"S{number} {label}: safe={result['safe']} expected={expected}: {result}")
    statuses = {record["status"] for record in result["spot"]["records"]}
    if status is not None and status not in statuses:
        raise AssertionError(f"S{number} {label}: missing {status}: {statuses}")
    print(f"[PASS] S{number} {label}")


def run(temp_root):
    current = temp_root / "current"
    candidate = temp_root / "candidate"
    _write_runtime(current)
    _write_runtime(candidate, alert_variant=True)

    _expect(1, "zero positions and orders pass", _base(current, candidate), True)
    _expect(2, "one managed Spot with complete OCO passes", _managed_case(current, candidate), True)
    _expect(3, "two managed Spot positions with complete OCO pass", _managed_case(current, candidate, two=True), True)

    case = _managed_case(current, candidate)
    case["local_state"]["positions"][0]["oco_order_list_id"] = ""
    case["spot_orders"] = []
    _expect(4, "managed Spot without OCO blocks", case, False, "UNSAFE_SPOT_NO_OCO")

    case = _managed_case(current, candidate)
    case["spot_orders"] = case["spot_orders"][:1]
    _expect(5, "managed Spot with TP only blocks", case, False, "UNSAFE_SPOT_OCO_INCOMPLETE")

    case = _managed_case(current, candidate)
    case["spot_orders"] = case["spot_orders"][1:]
    _expect(6, "managed Spot with stop only blocks", case, False, "UNSAFE_SPOT_OCO_INCOMPLETE")

    case = _managed_case(current, candidate)
    case["local_state"]["positions"][0]["oco_order_list_id"] = "999"
    _expect(7, "orderListId mismatch blocks", case, False, "UNSAFE_SPOT_ORDERLIST_MISMATCH")

    case = _managed_case(current, candidate)
    for order in case["spot_orders"]:
        order["origQty"] = "0.9"
    _expect(8, "under-protected OCO quantity blocks", case, False, "UNSAFE_SPOT_QTY_MISMATCH")

    case = _managed_case(current, candidate)
    case["spot_account"] = _account(("ETH", "0", "0.9"))
    _expect(9, "insufficient exchange balance blocks", case, False, "UNSAFE_SPOT_BALANCE_MISMATCH")

    case = _managed_case(current, candidate)
    for order in case["spot_orders"]:
        order["symbol"] = "BTCUSDT"
    _expect(10, "OCO symbol mismatch blocks", case, False, "UNSAFE_SPOT_SYMBOL_MISMATCH")

    case = _managed_case(current, candidate)
    case["spot_orders"][0]["side"] = "BUY"
    _expect(11, "unexpected BUY OCO leg blocks", case, False, "UNSAFE_SPOT_SIDE_MISMATCH")

    case = _managed_case(current, candidate)
    case["spot_orders"].append(
        {"symbol": "SOLUSDT", "orderId": 999, "orderListId": -1, "status": "NEW", "side": "SELL", "type": "LIMIT", "origQty": "1"}
    )
    _expect(12, "unassociated Spot order blocks", case, False, "UNSAFE_SPOT_UNKNOWN_ORDER")

    case = _base(current, candidate)
    position = _position()
    position["direction"] = "spot"
    case["local_state"] = {"positions": [position]}
    _expect(13, "unmanaged Spot position blocks", case, False, "UNSAFE_SPOT_UNMANAGED_POSITION")

    case = _managed_case(current, candidate)
    del case["local_state"]["positions"][0]["id"]
    _expect(14, "incomplete local position blocks", case, False, "UNSAFE_SPOT_STATE_INCOMPLETE")

    case = _managed_case(current, candidate)
    case["spot_orders"] = None
    _expect(15, "unknown order fetch blocks", case, False, "UNSAFE_SPOT_FETCH_ERROR")

    case = _base(current, candidate)
    case["exchange_positions"] = [{"symbol": "ETHUSDT", "positionAmt": "0.1"}]
    _expect(16, "any real Futures position blocks", case, False)

    case = _base(current, candidate)
    case["futures_orders"] = [{"symbol": "ETHUSDT", "orderId": 1}]
    _expect(17, "any Futures open order blocks", case, False)

    case = _base(current, candidate)
    reconciliation = case["bot_state"]["positions"]["short"]["reconciliation"]
    reconciliation.update(aligned=False, status="DESALINEADO")
    _expect(18, "unaligned Futures reconciliation blocks", case, False)

    case = _managed_case(current, candidate)
    case["spot_account"] = _account(("ETH", "0.001", "1"))
    _expect(19, "exchange dust within existing step tolerance passes", case, True)

    case = _managed_case(current, candidate)
    for order in case["spot_orders"]:
        order["origQty"] = "1.00001"
    _expect(20, "OCO quantity outside protection tolerance blocks", case, False, "UNSAFE_SPOT_QTY_MISMATCH")

    case = _managed_case(current, candidate, two=True)
    case["spot_orders"][-1]["status"] = "CANCELED"
    _expect(21, "one unsafe position blocks the complete set", case, False, "UNSAFE_SPOT_OCO_INACTIVE")

    case = _managed_case(current, candidate)
    for order in case["spot_orders"]:
        order["status"] = "CANCELED"
    _expect(22, "closed or canceled OCO blocks", case, False, "UNSAFE_SPOT_OCO_INACTIVE")

    compatibility = check_spot_runtime_compatibility(current, candidate)
    if not compatibility["compatible"]:
        raise AssertionError(f"C-prev1 alert-only delta should be compatible: {compatibility}")
    print("[PASS] C-prev1 alert-only runtime delta is compatible with open Spot")

    critical = candidate / "trading/longs.py"
    critical.write_text("CONTRACT = 'changed'\n", encoding="utf-8")
    compatibility = check_spot_runtime_compatibility(current, candidate)
    if compatibility["compatible"] or "trading/longs.py" not in compatibility["changed_critical_paths"]:
        raise AssertionError(f"C-prev2 critical lifecycle delta should block: {compatibility}")
    print("[PASS] C-prev2 critical Spot lifecycle delta blocks open Spot")

    real_current = temp_root / "real-current"
    real_candidate = temp_root / "real-candidate"
    _materialize_commit_runtime(real_current, PRODUCTION_COMMIT)
    _materialize_commit_runtime(real_candidate, CANDIDATE_COMMIT)

    compatibility = check_spot_runtime_compatibility(real_current, real_candidate)
    if not compatibility["compatible"]:
        raise AssertionError(f"C1 production-to-candidate transition should pass: {compatibility}")
    print("[PASS] C1 44fe226 to 2fb2f5a audited Spot transition is compatible")

    equivalent = _copy_runtime(real_candidate, temp_root / "c2-equivalent")
    compatibility = check_spot_runtime_compatibility(real_current, equivalent)
    if not compatibility["compatible"]:
        raise AssertionError(f"C2 equivalent audited transition should pass: {compatibility}")
    print("[PASS] C2 equivalent preventive Spot LONG delta is compatible")

    extra_cycle = _copy_runtime(real_candidate, temp_root / "c3-cycle")
    _append_semantic_change(
        extra_cycle / "trading/orchestration/cycle_runner.py",
        "UNRELATED_CYCLE_CHANGE",
    )
    _expect_incompatible(
        "C3 cycle delta", real_current, extra_cycle,
        "trading/orchestration/cycle_runner.py",
    )
    extra_helper = _copy_runtime(real_candidate, temp_root / "c3-helper")
    _append_semantic_change(
        extra_helper / PREVENTIVE_SPOT_CLOSE_PATH,
        "UNRELATED_HELPER_CHANGE",
    )
    _expect_incompatible(
        "C3 helper delta", real_current, extra_helper, PREVENTIVE_SPOT_CLOSE_PATH,
    )
    print("[PASS] C3 additional cycle/helper semantics remain incompatible")

    strategy_change = _copy_runtime(real_candidate, temp_root / "c4-strategy")
    cycle_path = strategy_change / "trading/orchestration/cycle_runner.py"
    cycle_source = cycle_path.read_text(encoding="utf-8")
    marker = "        state = utils.load_state()\n"
    injected = (
        marker
        + "        state['strategy_score_override'] = 1\n"
        + "        state['sizing_override'] = 1\n"
    )
    if cycle_source.count(marker) != 1:
        raise AssertionError("C4 cycle fixture marker is not unique")
    cycle_path.write_text(cycle_source.replace(marker, injected), encoding="utf-8")
    _expect_incompatible(
        "C4 strategy/sizing delta", real_current, strategy_change,
        "trading/orchestration/cycle_runner.py",
    )
    print("[PASS] C4 strategy/scoring/sizing cycle changes remain incompatible")

    for index, relative in enumerate((
        "trading/orchestration/position_lifecycle.py",
        "trading/longs.py",
        "trading/sl_guardian.py",
    )):
        changed_runtime = _copy_runtime(real_candidate, temp_root / f"c5-{index}")
        _append_semantic_change(changed_runtime / relative, "UNAUTHORIZED_LIFECYCLE_CHANGE")
        _expect_incompatible(f"C5 {relative}", real_current, changed_runtime, relative)
    print("[PASS] C5 lifecycle/OCO/Guardian changes remain incompatible")

    binance_change = _copy_runtime(real_candidate, temp_root / "c6-binance")
    _append_semantic_change(
        binance_change / "trading/binance_client.py",
        "UNAUTHORIZED_PAYLOAD_CHANGE",
    )
    _expect_incompatible(
        "C6 BinanceClient delta", real_current, binance_change, "trading/binance_client.py",
    )
    print("[PASS] C6 BinanceClient/payload changes remain incompatible")

    formatting = _copy_runtime(real_candidate, temp_root / "c7-formatting")
    formatting_cycle = formatting / "trading/orchestration/cycle_runner.py"
    formatting_cycle.write_text(
        formatting_cycle.read_text(encoding="utf-8").replace(
            "import preventive_spot_close\n",
            "import    preventive_spot_close  # audited formatting-only change\n",
            1,
        ) + "\n# trailing formatting-only comment\n",
        encoding="utf-8",
    )
    with (formatting / PREVENTIVE_SPOT_CLOSE_PATH).open("a", encoding="utf-8") as handle:
        handle.write("\n# formatting-only helper comment\n")
    compatibility = check_spot_runtime_compatibility(real_current, formatting)
    if not compatibility["compatible"]:
        raise AssertionError(f"C7 formatting-only change should pass: {compatibility}")
    print("[PASS] C7 comments and formatting are normalized deterministically")

    incomplete = _copy_runtime(real_candidate, temp_root / "c8-incomplete")
    (incomplete / PREVENTIVE_SPOT_CLOSE_PATH).unlink()
    missing_result = check_spot_runtime_compatibility(real_current, incomplete)
    if missing_result["compatible"] or not any(
        error == f"missing:{PREVENTIVE_SPOT_CLOSE_PATH}:candidate"
        for error in missing_result["errors"]
    ):
        raise AssertionError(f"C8 missing helper did not fail closed: {missing_result}")
    malformed = _copy_runtime(real_candidate, temp_root / "c8-malformed")
    (malformed / "trading/orchestration/cycle_runner.py").write_text(
        "def invalid(:\n", encoding="utf-8"
    )
    malformed_result = check_spot_runtime_compatibility(real_current, malformed)
    if malformed_result["compatible"] or not malformed_result["errors"]:
        raise AssertionError(f"C8 parse failure did not fail closed: {malformed_result}")
    print("[PASS] C8 incomplete evidence and parser failures fail closed")

    zero_spot = _base(real_current, extra_cycle)
    zero_result = evaluate_pre_cutover_safety(**zero_spot)
    if not zero_result["safe"] or zero_result["compatibility"]["status"] != "SPOT_RUNTIME_COMPATIBILITY_NOT_REQUIRED":
        raise AssertionError(f"C9 zero-Spot policy regressed: {zero_result}")
    print("[PASS] C9 zero Spot positions preserve compatibility-not-required policy")

    futures_cases = []
    futures_position = _base(real_current, real_candidate)
    futures_position["exchange_positions"] = [{"symbol": "ETHUSDT", "positionAmt": "0.1"}]
    futures_cases.append(futures_position)
    futures_order = _base(real_current, real_candidate)
    futures_order["futures_orders"] = [{"symbol": "ETHUSDT", "orderId": 1}]
    futures_cases.append(futures_order)
    futures_reconciliation = _base(real_current, real_candidate)
    futures_reconciliation["bot_state"]["positions"]["short"]["reconciliation"].update(
        aligned=False, status="DESALINEADO"
    )
    futures_cases.append(futures_reconciliation)
    for case in futures_cases:
        result = evaluate_pre_cutover_safety(**case)
        if result["safe"]:
            raise AssertionError(f"C10 unsafe Futures evidence passed: {result}")
    print("[PASS] C10 Futures position/order/reconciliation policy remains restrictive")

    _run_v17_gate_contracts(temp_root, real_current)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--temp-root", type=Path, required=True)
    args = parser.parse_args()
    if args.temp_root.exists():
        raise SystemExit(f"temporary fixture root already exists: {args.temp_root}")
    args.temp_root.mkdir(parents=True)
    run(args.temp_root)


if __name__ == "__main__":
    main()
