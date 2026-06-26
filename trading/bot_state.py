#!/usr/bin/env python3
"""Persisted read model for bot/capital state.

This module is observability-only. It does not place orders, transfer funds, or
change trading decisions.
"""
import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone

from config_loader import PROJECT_DIR, load_dotenv


BOT_STATE_FILE = os.path.join(PROJECT_DIR, 'trading', 'bot_state.json')
UY_TZ = timezone(timedelta(hours=-3), 'America/Montevideo')


def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _float_or_none(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round_or_none(value, digits=4):
    value = _float_or_none(value)
    return None if value is None else round(value, digits)


def _env_float(name):
    load_dotenv()
    raw = os.environ.get(name)
    if raw in (None, ''):
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _systemctl_value(args, allow_nonzero=False):
    try:
        proc = subprocess.run(
            ['systemctl', *args],
            text=True,
            capture_output=True,
            timeout=2,
            check=False,
        )
        if proc.returncode == 0 or allow_nonzero:
            return (proc.stdout or '').strip()
    except Exception:
        pass
    return None


def _systemctl_available():
    return _systemctl_value(['--version']) is not None


def _unit_load_state(unit_name):
    return _systemctl_value(['show', unit_name, '--property=LoadState', '--value'], allow_nonzero=True)


def _unit_exists(unit_name):
    load_state = _unit_load_state(unit_name)
    return load_state not in (None, '', 'not-found')


def _unit_active_state(unit_name):
    return _systemctl_value(['is-active', unit_name], allow_nonzero=True)


def _unit_enabled_state(unit_name):
    return _systemctl_value(['is-enabled', unit_name], allow_nonzero=True)


def _timer_has_next_run(timer_name):
    next_elapse = _systemctl_value(
        ['show', timer_name, '--property=NextElapseUSecRealtime', '--value'],
        allow_nonzero=True,
    )
    return bool(next_elapse and next_elapse.lower() not in {'n/a', '0'})


def _bot_systemd_status():
    if not _systemctl_available():
        return 'UNKNOWN'
    service = 'binancebot.service'
    timer = 'binancebot.timer'
    service_exists = _unit_exists(service)
    timer_exists = _unit_exists(timer)
    service_active = _unit_active_state(service)
    timer_active = _unit_active_state(timer)
    timer_enabled = _unit_enabled_state(timer)

    if service_active == 'active':
        return 'RUNNING'
    if service_active == 'failed' or timer_active == 'failed':
        return 'OFFLINE'
    if timer_exists and timer_active == 'active' and timer_enabled == 'enabled' and _timer_has_next_run(timer):
        return 'ONLINE'
    if timer_exists and timer_active in {'inactive', 'unknown'} and timer_enabled in {'disabled', 'masked'}:
        return 'PAUSED'
    if timer_exists and timer_enabled in {'disabled', 'masked'}:
        return 'PAUSED'
    if not service_exists and not timer_exists:
        return 'OFFLINE'
    return 'OFFLINE'


def _timer_service_status(service_name, timer_name=None):
    if not _systemctl_available():
        return 'UNKNOWN'

    service_exists = _unit_exists(service_name)
    service_active = _unit_active_state(service_name)
    if service_active == 'active':
        return 'ONLINE'
    if service_active == 'failed':
        return 'OFFLINE'

    timer_exists = False
    if timer_name:
        timer_exists = _unit_exists(timer_name)
        timer_active = _unit_active_state(timer_name)
        timer_enabled = _unit_enabled_state(timer_name)
        if timer_active == 'active' or timer_enabled == 'enabled':
            return 'ONLINE'
        if timer_active == 'failed':
            return 'OFFLINE'
        if timer_exists and timer_enabled in {'disabled', 'masked'}:
            return 'PAUSED'
        if timer_exists and timer_active in {'inactive', 'unknown'}:
            return 'PAUSED'

    if service_exists and service_active in {'inactive', 'unknown'}:
        return 'OFFLINE'
    if not service_exists and not timer_exists:
        return 'OFFLINE'
    return 'OFFLINE'


def _service_status(service_name):
    if not _systemctl_available():
        return 'UNKNOWN'
    if not _unit_exists(service_name):
        return 'OFFLINE'
    active = _unit_active_state(service_name)
    if active == 'active':
        return 'ONLINE'
    return 'OFFLINE'


def get_system_statuses():
    return {
        'bot': _bot_systemd_status(),
        'guardian': _timer_service_status('binancebot-guardian.service', 'binancebot-guardian.timer'),
        'dashboard': _service_status('binancebot-dashboard.service'),
        'telegram': _service_status('binancebot-telegram.service'),
    }


def _wallet_max_positions(target_capital, configured_max, dynamic_value):
    target = _float_or_none(target_capital)
    if target is not None and target <= 0:
        return 0
    if dynamic_value is None:
        return configured_max
    try:
        dynamic_int = int(dynamic_value)
    except (TypeError, ValueError):
        return configured_max
    return max(0, min(dynamic_int, configured_max))


def get_total_capital_limit():
    return _env_float('BOT_TOTAL_CAPITAL_LIMIT_USDT')


def validate_total_capital_limit():
    value = get_total_capital_limit()
    if value is None:
        return False, 'Missing required environment variable: BOT_TOTAL_CAPITAL_LIMIT_USDT', None
    if value <= 0:
        return False, 'BOT_TOTAL_CAPITAL_LIMIT_USDT must be > 0', value
    return True, '', value


def load_bot_state(default=None):
    try:
        with open(BOT_STATE_FILE, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else default
    except Exception:
        return default


def _position_capital(state):
    positions = state.get('positions') if isinstance(state, dict) else []
    if not isinstance(positions, list):
        positions = []
    longs = [p for p in positions if isinstance(p, dict) and p.get('direction') == 'long']
    shorts = [p for p in positions if isinstance(p, dict) and p.get('direction') == 'short']
    spot_used = sum(
        float(p.get('entry_price') or 0) * float(p.get('quantity') or 0)
        for p in longs
    )
    futures_used = 0.0
    for pos in shorts:
        leverage = float(pos.get('leverage') or 1)
        if leverage <= 0:
            leverage = 1.0
        futures_used += float(pos.get('entry_price') or 0) * float(pos.get('quantity') or 0) / leverage
    return longs, shorts, spot_used, futures_used


def calculate_targets(total_authorized, btc_ctx, config_module, rebalance_module):
    if total_authorized is None:
        return None, None
    trend = 'unknown'
    if isinstance(btc_ctx, dict):
        trend = btc_ctx.get('trend') or 'unknown'

    if getattr(config_module, 'DIRECTIONAL_MODE', False) and trend == 'bearish':
        return 0.0, float(total_authorized)
    if getattr(config_module, 'DIRECTIONAL_MODE', False) and trend == 'bullish':
        return float(total_authorized), 0.0
    if trend == 'bearish':
        ratio = getattr(rebalance_module, 'RATIO_BEARISH_FUTURES', 0.65)
        return float(total_authorized) * (1 - ratio), float(total_authorized) * ratio
    if trend == 'bullish':
        ratio = 1.0 if getattr(config_module, 'DIRECTIONAL_MODE', False) else getattr(rebalance_module, 'RATIO_BULLISH_SPOT', 0.65)
        return float(total_authorized) * ratio, float(total_authorized) * (1 - ratio)
    return float(total_authorized) * 0.5, float(total_authorized) * 0.5


def build_bot_state(
    state,
    btc_ctx=None,
    spot_real=None,
    futures_real=None,
    spot_target=None,
    futures_target=None,
    max_longs=None,
    max_shorts=None,
    system_health='OK',
    bot_status='ONLINE',
    guardian_status='UNKNOWN',
    dashboard_status='UNKNOWN',
):
    import config
    import rebalance
    import utils

    state = state if isinstance(state, dict) else {}
    longs, shorts, spot_used, futures_used = _position_capital(state)
    spot_real = _float_or_none(spot_real)
    futures_real = _float_or_none(futures_real)
    total_real = None if spot_real is None or futures_real is None else spot_real + futures_real
    total_limit = get_total_capital_limit()

    warning = None
    if total_real is None or total_limit is None or total_limit <= 0:
        total_authorized = None
        if total_limit is None or total_limit <= 0:
            warning = 'BOT_TOTAL_CAPITAL_LIMIT_USDT missing or invalid'
    else:
        total_authorized = min(total_real, total_limit)
        if total_real < total_limit:
            warning = 'Capital real menor al limite configurado; usando capital real disponible.'

    if spot_target is None or futures_target is None:
        spot_target, futures_target = calculate_targets(total_authorized, btc_ctx, config, rebalance)

    if max_longs is None:
        try:
            max_longs = _wallet_max_positions(
                spot_target,
                getattr(config, 'MAX_LONG_POSITIONS', 2),
                utils.get_max_long_positions(spot_target if spot_target is not None else (spot_real or 0)),
            )
        except Exception:
            max_longs = None
    if max_shorts is None:
        try:
            max_shorts = _wallet_max_positions(
                futures_target,
                getattr(config, 'MAX_SHORT_POSITIONS', 2),
                utils.get_max_short_positions(futures_target if futures_target is not None else (futures_real or 0)),
            )
        except Exception:
            max_shorts = None

    last_execution = state.get('last_update') or _now_iso()
    live_system = get_system_statuses()
    if live_system.get('bot') != 'UNKNOWN':
        bot_status = live_system.get('bot')
    if live_system.get('guardian') != 'UNKNOWN':
        guardian_status = live_system.get('guardian')
    if live_system.get('dashboard') != 'UNKNOWN':
        dashboard_status = live_system.get('dashboard')
    return {
        'timestamp': _now_iso(),
        'timezone': 'America/Montevideo',
        'market': {
            'regime': btc_ctx.get('trend') if isinstance(btc_ctx, dict) else 'unknown',
            'directional_mode': getattr(config, 'DIRECTIONAL_MODE', None),
            'force_mode': btc_ctx.get('force_mode') if isinstance(btc_ctx, dict) else None,
        },
        'capital': {
            'total_real': _round_or_none(total_real, 4),
            'total_limit': _round_or_none(total_limit, 4),
            'total_authorized': _round_or_none(total_authorized, 4),
            'spot_real': _round_or_none(spot_real, 4),
            'futures_real': _round_or_none(futures_real, 4),
            'spot_target': _round_or_none(spot_target, 4),
            'futures_target': _round_or_none(futures_target, 4),
            'spot_used': _round_or_none(spot_used, 4),
            'futures_used': _round_or_none(futures_used, 4),
            'spot_available_for_bot': _round_or_none(None if spot_target is None else max(0.0, spot_target - spot_used), 4),
            'futures_available_for_bot': _round_or_none(None if futures_target is None else max(0.0, futures_target - futures_used), 4),
            'warning': warning,
        },
        'positions': {
            'long': {'current': len(longs), 'max': max_longs},
            'short': {'current': len(shorts), 'max': max_shorts},
        },
        'pnl': {
            'today': _round_or_none(state.get('daily_pnl_usdt', 0), 4),
            'total': _round_or_none(state.get('total_pnl_usdt', 0), 4),
        },
        'system': {
            'health': system_health or 'UNKNOWN',
            'bot': bot_status or 'UNKNOWN',
            'guardian': guardian_status or 'UNKNOWN',
            'dashboard': dashboard_status or 'UNKNOWN',
            'last_execution': last_execution,
            'last_snapshot': None,
            'last_healthcheck': None,
        },
    }


def persist_bot_state(payload):
    os.makedirs(os.path.dirname(BOT_STATE_FILE), exist_ok=True)
    tmp = f'{BOT_STATE_FILE}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write('\n')
    os.replace(tmp, BOT_STATE_FILE)
    try:
        os.chmod(BOT_STATE_FILE, 0o600)
    except Exception:
        pass
    return BOT_STATE_FILE


def safe_persist_bot_state(**kwargs):
    try:
        payload = build_bot_state(**kwargs)
        return persist_bot_state(payload), None
    except Exception as exc:
        return None, str(exc)
