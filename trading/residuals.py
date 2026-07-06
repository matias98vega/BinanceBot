#!/usr/bin/env python3
"""Read-only style state for Spot residuals that cannot be protected by OCO."""

import json
import logging
import os
import time
from datetime import datetime, timezone


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_STATUS_FILE = os.path.join(BASE_DIR, 'data', 'history', 'residuals_status.json')
ALERT_THROTTLE_SECONDS = 6 * 3600


def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _safe_float(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _status_path(path=None):
    return path or DEFAULT_STATUS_FILE


def _load(path=None):
    path = _status_path(path)
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logging.warning('residual status read failed path=%s error=%s', path, exc)
        return {}


def _save(data, path=None):
    path = _status_path(path)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = f'{path}.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write('\n')
        os.replace(tmp_path, path)
    except Exception as exc:
        logging.warning('residual status write failed path=%s error=%s', path, exc)


def load_status(path=None):
    return _load(path)


def classify_unprotectable_residual(symbol, asset, quantity, estimated_value, min_notional,
                                    reason='below_min_notional', rounded_qty=None,
                                    rounded_price=None, notional_after_rounding=None,
                                    limit_price=None, stop_price=None,
                                    stop_limit_price=None, limit_notional=None,
                                    stop_notional=None, min_leg_notional=None,
                                    limiting_leg=None,
                                    path=None):
    """Persist an unprotectable residual and return the updated entry plus alert decision."""
    resolved_path = _status_path(path)
    logging.warning(
        'RESIDUAL STATUS WRITE path=%s symbol=%s reason=%s quantity=%s estimated_value=%s',
        resolved_path, symbol, reason, quantity, estimated_value,
    )
    data = _load(path)
    residuals = data.get('residuals') if isinstance(data.get('residuals'), dict) else {}
    previous = residuals.get(symbol) if isinstance(residuals.get(symbol), dict) else {}
    now = _now_iso()
    last_alert = previous.get('last_alert')
    last_alert_ts = _parse_ts(last_alert)
    should_alert = last_alert_ts is None or time.time() - last_alert_ts >= ALERT_THROTTLE_SECONDS
    alert_count = int(previous.get('alert_count') or 0)
    if should_alert:
        alert_count += 1
    entry = {
        'symbol': symbol,
        'asset': asset,
        'quantity': _safe_float(quantity, 0.0),
        'estimated_value': _safe_float(estimated_value, 0.0),
        'min_notional': _safe_float(min_notional, 0.0),
        'reason': reason,
        'status': 'unprotectable_residual',
        'first_seen': previous.get('first_seen') or now,
        'last_seen': now,
        'last_alert': now if should_alert else previous.get('last_alert'),
        'alert_count': alert_count,
        'suggested_action': 'vender manualmente o acumular mas saldo antes de proteger',
        'rounded_qty': _safe_float(rounded_qty),
        'rounded_price': _safe_float(rounded_price),
        'notional_after_rounding': _safe_float(notional_after_rounding),
        'limit_price': _safe_float(limit_price),
        'stop_price': _safe_float(stop_price),
        'stop_limit_price': _safe_float(stop_limit_price),
        'limit_notional': _safe_float(limit_notional),
        'stop_notional': _safe_float(stop_notional),
        'min_leg_notional': _safe_float(min_leg_notional),
        'limiting_leg': limiting_leg,
    }
    residuals[symbol] = entry
    data['residuals'] = residuals
    data['updated_at'] = now
    _save(data, resolved_path)
    return entry, should_alert


def _parse_ts(value):
    if not value:
        return None
    try:
        text = str(value).replace('Z', '+00:00')
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return None


def residual_alert_message(entry):
    if entry.get('reason') == 'oco_leg_below_min_notional':
        return (
            f'⚠️ {entry.get("asset") or entry.get("symbol")} residual sin OCO.\n'
            'No se puede proteger porque una pata de la OCO queda bajo el mínimo de Binance.\n'
            f'Cantidad: {entry.get("quantity", 0):.8f}\n'
            f'Valor estimado: {entry.get("estimated_value", 0):.2f} USDT\n'
            f'Mínimo requerido: {entry.get("min_notional", 0):.2f} USDT\n'
            f'Notional TP: {_safe_float(entry.get("limit_notional"), 0.0):.2f} USDT\n'
            f'Notional SL: {_safe_float(entry.get("stop_notional"), 0.0):.2f} USDT\n'
            f'Pata limitante: {entry.get("limiting_leg") or "No disponible"}\n'
            'Acción sugerida: vender manualmente o acumular más saldo antes de proteger.'
        )
    return (
        f'⚠️ {entry.get("asset") or entry.get("symbol")} residual sin OCO.\n'
        'No se puede proteger porque el valor queda por debajo del mínimo permitido por Binance.\n'
        f'Cantidad: {entry.get("quantity", 0):.8f}\n'
        f'Valor estimado: {entry.get("estimated_value", 0):.2f} USDT\n'
        f'Mínimo requerido: {entry.get("min_notional", 0):.2f} USDT\n'
        'Acción sugerida: vender manualmente o acumular más saldo antes de proteger.'
    )


def handle_unprotectable_spot_residual(symbol, asset, quantity, price, filters,
                                       reason='below_min_notional', out_fn=None,
                                       limit_price=None, stop_price=None,
                                       stop_limit_price=None,
                                       path=None):
    """Return True when the Spot balance is too small to protect and was recorded."""
    import logging
    import decision_timeline
    import utils

    filters = filters if isinstance(filters, dict) else {}
    step = _safe_float(filters.get('step_size'), 0.0) or 0.0
    tick = _safe_float(filters.get('tick_size'), 0.0) or 0.0
    min_qty = _safe_float(filters.get('min_qty'), 0.0) or 0.0
    min_notional = _safe_float(filters.get('min_notional'), 5.0) or 5.0
    qty = _safe_float(quantity, 0.0) or 0.0
    px = _safe_float(price, 0.0) or 0.0
    rounded_qty = utils.round_step(qty, step) if step else qty
    rounded_price = utils.round_tick(px, tick) if tick else px
    notional_after_rounding = rounded_qty * rounded_price
    limit_px = _safe_float(limit_price)
    stop_px = _safe_float(stop_price)
    stop_limit_px = _safe_float(stop_limit_price)
    rounded_limit_price = utils.round_tick(limit_px, tick) if tick and limit_px is not None else limit_px
    rounded_stop_price = utils.round_tick(stop_px, tick) if tick and stop_px is not None else stop_px
    rounded_stop_limit_price = utils.round_tick(stop_limit_px, tick) if tick and stop_limit_px is not None else stop_limit_px
    limit_notional = rounded_qty * rounded_limit_price if rounded_limit_price is not None else None
    stop_notional = rounded_qty * rounded_stop_limit_price if rounded_stop_limit_price is not None else None
    min_leg_notional = None
    limiting_leg = None
    if limit_notional is not None and stop_notional is not None:
        if limit_notional <= stop_notional:
            min_leg_notional = limit_notional
            limiting_leg = 'TP'
        else:
            min_leg_notional = stop_notional
            limiting_leg = 'SL / stopLimitPrice'
    oco_leg_valid = min_leg_notional is None or min_leg_notional >= min_notional
    if (
        rounded_qty > 0
        and (min_qty <= 0 or rounded_qty >= min_qty)
        and notional_after_rounding >= min_notional
        and oco_leg_valid
    ):
        return False

    detected_reason = reason
    if rounded_qty <= 0 or (min_qty > 0 and rounded_qty < min_qty):
        detected_reason = 'below_min_qty'
    elif min_leg_notional is not None and min_leg_notional < min_notional:
        detected_reason = 'oco_leg_below_min_notional'
    entry, should_alert = classify_unprotectable_residual(
        symbol,
        asset,
        qty,
        qty * px,
        min_notional,
        reason=detected_reason,
        rounded_qty=rounded_qty,
        rounded_price=rounded_price,
        notional_after_rounding=notional_after_rounding,
        limit_price=rounded_limit_price,
        stop_price=rounded_stop_price,
        stop_limit_price=rounded_stop_limit_price,
        limit_notional=limit_notional,
        stop_notional=stop_notional,
        min_leg_notional=min_leg_notional,
        limiting_leg=limiting_leg,
        path=path,
    )
    logging.warning(
        'RESIDUAL UNPROTECTABLE symbol=%s asset=%s quantity=%s estimated_value=%.8f '
        'min_notional=%.8f reason=%s rounded_qty=%s rounded_price=%s '
        'notional_after_rounding=%.8f limit_price=%s stop_price=%s '
        'stop_limit_price=%s limit_notional=%s stop_notional=%s '
        'min_leg_notional=%s limiting_leg=%s',
        symbol,
        asset,
        qty,
        qty * px,
        min_notional,
        entry.get('reason'),
        rounded_qty,
        rounded_price,
        notional_after_rounding,
        rounded_limit_price,
        rounded_stop_price,
        rounded_stop_limit_price,
        limit_notional,
        stop_notional,
        min_leg_notional,
        limiting_leg,
    )
    try:
        decision_timeline.record_event(
            event='spot_residual_unprotectable',
            message=f'{symbol} residual sin OCO: valor bajo mínimo Binance',
            level='WARNING',
            category='RISK',
            symbol=symbol,
            direction='LONG',
            details={
                'quantity': qty,
                'estimated_value': qty * px,
                'min_notional': min_notional,
                'limit_price': rounded_limit_price,
                'stop_price': rounded_stop_price,
                'stop_limit_price': rounded_stop_limit_price,
                'limit_notional': limit_notional,
                'stop_notional': stop_notional,
                'min_leg_notional': min_leg_notional,
                'limiting_leg': limiting_leg,
                'reason': entry.get('reason'),
            },
        )
    except Exception:
        pass
    if out_fn:
        out_fn(
            f'⚠️ {asset} residual sin OCO: valor {notional_after_rounding:.2f} USDT '
            f'< mínimo {min_notional:.2f} USDT'
        )
    if should_alert:
        utils.send_alert(residual_alert_message(entry))
    return True
