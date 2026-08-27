#!/usr/bin/env python3
"""Pure, read-only safety classification for immutable deploy cutovers.

This module never calls Binance and never writes state.  The deploy script owns
the GET-only observations and passes immutable values here for classification.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path


SAFE_MANAGED_SPOT = "SAFE_MANAGED_SPOT"
SAFE_NO_SPOT = "SAFE_NO_SPOT"
ACTIVE_SPOT_ORDER_STATUSES = frozenset({"NEW", "PENDING_NEW"})
SPOT_STOP_TYPES = frozenset({"STOP_LOSS_LIMIT"})
SPOT_TAKE_PROFIT_TYPES = frozenset({"LIMIT_MAKER"})

# Existing repository contracts, not deploy-specific thresholds:
# - audit_pipeline.evaluate_spot_position_reconciliation uses max(step, 1e-12)
#   for managed-vs-observed Spot quantity alignment.
# - pre_entry_safety_gate.DEFAULT_PROTECTION_TOLERANCE is 1e-6.
MIN_QUANTITY_TOLERANCE = Decimal("1e-12")
DEFAULT_PROTECTION_TOLERANCE = Decimal("1e-6")

SPOT_CRITICAL_FILES = (
    "trading/atomic_persistence.py",
    "trading/binance_client.py",
    "trading/bot.py",
    "trading/config.py",
    "trading/config_loader.py",
    "trading/history.py",
    "trading/longs.py",
    "trading/market.py",
    "trading/orchestration/audit_pipeline.py",
    "trading/orchestration/position_lifecycle.py",
    "trading/residuals.py",
    "trading/sl_guardian.py",
)

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,30}USDT$")
_IGNORED_CYCLE_FUNCTIONS = frozenset({"sync_preventive_telegram_alert"})
_IGNORED_CYCLE_CONSTANTS = frozenset(
    {
        "PREVENTIVE_BTC_RISE_CLOSE_SHORTS_EVENT",
        "PREVENTIVE_BTC_FALL_CLOSE_LONGS_EVENT",
    }
)
_IGNORED_UTIL_FUNCTIONS = frozenset({"send_alert", "rearm_telegram_alert_event"})


class SafetyEvidenceError(ValueError):
    """Raised when read-only evidence cannot be parsed unambiguously."""


def _decimal(value, field):
    if isinstance(value, bool) or value is None or str(value).strip() == "":
        raise SafetyEvidenceError(f"invalid {field}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SafetyEvidenceError(f"invalid {field}") from exc
    if not parsed.is_finite():
        raise SafetyEvidenceError(f"invalid {field}")
    return parsed


def _order_list_id(value):
    if isinstance(value, bool):
        raise SafetyEvidenceError("invalid orderListId")
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise SafetyEvidenceError("invalid orderListId") from exc
    if parsed <= 0:
        raise SafetyEvidenceError("invalid orderListId")
    return parsed


def _unsafe_record(trade_id="", symbol="", status="UNSAFE_SPOT_STATE_INCOMPLETE"):
    return {
        "trade_id": str(trade_id or ""),
        "symbol": str(symbol or "").upper(),
        "managed": False,
        "balance_aligned": False,
        "oco": False,
        "qty_aligned": False,
        "status": status,
    }


def _balance_by_asset(account):
    if not isinstance(account, dict) or not isinstance(account.get("balances"), list):
        raise SafetyEvidenceError("invalid Spot account")
    result = {}
    for row in account["balances"]:
        if not isinstance(row, dict):
            raise SafetyEvidenceError("invalid Spot balance")
        asset = str(row.get("asset") or "").strip().upper()
        if not asset or asset in result:
            raise SafetyEvidenceError("ambiguous Spot balance")
        free = _decimal(row.get("free"), "free balance")
        locked = _decimal(row.get("locked"), "locked balance")
        if free < 0 or locked < 0:
            raise SafetyEvidenceError("negative Spot balance")
        result[asset] = free + locked
    return result


def classify_spot_deploy_safety(
    local_positions,
    spot_account,
    spot_orders,
    spot_filters,
    *,
    protection_tolerance=DEFAULT_PROTECTION_TOLERANCE,
):
    """Classify local Spot positions against fresh balance and OCO evidence."""
    if not isinstance(local_positions, list):
        record = _unsafe_record(status="UNSAFE_SPOT_STATE_INCOMPLETE")
        return {
            "safe": False,
            "positions_safe": False,
            "orders_safe": False,
            "records": [record],
            "unknown_orders": [],
        }
    if not isinstance(spot_orders, list):
        record = _unsafe_record(status="UNSAFE_SPOT_FETCH_ERROR")
        return {
            "safe": False,
            "positions_safe": False,
            "orders_safe": False,
            "records": [record],
            "unknown_orders": [],
        }
    if not isinstance(spot_filters, dict):
        record = _unsafe_record(status="UNSAFE_SPOT_FETCH_ERROR")
        return {
            "safe": False,
            "positions_safe": False,
            "orders_safe": False,
            "records": [record],
            "unknown_orders": [],
        }
    try:
        balances = _balance_by_asset(spot_account)
        tolerance = _decimal(protection_tolerance, "protection tolerance")
        if tolerance < 0:
            raise SafetyEvidenceError("negative protection tolerance")
        for order in spot_orders:
            if not isinstance(order, dict):
                raise SafetyEvidenceError("invalid Spot order")
    except SafetyEvidenceError:
        record = _unsafe_record(status="UNSAFE_SPOT_FETCH_ERROR")
        return {
            "safe": False,
            "positions_safe": False,
            "orders_safe": False,
            "records": [record],
            "unknown_orders": [],
        }

    records = []
    consumed_orders = set()
    seen_symbols = set()
    seen_order_lists = set()

    for position in local_positions:
        if not isinstance(position, dict):
            records.append(_unsafe_record(status="UNSAFE_SPOT_STATE_INCOMPLETE"))
            continue
        direction = str(position.get("direction") or "").strip().lower()
        if direction == "short":
            continue
        trade_id = str(position.get("id") or position.get("trade_id") or "").strip()
        symbol = str(position.get("symbol") or "").strip().upper()
        record = _unsafe_record(trade_id, symbol)
        if direction != "long":
            record["status"] = "UNSAFE_SPOT_UNMANAGED_POSITION"
            records.append(record)
            continue
        if not trade_id or not _SYMBOL_RE.fullmatch(symbol):
            record["status"] = "UNSAFE_SPOT_STATE_INCOMPLETE"
            records.append(record)
            continue
        try:
            managed_qty = _decimal(position.get("quantity"), "managed quantity")
            local_order_list = _order_list_id(position.get("oco_order_list_id"))
        except SafetyEvidenceError:
            record["status"] = (
                "UNSAFE_SPOT_NO_OCO"
                if not str(position.get("oco_order_list_id") or "").strip()
                else "UNSAFE_SPOT_STATE_INCOMPLETE"
            )
            records.append(record)
            continue
        if managed_qty <= 0:
            record["status"] = "UNSAFE_SPOT_STATE_INCOMPLETE"
            records.append(record)
            continue
        if symbol in seen_symbols or local_order_list in seen_order_lists:
            record["status"] = "UNSAFE_SPOT_STATE_INCOMPLETE"
            records.append(record)
            continue
        seen_symbols.add(symbol)
        seen_order_lists.add(local_order_list)
        record["managed"] = True

        filters = spot_filters.get(symbol)
        try:
            if not isinstance(filters, dict):
                raise SafetyEvidenceError("missing Spot filters")
            step = _decimal(filters.get("step_size"), "step size")
            if step <= 0:
                raise SafetyEvidenceError("invalid step size")
            asset = symbol[:-4]
            observed_qty = balances[asset]
        except (KeyError, SafetyEvidenceError):
            record["status"] = "UNSAFE_SPOT_FETCH_ERROR"
            records.append(record)
            continue
        balance_tolerance = max(step, MIN_QUANTITY_TOLERANCE)
        balance_delta = observed_qty - managed_qty
        if balance_delta < 0 or balance_delta > balance_tolerance:
            record["status"] = "UNSAFE_SPOT_BALANCE_MISMATCH"
            records.append(record)
            continue
        record["balance_aligned"] = True

        matching = []
        same_symbol = []
        for index, order in enumerate(spot_orders):
            order_symbol = str(order.get("symbol") or "").strip().upper()
            if order_symbol == symbol:
                same_symbol.append(index)
            try:
                observed_list = _order_list_id(order.get("orderListId"))
            except SafetyEvidenceError:
                continue
            if observed_list == local_order_list:
                matching.append(index)
        if not matching:
            record["status"] = (
                "UNSAFE_SPOT_ORDERLIST_MISMATCH" if same_symbol else "UNSAFE_SPOT_NO_OCO"
            )
            records.append(record)
            continue
        consumed_orders.update(matching)
        if len(matching) != 2:
            record["status"] = "UNSAFE_SPOT_OCO_INCOMPLETE"
            records.append(record)
            continue
        legs = [spot_orders[index] for index in matching]
        if any(str(leg.get("symbol") or "").strip().upper() != symbol for leg in legs):
            record["status"] = "UNSAFE_SPOT_SYMBOL_MISMATCH"
            records.append(record)
            continue
        if any(str(leg.get("side") or "").strip().upper() != "SELL" for leg in legs):
            record["status"] = "UNSAFE_SPOT_SIDE_MISMATCH"
            records.append(record)
            continue
        if any(str(leg.get("status") or "").strip().upper() not in ACTIVE_SPOT_ORDER_STATUSES for leg in legs):
            record["status"] = "UNSAFE_SPOT_OCO_INACTIVE"
            records.append(record)
            continue
        types = [str(leg.get("type") or "").strip().upper() for leg in legs]
        if sum(kind in SPOT_STOP_TYPES for kind in types) != 1 or sum(
            kind in SPOT_TAKE_PROFIT_TYPES for kind in types
        ) != 1:
            record["status"] = "UNSAFE_SPOT_OCO_INCOMPLETE"
            records.append(record)
            continue
        qty_tolerance = max(tolerance, managed_qty * tolerance)
        try:
            leg_quantities = [_decimal(leg.get("origQty"), "OCO quantity") for leg in legs]
        except SafetyEvidenceError:
            record["status"] = "UNSAFE_SPOT_QTY_MISMATCH"
            records.append(record)
            continue
        if any(qty <= 0 or abs(qty - managed_qty) > qty_tolerance for qty in leg_quantities):
            record["status"] = "UNSAFE_SPOT_QTY_MISMATCH"
            records.append(record)
            continue
        record.update(oco=True, qty_aligned=True, status=SAFE_MANAGED_SPOT)
        records.append(record)

    unknown_indexes = [index for index in range(len(spot_orders)) if index not in consumed_orders]
    unknown_orders = [
        {
            "symbol": str(spot_orders[index].get("symbol") or "").strip().upper(),
            "orderId": spot_orders[index].get("orderId"),
            "orderListId": spot_orders[index].get("orderListId"),
        }
        for index in unknown_indexes
    ]
    if unknown_orders:
        records.append(
            _unsafe_record(
                symbol=unknown_orders[0]["symbol"],
                status="UNSAFE_SPOT_UNKNOWN_ORDER",
            )
        )
    if not records:
        records.append(
            {
                "trade_id": "",
                "symbol": "",
                "managed": True,
                "balance_aligned": True,
                "oco": True,
                "qty_aligned": True,
                "status": SAFE_NO_SPOT,
            }
        )
    positions_safe = all(
        record["status"] in {SAFE_MANAGED_SPOT, SAFE_NO_SPOT}
        for record in records
        if record["status"] != "UNSAFE_SPOT_UNKNOWN_ORDER"
    )
    orders_safe = not unknown_orders and all(
        record["status"] in {SAFE_MANAGED_SPOT, SAFE_NO_SPOT} for record in records
    )
    return {
        "safe": positions_safe and orders_safe,
        "positions_safe": positions_safe,
        "orders_safe": orders_safe,
        "records": records,
        "unknown_orders": unknown_orders,
    }


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_ignored_cycle_call(statement):
    if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
        return False
    call = statement.value
    function = call.func
    if isinstance(function, ast.Name) and function.id in _IGNORED_CYCLE_FUNCTIONS:
        return True
    return (
        isinstance(function, ast.Attribute)
        and isinstance(function.value, ast.Name)
        and function.value.id == "utils"
        and function.attr == "send_alert"
        and len(call.args) == 1
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == "close_reason"
        and not call.keywords
    )


class _CycleAlertCallStripper(ast.NodeTransformer):
    def visit_Expr(self, node):
        if _is_ignored_cycle_call(node):
            return None
        return self.generic_visit(node)


def _normalized_ast(path, profile):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    normalized = copy.deepcopy(tree)
    body = []
    for node in normalized.body:
        if profile == "cycle_runner":
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in _IGNORED_CYCLE_FUNCTIONS:
                continue
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = {target.id for target in targets if isinstance(target, ast.Name)}
                if names and names <= _IGNORED_CYCLE_CONSTANTS:
                    continue
            if isinstance(node, ast.ClassDef) and node.name == "CycleRunner":
                for member in node.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and member.name == "run":
                        stripper = _CycleAlertCallStripper()
                        member.body = [
                            stripped
                            for item in member.body
                            if (stripped := stripper.visit(item)) is not None
                        ]
        elif profile == "utils":
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in _IGNORED_UTIL_FUNCTIONS:
                continue
        body.append(node)
    normalized.body = body
    return ast.dump(normalized, annotate_fields=True, include_attributes=False)


def check_spot_runtime_compatibility(current_root, candidate_root):
    """Fail closed when open-Spot lifecycle/runtime contracts differ."""
    current_root = Path(current_root)
    candidate_root = Path(candidate_root)
    changed = []
    errors = []
    for relative in SPOT_CRITICAL_FILES:
        current = current_root / relative
        candidate = candidate_root / relative
        if not current.is_file() or not candidate.is_file():
            errors.append(f"missing:{relative}")
            continue
        if _sha256(current) != _sha256(candidate):
            changed.append(relative)
    for relative, profile in (
        ("trading/orchestration/cycle_runner.py", "cycle_runner"),
        ("trading/utils.py", "utils"),
    ):
        current = current_root / relative
        candidate = candidate_root / relative
        if not current.is_file() or not candidate.is_file():
            errors.append(f"missing:{relative}")
            continue
        try:
            if _normalized_ast(current, profile) != _normalized_ast(candidate, profile):
                changed.append(relative)
        except (OSError, SyntaxError, UnicodeError) as exc:
            errors.append(f"parse:{relative}:{type(exc).__name__}")
    return {
        "compatible": not changed and not errors,
        "status": "SPOT_RUNTIME_COMPATIBLE" if not changed and not errors else "SPOT_RUNTIME_INCOMPATIBLE",
        "changed_critical_paths": sorted(changed),
        "errors": errors,
    }


def evaluate_pre_cutover_safety(
    *,
    local_state,
    bot_state,
    exchange_positions,
    futures_orders,
    spot_orders,
    spot_account,
    spot_filters,
    current_root=None,
    candidate_root=None,
    protection_tolerance=DEFAULT_PROTECTION_TOLERANCE,
):
    """Combine strict Futures checks with managed/protected Spot classification."""
    positions = local_state.get("positions") if isinstance(local_state, dict) else None
    reconciliation = None
    if isinstance(bot_state, dict):
        reconciliation = (((bot_state.get("positions") or {}).get("short") or {}).get("reconciliation"))
    spot = classify_spot_deploy_safety(
        positions,
        spot_account,
        spot_orders,
        spot_filters,
        protection_tolerance=protection_tolerance,
    )
    local_futures_clear = isinstance(positions, list) and not any(
        isinstance(position, dict)
        and str(position.get("direction") or "").strip().lower() == "short"
        for position in positions
    )
    futures_known = isinstance(exchange_positions, list)
    exchange_futures_clear = futures_known
    if futures_known:
        try:
            exchange_futures_clear = all(
                isinstance(row, dict) and _decimal(row.get("positionAmt"), "Futures position amount") == 0
                for row in exchange_positions
            )
        except SafetyEvidenceError:
            exchange_futures_clear = False
            futures_known = False
    orders_known = isinstance(futures_orders, list) and isinstance(spot_orders, list)
    spot_observation_known = isinstance(spot_account, dict) and isinstance(spot_filters, dict)
    reconciliation = reconciliation if isinstance(reconciliation, dict) else {}
    futures_reconciliation_aligned = (
        reconciliation.get("aligned") is True and reconciliation.get("status") == "ALINEADO"
    )
    count_checks = {}
    for name in ("managed", "orphan", "unmanaged", "unprotected", "desynced"):
        value = reconciliation.get(f"{name}_count")
        count_checks[name] = isinstance(value, int) and not isinstance(value, bool) and value == 0
    local_spot_count = sum(
        1
        for position in positions or []
        if isinstance(position, dict)
        and str(position.get("direction") or "").strip().lower() == "long"
    )
    if local_spot_count:
        compatibility = (
            check_spot_runtime_compatibility(current_root, candidate_root)
            if current_root is not None and candidate_root is not None
            else {
                "compatible": False,
                "status": "SPOT_RUNTIME_INCOMPATIBLE",
                "changed_critical_paths": [],
                "errors": ["missing_runtime_roots"],
            }
        )
    else:
        compatibility = {
            "compatible": True,
            "status": "SPOT_RUNTIME_COMPATIBILITY_NOT_REQUIRED",
            "changed_critical_paths": [],
            "errors": [],
        }
    checks = {
        "local_futures_positions_clear": local_futures_clear,
        "spot_positions_deploy_safe": spot["positions_safe"],
        "spot_orders_deploy_safe": spot["orders_safe"],
        "spot_runtime_compatible": compatibility["compatible"],
        "exchange_futures_positions": futures_known and exchange_futures_clear,
        "managed_futures": count_checks["managed"],
        "orphan_futures": count_checks["orphan"],
        "unmanaged_futures": count_checks["unmanaged"],
        "unprotected_futures": count_checks["unprotected"],
        "desynced_futures": count_checks["desynced"],
        "futures_reconciliation_aligned": futures_reconciliation_aligned,
        "futures_open_orders": isinstance(futures_orders, list) and len(futures_orders) == 0,
        "order_fetch_known": orders_known,
        "spot_observation_known": spot_observation_known,
    }
    return {
        "safe": all(checks.values()),
        "checks": checks,
        "spot": spot,
        "compatibility": compatibility,
        "local_spot_count": local_spot_count,
    }
