#!/usr/bin/env python3
"""Offline S1-S22 and compatibility checks for deploy Spot safety."""

from __future__ import annotations

import argparse
from pathlib import Path

from deploy_spot_safety import (
    SPOT_CRITICAL_FILES,
    check_spot_runtime_compatibility,
    evaluate_pre_cutover_safety,
)


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
        raise AssertionError(f"C1 alert-only delta should be compatible: {compatibility}")
    print("[PASS] C1 alert-only runtime delta is compatible with open Spot")

    critical = candidate / "trading/longs.py"
    critical.write_text("CONTRACT = 'changed'\n", encoding="utf-8")
    compatibility = check_spot_runtime_compatibility(current, candidate)
    if compatibility["compatible"] or "trading/longs.py" not in compatibility["changed_critical_paths"]:
        raise AssertionError(f"C2 critical lifecycle delta should block: {compatibility}")
    print("[PASS] C2 critical Spot lifecycle delta blocks open Spot")


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
