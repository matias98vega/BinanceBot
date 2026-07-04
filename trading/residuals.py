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
                                    path=None):
    """Persist an unprotectable residual and return the updated entry plus alert decision."""
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
    }
    residuals[symbol] = entry
    data['residuals'] = residuals
    data['updated_at'] = now
    _save(data, path)
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
    return (
        f'⚠️ {entry.get("asset") or entry.get("symbol")} residual sin OCO.\n'
        'No se puede proteger porque el valor queda por debajo del mínimo permitido por Binance.\n'
        f'Cantidad: {entry.get("quantity", 0):.8f}\n'
        f'Valor estimado: {entry.get("estimated_value", 0):.2f} USDT\n'
        f'Mínimo requerido: {entry.get("min_notional", 0):.2f} USDT\n'
        'Acción sugerida: vender manualmente o acumular más saldo antes de proteger.'
    )
