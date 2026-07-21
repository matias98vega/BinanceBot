#!/usr/bin/env python3
"""Pure, conservative state-vs-exchange pre-entry safety evaluation.

The evaluator never writes, sends orders, cancels protection or repairs state.
Callers own observability and enforcement. Production defaults to AUDIT_ONLY.
"""
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import os
import time


SAFE = 'SAFE_TO_ENTER'
AUDIT_ONLY = 'AUDIT_ONLY'
ENFORCE = 'ENFORCE'
DEFAULT_MAX_AGE_SECONDS = 180
DEFAULT_QTY_TOLERANCE = 1e-6
DEFAULT_PROTECTION_TOLERANCE = 1e-6

STATUS_PRIORITY = (
    'BLOCKED_ACTIVE_RISK_STATE', 'BLOCKED_EXCHANGE_STATE_UNKNOWN',
    'BLOCKED_ORPHAN_POSITION', 'BLOCKED_POSITION_MISMATCH',
    'BLOCKED_MISSING_PROTECTION', 'BLOCKED_RECONCILIATION_PENDING',
    'BLOCKED_DUPLICATE_SYMBOL', 'BLOCKED_CAPACITY',
    'BLOCKED_BALANCE_UNRELIABLE', 'BLOCKED_LOCAL_STATE_INVALID', SAFE,
)


