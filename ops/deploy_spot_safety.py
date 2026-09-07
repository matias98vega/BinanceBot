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

PREVENTIVE_SPOT_CLOSE_PATH = "trading/preventive_spot_close.py"

# Structural fingerprints for the one audited migration from the legacy inline
# preventive LONG Spot block to the fail-closed helper. Comments and formatting
# are ignored by AST normalization; every semantic change remains incompatible.
_AUDITED_PREVENTIVE_SPOT_AST = {
    "legacy_block": "44cb3f5cb67e53b61bb751bfb8db2d3306261428b8cad63bcc6dd1fdd9c538fe",
    "helper_wiring": "a93cfdc95cf05e4c50fdc2017e028dc62afe9b97d43ba8e36678c0cc6c3f3bb8",
    "helper_method": "7f5812b35138ee764bd2a5f44a110fac7b0af6cb588b7efc56abe2e92048b0a1",
    "helper_module": "ef9752d4f7785ebc35af7310f6acb22902604fd22db981f0a406b16d74ba0d24",
}

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


def _ast_sha256(node):
    payload = ast.dump(node, annotate_fields=True, include_attributes=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _statement_block_sha256(statements):
    return _ast_sha256(ast.Module(body=list(statements), type_ignores=[]))


def _single_named_member(container, node_type, name):
    matches = [
        item for item in container
        if isinstance(item, node_type) and item.name == name
    ]
    if len(matches) != 1:
        raise SafetyEvidenceError(f"expected one {name}, found {len(matches)}")
    return matches[0]


def _is_preventive_spot_import(node):
    return (
        isinstance(node, ast.Import)
        and len(node.names) == 1
        and node.names[0].name == "preventive_spot_close"
        and node.names[0].asname is None
    )


def _normalize_audited_preventive_spot_flow(tree):
    """Normalize only the exact audited legacy-to-helper transition."""
    cycle_class = _single_named_member(tree.body, ast.ClassDef, "CycleRunner")
    run_method = _single_named_member(cycle_class.body, ast.FunctionDef, "run")
    helper_methods = [
        item for item in cycle_class.body
        if isinstance(item, ast.FunctionDef)
        and item.name == "_handle_preventive_long_spot"
    ]
    helper_imports = [item for item in tree.body if _is_preventive_spot_import(item)]
    helper_calls = [
        item for item in ast.walk(run_method)
        if isinstance(item, ast.Attribute)
        and item.attr == "_handle_preventive_long_spot"
    ]

    flow_matches = []
    for node in ast.walk(run_method):
        if not isinstance(node, ast.If) or len(node.body) < 2:
            continue
        digest = _statement_block_sha256(node.body[1:])
        if digest == _AUDITED_PREVENTIVE_SPOT_AST["legacy_block"]:
            flow_matches.append(("legacy_block", node))
        elif digest == _AUDITED_PREVENTIVE_SPOT_AST["helper_wiring"]:
            flow_matches.append(("helper_wiring", node))

    has_transition_components = bool(helper_methods or helper_imports or helper_calls)
    if not flow_matches:
        if has_transition_components:
            raise SafetyEvidenceError("incomplete preventive Spot close integration")
        return tree
    if len(flow_matches) != 1:
        raise SafetyEvidenceError("ambiguous preventive Spot close integration")

    flow_kind, flow_node = flow_matches[0]
    if flow_kind == "legacy_block":
        if has_transition_components:
            raise SafetyEvidenceError("legacy flow mixed with helper integration")
    else:
        if len(helper_imports) != 1 or len(helper_methods) != 1 or len(helper_calls) != 1:
            raise SafetyEvidenceError("incomplete preventive Spot close integration")
        if _ast_sha256(helper_methods[0]) != _AUDITED_PREVENTIVE_SPOT_AST["helper_method"]:
            raise SafetyEvidenceError("unexpected preventive Spot helper method")
        tree.body.remove(helper_imports[0])
        cycle_class.body.remove(helper_methods[0])

    flow_node.body[1:] = [
        ast.Expr(value=ast.Constant(value="AUDITED_PREVENTIVE_SPOT_CLOSE_FLOW"))
    ]
    return tree


def _normalized_module_ast(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return ast.dump(tree, annotate_fields=True, include_attributes=False)


def _cycle_uses_preventive_spot_helper(root):
    path = root / "trading/orchestration/cycle_runner.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return any(
        _is_preventive_spot_import(item)
        or (
            isinstance(item, ast.FunctionDef)
            and item.name == "_handle_preventive_long_spot"
        )
        or (
            isinstance(item, ast.Attribute)
            and item.attr == "_handle_preventive_long_spot"
        )
        for item in ast.walk(tree)
    )


def _check_preventive_spot_helper(current_root, candidate_root, changed, errors):
    current = current_root / PREVENTIVE_SPOT_CLOSE_PATH
    candidate = candidate_root / PREVENTIVE_SPOT_CLOSE_PATH
    try:
        current_uses_helper = _cycle_uses_preventive_spot_helper(current_root)
        candidate_uses_helper = _cycle_uses_preventive_spot_helper(candidate_root)
    except (OSError, SyntaxError, UnicodeError) as exc:
        errors.append(f"parse:{PREVENTIVE_SPOT_CLOSE_PATH}:dependency:{type(exc).__name__}")
        return
    if current_uses_helper and not current.is_file():
        errors.append(f"missing:{PREVENTIVE_SPOT_CLOSE_PATH}:current")
    if candidate_uses_helper and not candidate.is_file():
        errors.append(f"missing:{PREVENTIVE_SPOT_CLOSE_PATH}:candidate")
    if not current.is_file() and not candidate.is_file():
        return
    if not candidate.is_file():
        changed.append(PREVENTIVE_SPOT_CLOSE_PATH)
        return
    try:
        candidate_ast = _normalized_module_ast(candidate)
        if current.is_file():
            if _normalized_module_ast(current) != candidate_ast:
                changed.append(PREVENTIVE_SPOT_CLOSE_PATH)
        elif hashlib.sha256(candidate_ast.encode("utf-8")).hexdigest() != _AUDITED_PREVENTIVE_SPOT_AST["helper_module"]:
            changed.append(PREVENTIVE_SPOT_CLOSE_PATH)
    except (OSError, SyntaxError, UnicodeError) as exc:
        errors.append(f"parse:{PREVENTIVE_SPOT_CLOSE_PATH}:{type(exc).__name__}")


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
    if profile == "cycle_runner":
        normalized = _normalize_audited_preventive_spot_flow(normalized)
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
        except (OSError, SafetyEvidenceError, SyntaxError, UnicodeError) as exc:
            errors.append(f"parse:{relative}:{type(exc).__name__}")
    _check_preventive_spot_helper(current_root, candidate_root, changed, errors)
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