def _now_iso(now=None):
    value = float(time.time() if now is None else now)
    return datetime.fromtimestamp(value, timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _number(value):
    try:
        number = float(value)
        return number if number == number and abs(number) != float('inf') else None
    except (TypeError, ValueError):
        return None


def _timestamp(value):
    if isinstance(value, (int, float)):
        return float(value) / 1000 if value > 10_000_000_000 else float(value)
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except Exception:
        return None


def configured_mode(environ=None):
    value = (environ or os.environ).get('PRE_ENTRY_GATE_MODE', AUDIT_ONLY).strip().upper()
    return ENFORCE if value == ENFORCE else AUDIT_ONLY


@dataclass(frozen=True)
class GateCheck:
    name: str
    passed: bool
    blocking: bool = False
    status_code: str = SAFE
    severity: str = 'INFO'
    reason: str = ''
    evidence: dict = field(default_factory=dict)
    source: str = 'pre_entry_safety_gate'
    observed_at: str = ''
    stale: bool = False
    unknown: bool = False


def _check(name, passed, status=SAFE, reason='', evidence=None, severity='RISK', observed_at='', stale=False, unknown=False, blocking=None):
    return GateCheck(name, bool(passed), (not passed if blocking is None else bool(blocking)), status if not passed else SAFE,
                     'INFO' if passed else severity, reason, evidence or {}, 'pre_entry_safety_gate', observed_at, stale, unknown)


def collect_exchange_observation(client, now=None):
    """Perform only existing GET/read-only calls and return reusable evidence."""
    observed_at = _now_iso(now)
    result = {'observed_at': observed_at, 'errors': {}, 'source': 'live_read_only'}
    reads = (
        ('spot_account', client.get_spot_account),
        ('futures_account', client.futures_account),
        ('futures_positions', client.futures_position_risk),
    )
    for key, reader in reads:
        try:
            result[key] = reader()
        except Exception as exc:
            result[key] = None
            result['errors'][key] = str(exc)
    try:
        result['spot_open_orders'] = client.spot_open_orders({}) if hasattr(client, 'spot_open_orders') else client.spot_signed('GET', '/api/v3/openOrders', {})
    except Exception as exc:
        result['spot_open_orders'] = None
        result['errors']['spot_open_orders'] = str(exc)
    try:
        result['futures_open_orders'] = client.futures_open_orders({})
    except Exception as exc:
        result['futures_open_orders'] = None
        result['errors']['futures_open_orders'] = str(exc)
    return result


def _positions(local_state):
    return local_state.get('positions') if isinstance(local_state, dict) and isinstance(local_state.get('positions'), list) else None


def _local_validation(local_state, observed_at):
    positions = _positions(local_state)
    errors, seen = [], {}
    if positions is None:
        return _check('LOCAL_STATE_VALID', False, 'BLOCKED_LOCAL_STATE_INVALID', 'positions is not a list', observed_at=observed_at)
    for index, pos in enumerate(positions):
        if not isinstance(pos, dict):
            errors.append(f'position[{index}] is not an object'); continue
        symbol = str(pos.get('symbol') or '').upper()
        side = str(pos.get('direction') or pos.get('side') or '').lower()
        qty, entry = _number(pos.get('quantity')), _number(pos.get('entry_price'))
        if not symbol: errors.append(f'position[{index}] missing symbol')
        if side not in {'long', 'short'}: errors.append(f'{symbol or index} invalid side')
        if qty is None or qty <= 0: errors.append(f'{symbol or index} invalid quantity')
        if entry is None or entry <= 0: errors.append(f'{symbol or index} invalid entry_price')
        key = (symbol, side)
        if key in seen and seen[key] != qty: errors.append(f'{symbol} conflicting duplicate quantities')
        seen[key] = qty
    return _check('LOCAL_STATE_VALID', not errors, 'BLOCKED_LOCAL_STATE_INVALID', '; '.join(errors), {'position_count': len(positions), 'errors': errors}, observed_at=observed_at)


def _freshness(observation, now, max_age):
    ts = _timestamp(observation.get('observed_at')) if isinstance(observation, dict) else None
    age = None if ts is None else max(0.0, float(now) - ts)
    status = 'UNKNOWN' if age is None else 'STALE' if age > max_age else 'FRESH'
    return status, age


def _spot_balances(account):
    if not isinstance(account, dict) or not isinstance(account.get('balances'), list): return None
    result = {}
    for row in account['balances']:
        free, locked = _number(row.get('free')), _number(row.get('locked'))
        if not row.get('asset') or free is None or locked is None: return None
        result[str(row['asset']).upper()] = {'free': free, 'locked': locked}
    return result


def _futures_positions(rows):
    if not isinstance(rows, list): return None
    result = {}
    for row in rows:
        amount = _number(row.get('positionAmt'))
        symbol = str(row.get('symbol') or '').upper()
        if not symbol or amount is None: return None
        if abs(amount) > 0: result[symbol] = {**row, 'amount': amount}
    return result


def _order_qty(order):
    return _number(order.get('origQty') or order.get('quantity')) or 0.0


def _active(order):
    return str(order.get('status') or 'NEW').upper() in {'NEW', 'PARTIALLY_FILLED', 'PENDING_NEW'}


def _spot_protected(pos, orders, tolerance):
    symbol, qty = str(pos.get('symbol')).upper(), _number(pos.get('quantity')) or 0
    relevant = [o for o in orders if str(o.get('symbol')).upper() == symbol and _active(o) and int(o.get('orderListId') or -1) >= 0]
    groups = {}
    for order in relevant: groups.setdefault(str(order.get('orderListId')), []).append(order)
    protected = max((sum(_order_qty(o) for o in group) / max(1, len(group)) for group in groups.values()), default=0)
    valid_group = any(len(group) >= 2 and any('STOP' in str(o.get('type', '')).upper() for o in group) and
                      any('LIMIT' in str(o.get('type', '')).upper() for o in group) for group in groups.values())
    return valid_group and protected + tolerance >= qty, {'protected_quantity': protected, 'groups': len(groups), 'orders': len(relevant)}


def _futures_protected(pos, orders, tolerance):
    symbol, qty = str(pos.get('symbol')).upper(), _number(pos.get('quantity')) or 0
    relevant = [o for o in orders if str(o.get('symbol')).upper() == symbol and _active(o) and str(o.get('reduceOnly')).lower() == 'true']
    protected = sum(_order_qty(o) for o in relevant)
    return bool(relevant) and protected + tolerance >= qty and all(str(o.get('side')).upper() == 'BUY' for o in relevant), {'protected_quantity': protected, 'orders': len(relevant)}


def evaluate_pre_entry_safety(*, client=None, local_state=None, bot_state=None, side='LONG', symbol='', reconciliation_status=None,
                              context=None, mode=None, now=None, max_age_seconds=None, quantity_tolerance=None,
                              protection_tolerance=None):
    """Return a structured decision. The function itself has no side effects."""
    started = time.perf_counter()
    now = float(time.time() if now is None else now)
    side, symbol = str(side).upper(), str(symbol).upper()
    mode = (mode or configured_mode()).upper()
    max_age = float(max_age_seconds or os.getenv('PRE_ENTRY_EXCHANGE_OBSERVATION_MAX_AGE_SECONDS', DEFAULT_MAX_AGE_SECONDS))
    qty_tol = float(quantity_tolerance or os.getenv('PRE_ENTRY_POSITION_QTY_TOLERANCE', DEFAULT_QTY_TOLERANCE))
    protect_tol = float(protection_tolerance or os.getenv('PRE_ENTRY_PROTECTION_TOLERANCE', DEFAULT_PROTECTION_TOLERANCE))
    local_state = local_state if isinstance(local_state, dict) else {}
    bot_state = bot_state if isinstance(bot_state, dict) else {}
    context = dict(context or {})
    observation = context.get('exchange_observation')
    if observation is None and client is not None:
        observation = collect_exchange_observation(client, now=now)
    observation = observation if isinstance(observation, dict) else {}
    observed_at = str(observation.get('observed_at') or '')
    checks = [_local_validation(local_state, observed_at)]

    fresh_status, age = _freshness(observation, now, max_age)
    errors = observation.get('errors') if isinstance(observation.get('errors'), dict) else {}
    required = ('spot_account', 'spot_open_orders', 'futures_account', 'futures_positions', 'futures_open_orders')
    complete = fresh_status == 'FRESH' and all(observation.get(key) is not None for key in required) and not errors
    checks.append(_check('EXCHANGE_READ_COMPLETE', complete, 'BLOCKED_EXCHANGE_STATE_UNKNOWN',
                         'essential exchange observation missing, stale or failed', {'freshness': fresh_status, 'age_seconds': age, 'errors': errors},
                         observed_at=observed_at, stale=fresh_status == 'STALE', unknown=fresh_status == 'UNKNOWN' or bool(errors)))

    positions = _positions(local_state) or []
    local_longs = [p for p in positions if str(p.get('direction') or '').lower() == 'long']
    local_shorts = [p for p in positions if str(p.get('direction') or '').lower() == 'short']
    balances = _spot_balances(observation.get('spot_account'))
    futures = _futures_positions(observation.get('futures_positions'))
    spot_orders = observation.get('spot_open_orders') if isinstance(observation.get('spot_open_orders'), list) else []
    fut_orders = observation.get('futures_open_orders') if isinstance(observation.get('futures_open_orders'), list) else []

    balance_ok = balances is not None and isinstance(observation.get('futures_account'), dict)
    if balance_ok:
        usdt = balances.get('USDT', {})
        balance_ok = all(_number(usdt.get(key)) is not None and _number(usdt.get(key)) >= 0 for key in ('free', 'locked'))
        for key in ('totalWalletBalance', 'availableBalance'):
            value = _number(observation['futures_account'].get(key))
            balance_ok = balance_ok and value is not None and value >= 0
    checks.append(_check('BALANCES_RELIABLE', balance_ok, 'BLOCKED_BALANCE_UNRELIABLE', 'balance values are missing, invalid or negative', observed_at=observed_at, unknown=not balance_ok))

    mismatches, missing_protection = [], []
    if balances is not None:
        for pos in local_longs:
            asset = str(pos.get('symbol')).removesuffix('USDT')
            observed_qty = sum((balances.get(asset) or {}).get(k, 0) for k in ('free', 'locked'))
            expected = _number(pos.get('quantity')) or 0
            if abs(observed_qty - expected) > max(qty_tol, expected * qty_tol): mismatches.append({'symbol': pos.get('symbol'), 'local': expected, 'exchange': observed_qty})
            protected, evidence = _spot_protected(pos, spot_orders, protect_tol)
            if not protected: missing_protection.append({'symbol': pos.get('symbol'), 'side': 'LONG', **evidence})
    if futures is not None:
        for pos in local_shorts:
            observed = futures.get(str(pos.get('symbol')).upper())
            expected = _number(pos.get('quantity')) or 0
            actual = abs(observed['amount']) if observed else 0
            if not observed or observed['amount'] >= 0 or abs(actual - expected) > max(qty_tol, expected * qty_tol):
                mismatches.append({'symbol': pos.get('symbol'), 'local': -expected, 'exchange': None if not observed else observed['amount']})
            protected, evidence = _futures_protected(pos, fut_orders, protect_tol)
            if not protected: missing_protection.append({'symbol': pos.get('symbol'), 'side': 'SHORT', **evidence})
    checks.append(_check('MANAGED_POSITIONS_MATCH_OBSERVED', not mismatches, 'BLOCKED_POSITION_MISMATCH', 'managed position quantities/sides differ from exchange', {'mismatches': mismatches}, observed_at=observed_at))

    local_short_symbols = {str(p.get('symbol')).upper() for p in local_shorts}
    orphan_futures = sorted(set(futures or {}) - local_short_symbols)
    checks.append(_check('NO_ORPHAN_POSITIONS', not orphan_futures, 'BLOCKED_ORPHAN_POSITION', 'unmanaged Futures position observed', {'symbols': orphan_futures}, observed_at=observed_at))
    checks.append(_check('EXISTING_POSITIONS_PROTECTED', not missing_protection, 'BLOCKED_MISSING_PROTECTION', 'managed position lacks complete exchange protection', {'positions': missing_protection}, observed_at=observed_at))

    reconciliation_status = reconciliation_status if isinstance(reconciliation_status, dict) else {}
    material_pending = bool(reconciliation_status.get('position_pending') or reconciliation_status.get('pending_position_reconciliation') or
                            reconciliation_status.get('orphan_count') or reconciliation_status.get('mismatch_count'))
    checks.append(_check('NO_PENDING_RECONCILIATION', not material_pending, 'BLOCKED_RECONCILIATION_PENDING', 'material position reconciliation is pending', {'status': reconciliation_status}, observed_at=observed_at))

    capacity = context.get('capacity') if isinstance(context.get('capacity'), dict) else {}
    current = capacity.get('current', len(local_longs) if side == 'LONG' else len(local_shorts))
    maximum = capacity.get('operational_max')
    allowed = capacity.get('new_entries_allowed')
    capacity_known = _number(current) is not None and _number(maximum) is not None
    capacity_ok = capacity_known and _number(current) < _number(maximum) and allowed is not False
    checks.append(_check('CAPACITY_AVAILABLE', capacity_ok, 'BLOCKED_CAPACITY', 'operational capacity unavailable or full', {'current': current, 'operational_max': maximum, 'target_max': capacity.get('target_max'), 'new_entries_allowed': allowed}, observed_at=observed_at, unknown=not capacity_known))

    local_symbols = {str(p.get('symbol')).upper() for p in positions}
    exchange_symbols = set(futures or {})
    open_order_symbols = {str(o.get('symbol')).upper() for o in spot_orders + fut_orders if _active(o)}
    cooldowns = local_state.get('cooldown_symbols') or {}
    cooldown_symbols = set(cooldowns if isinstance(cooldowns, list) else (key for key, expiry in cooldowns.items() if not expiry or _number(expiry) > now))
    duplicate_reasons = []
    if symbol in local_symbols: duplicate_reasons.append('SAME_SYMBOL_MANAGED_POSITION')
    if symbol in exchange_symbols and symbol not in local_symbols: duplicate_reasons.append('SAME_SYMBOL_ORPHAN_POSITION')
    if symbol in open_order_symbols and symbol not in local_symbols: duplicate_reasons.append('SAME_SYMBOL_OPEN_ORDER')
    if symbol in cooldown_symbols: duplicate_reasons.append('SAME_SYMBOL_COOLDOWN')
    checks.append(_check('NO_DUPLICATE_SYMBOL', not duplicate_reasons, 'BLOCKED_DUPLICATE_SYMBOL', 'symbol already active, ordered or cooling down', {'reasons': duplicate_reasons}, observed_at=observed_at))

    pause = bot_state.get('safety_pause') if isinstance(bot_state.get('safety_pause'), dict) else {}
    risk_active = local_state.get('status') == 'paused' or (_number(local_state.get('pause_until')) or 0) > now or pause.get('active') is True or context.get('active_risk_state') is True
    checks.append(_check('NO_ACTIVE_RISK_PAUSE', not risk_active, 'BLOCKED_ACTIVE_RISK_STATE', 'existing policy has paused new entries', {'state_status': local_state.get('status'), 'pause_until': local_state.get('pause_until')}, observed_at=observed_at))
    unknown_orders = observation.get('spot_open_orders') is None or observation.get('futures_open_orders') is None
    checks.append(_check('NO_UNKNOWN_ORDER_STATE', not unknown_orders, 'BLOCKED_EXCHANGE_STATE_UNKNOWN', 'open order state is unknown', observed_at=observed_at, unknown=unknown_orders))
    side_consistent = side in {'LONG', 'SHORT'}
    checks.append(_check('SIDE_SPECIFIC_STATE_CONSISTENT', side_consistent, 'BLOCKED_LOCAL_STATE_INVALID', 'side must be LONG or SHORT', {'side': side}, observed_at=observed_at))

    blockers = [item for item in checks if item.blocking]
    statuses = {item.status_code for item in blockers}
    status = next((candidate for candidate in STATUS_PRIORITY if candidate in statuses), SAFE)
    safe = not blockers
    result = {
        'safe_to_enter': safe, 'entry_allowed': safe or mode == AUDIT_ONLY, 'status': status,
        'mode': mode, 'reasons': [item.reason for item in blockers if item.reason],
        'blocking_reasons': [item.status_code for item in blockers],
        'checks': {item.name: asdict(item) for item in checks}, 'observed_at': observed_at,
        'freshness': {'exchange': fresh_status, 'age_seconds': age, 'max_age_seconds': max_age},
        'side': side, 'symbol': symbol, 'severity': 'OK' if safe else 'RISK',
        'duration_ms': round((time.perf_counter() - started) * 1000, 3), 'source': 'pre_entry_safety_gate',
        'tolerances': {'quantity': qty_tol, 'protection': protect_tol},
    }
    return result


def rejection_reason(result):
    return f'PRE_ENTRY_GATE_{str((result or {}).get("status") or "UNKNOWN").removeprefix("BLOCKED_")}'
