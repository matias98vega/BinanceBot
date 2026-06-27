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
    if not service_exists and not timer_exists:
        return 'OFFLINE'
    if timer_exists and timer_active == 'active':
        return 'ONLINE'
    if timer_exists and timer_enabled == 'enabled' and _timer_has_next_run(timer):
        return 'ONLINE'
    if timer_exists and timer_active in {'inactive', 'unknown'} and timer_enabled in {'disabled', 'masked'}:
        return 'PAUSED'
    if service_exists and service_active in {'inactive', 'unknown'}:
        return 'PAUSED'
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


def _rebalance_wallet_min(default=0.0):
    try:
        import rebalance
        return float(getattr(rebalance, 'REBALANCE_MIN_WALLET', default))
    except Exception:
        return float(default)


def compute_observable_max_positions(spot_target, futures_target, max_longs=None, max_shorts=None):
    """Return max position slots for observability using wallet targets as source.

    This is read-only and does not affect trading entry logic.
    """
    import config
    import utils

    try:
        if max_longs is None:
            dynamic_longs = utils.get_max_long_positions(spot_target if spot_target is not None else 0)
        else:
            dynamic_longs = min(
                int(max_longs),
                int(utils.get_max_long_positions(spot_target if spot_target is not None else 0)),
            )
        max_longs = _wallet_max_positions(
            spot_target,
            getattr(config, 'MAX_LONG_POSITIONS', 2),
            dynamic_longs,
        )
    except Exception:
        max_longs = _wallet_max_positions(
            spot_target,
            getattr(config, 'MAX_LONG_POSITIONS', 2),
            max_longs,
        )

    try:
        if max_shorts is None:
            dynamic_shorts = utils.get_max_short_positions(futures_target if futures_target is not None else 0)
        else:
            dynamic_shorts = min(
                int(max_shorts),
                int(utils.get_max_short_positions(futures_target if futures_target is not None else 0)),
            )
        max_shorts = _wallet_max_positions(
            futures_target,
            getattr(config, 'MAX_SHORT_POSITIONS', 2),
            dynamic_shorts,
        )
    except Exception:
        max_shorts = _wallet_max_positions(
            futures_target,
            getattr(config, 'MAX_SHORT_POSITIONS', 2),
            max_shorts,
        )

    return max_longs, max_shorts


def _reserved_wallet_amount(real_amount, target_amount, wallet_min):
    real = _float_or_none(real_amount)
    target = _float_or_none(target_amount)
    if real is None or target is None:
        return 0.0
    if target <= 0 and 0 < real <= wallet_min:
        return real
    return 0.0


def _build_rebalance_diagnostics(spot_real, futures_real, spot_target, futures_target, spot_used, futures_used, total_authorized, capital_note):
    payload = {
        'status': 'UNKNOWN',
        'direction': 'NONE',
        'amount_pending': None,
        'reason': 'Datos insuficientes',
        'spot_real': _round_or_none(spot_real, 4),
        'spot_target': _round_or_none(spot_target, 4),
        'futures_real': _round_or_none(futures_real, 4),
        'futures_target': _round_or_none(futures_target, 4),
    }
    if total_authorized is None:
        payload.update({'status': 'BLOCKED', 'reason': capital_note or 'Capital autorizado no disponible'})
        return payload
    if spot_real is None or futures_real is None:
        payload.update({'status': 'UNKNOWN', 'reason': 'Balances reales no disponibles'})
        return payload
    if spot_target is None or futures_target is None:
        payload.update({'status': 'UNKNOWN', 'reason': 'Targets de capital no disponibles'})
        return payload

    try:
        import rebalance
        threshold = float(getattr(rebalance, 'REBALANCE_MIN_USDT', 2.0))
        wallet_min = float(getattr(rebalance, 'REBALANCE_MIN_WALLET', 0.0))
    except Exception:
        threshold = 2.0
        wallet_min = 0.0

    spot_real = float(spot_real)
    futures_real = float(futures_real)
    spot_target = float(spot_target)
    futures_target = float(futures_target)
    spot_diff = abs(spot_real - spot_target)
    futures_diff = abs(futures_real - futures_target)
    if spot_diff <= threshold and futures_diff <= threshold:
        payload.update({'status': 'NOT_REQUIRED', 'direction': 'NONE', 'amount_pending': 0.0, 'reason': 'Capital ya balanceado'})
        return payload

    spot_free_est = max(0.0, spot_real - float(spot_used or 0))
    futures_free_est = max(0.0, futures_real - float(futures_used or 0))
    if spot_real > spot_target and futures_real < futures_target:
        amount = min(spot_real - spot_target, futures_target - futures_real)
        if amount <= max(threshold, wallet_min):
            payload.update({
                'status': 'NOT_REQUIRED',
                'direction': 'NONE',
                'amount_pending': 0.0,
                'reason': 'Capital alineado respetando reserva minima de Spot',
            })
            return payload
        status = 'PENDING'
        reason = 'Esperando rebalance Spot -> Futures'
        if spot_free_est < min(amount, threshold):
            status = 'BLOCKED'
            reason = 'Esperando liberar Spot'
        payload.update({
            'status': status,
            'direction': 'SPOT_TO_FUTURES',
            'amount_pending': _round_or_none(amount, 4),
            'reason': reason,
        })
        return payload
    if futures_real > futures_target and spot_real < spot_target:
        amount = min(futures_real - futures_target, spot_target - spot_real)
        if amount <= max(threshold, wallet_min):
            payload.update({
                'status': 'NOT_REQUIRED',
                'direction': 'NONE',
                'amount_pending': 0.0,
                'reason': 'Capital alineado respetando reserva minima de Futures',
            })
            return payload
        status = 'PENDING'
        reason = 'Esperando rebalance Futures -> Spot'
        if futures_free_est < min(amount, threshold):
            status = 'BLOCKED'
            reason = 'Esperando liberar Futures'
        payload.update({
            'status': status,
            'direction': 'FUTURES_TO_SPOT',
            'amount_pending': _round_or_none(amount, 4),
            'reason': reason,
        })
        return payload

    pending = max(spot_diff, futures_diff)
    if pending <= max(threshold, wallet_min):
        payload.update({
            'status': 'NOT_REQUIRED',
            'direction': 'NONE',
            'amount_pending': 0.0,
            'reason': 'Capital alineado respetando reserva minima de wallet',
        })
        return payload

    payload.update({
        'status': 'PENDING',
        'direction': 'NONE',
        'amount_pending': _round_or_none(pending, 4),
        'reason': 'Capital fuera de target',
    })
    return payload


def _build_diagnostics(
    state,
    btc_ctx,
    spot_real,
    futures_real,
    spot_target,
    futures_target,
    total_authorized,
    capital_note,
    rebalance_info,
    longs,
    shorts,
    max_longs,
    max_shorts,
    system_health,
):
    entries_allowed = True
    entries_status = 'ENABLED'
    entries_reason = 'Todo habilitado'
    last_warning = None
    last_error = None

    status = state.get('status')
    pause_until = int(state.get('pause_until') or 0)
    now = int(time.time())
    skip_cycles = int(state.get('skip_next_cycles') or 0)
    force_mode = btc_ctx.get('force_mode') if isinstance(btc_ctx, dict) else None
    trend = btc_ctx.get('trend') if isinstance(btc_ctx, dict) else None
    change_4h = _float_or_none(btc_ctx.get('change_4h')) if isinstance(btc_ctx, dict) else None
    directional = False
    try:
        import config
        directional = bool(getattr(config, 'DIRECTIONAL_MODE', False))
    except Exception:
        directional = False

    long_status = 'ENABLED'
    long_reason = 'Longs habilitados'
    short_status = 'ENABLED'
    short_reason = 'Shorts habilitados'

    if directional and trend == 'bearish':
        long_status = 'BLOCKED'
        long_reason = 'Mercado bearish en modo direccional'
    elif force_mode == 'short_only':
        long_status = 'BLOCKED'
        long_reason = 'Force mode short_only'
    elif max_longs == 0:
        long_status = 'BLOCKED'
        long_reason = 'Sin capital objetivo para Longs'
    elif max_longs is not None and len(longs) >= max_longs:
        long_status = 'BLOCKED'
        if len(longs) > max_longs:
            long_reason = f'Sobrecapacidad actual: Longs {len(longs)}/{max_longs}. No se abriran nuevos longs hasta normalizar.'
        else:
            long_reason = 'Maximo de Longs alcanzado'

    if directional and trend == 'bullish':
        short_status = 'BLOCKED'
        short_reason = 'Mercado bullish en modo direccional'
    elif force_mode == 'long_only':
        short_status = 'BLOCKED'
        short_reason = 'Force mode long_only'
    elif max_shorts == 0:
        short_status = 'BLOCKED'
        short_reason = 'Sin capital objetivo para Shorts'
    elif max_shorts is not None and len(shorts) >= max_shorts:
        short_status = 'BLOCKED'
        if len(shorts) > max_shorts:
            short_reason = f'Sobrecapacidad actual: Shorts {len(shorts)}/{max_shorts}. No se abriran nuevos shorts hasta normalizar.'
        else:
            short_reason = 'Maximo de Shorts alcanzado'

    if (
        rebalance_info.get('status') in {'PENDING', 'BLOCKED'}
        and rebalance_info.get('direction') == 'SPOT_TO_FUTURES'
        and futures_target is not None
        and float(futures_target or 0) > 0
        and float(futures_real or 0) < float(futures_target or 0)
    ):
        short_status = 'WAITING'
        short_reason = f'Esperando capital en Futures: {futures_real or 0:.2f} / objetivo {futures_target:.2f} USDT'
    if (
        rebalance_info.get('status') in {'PENDING', 'BLOCKED'}
        and rebalance_info.get('direction') == 'FUTURES_TO_SPOT'
        and spot_target is not None
        and float(spot_target or 0) > 0
        and float(spot_real or 0) < float(spot_target or 0)
    ):
        long_status = 'WAITING'
        long_reason = f'Esperando capital en Spot: {spot_real or 0:.2f} / objetivo {spot_target:.2f} USDT'

    if status == 'paused':
        entries_allowed = False
        entries_status = 'BLOCKED'
        entries_reason = 'Bot pausado'
    elif pause_until > now:
        entries_allowed = False
        entries_status = 'BLOCKED'
        entries_reason = 'Circuit breaker activo'
    elif skip_cycles > 0:
        entries_allowed = False
        entries_status = 'BLOCKED'
        entries_reason = 'Cooldown post-SL'
    elif total_authorized is None or total_authorized <= 0:
        entries_allowed = False
        entries_status = 'BLOCKED'
        entries_reason = 'Capital insuficiente'
    elif long_status in {'BLOCKED', 'WAITING'} and short_status in {'BLOCKED', 'WAITING'}:
        entries_allowed = False
        entries_status = 'BLOCKED'
        if 'Esperando capital en Futures' in short_reason:
            entries_reason = 'Esperando rebalance hacia Futures para operar shorts'
        elif 'Esperando capital en Spot' in long_reason:
            entries_reason = 'Esperando rebalance hacia Spot para operar longs'
        else:
            entries_reason = 'Entradas bloqueadas por regimen/capital'
    elif long_status in {'BLOCKED', 'WAITING'} or short_status in {'BLOCKED', 'WAITING'}:
        entries_allowed = True
        entries_status = 'PARTIAL'
        entries_reason = 'Entradas parcialmente habilitadas'
    elif force_mode:
        entries_reason = 'Directional mode'
    else:
        entries_reason = 'Todo habilitado'

    if change_4h is not None and abs(change_4h) >= 4:
        if entries_reason == 'Todo habilitado':
            entries_reason = 'BTC movio mas de 4% en 4h'

    if system_health == 'ERROR':
        last_error = 'BotState generado con health ERROR'

    if not entries_allowed:
        if rebalance_info.get('status') == 'PENDING' and rebalance_info.get('direction') == 'SPOT_TO_FUTURES':
            next_expected_action = 'Esperando rebalance Spot -> Futures'
        elif rebalance_info.get('status') == 'PENDING' and rebalance_info.get('direction') == 'FUTURES_TO_SPOT':
            next_expected_action = 'Esperando rebalance Futures -> Spot'
        elif 'BTC' in entries_reason:
            next_expected_action = 'Trading pausado por BTC'
        elif 'posiciones' in entries_reason.lower():
            next_expected_action = 'Esperando cierre de posicion'
        else:
            next_expected_action = entries_reason
    elif rebalance_info.get('status') == 'PENDING':
        if rebalance_info.get('direction') == 'SPOT_TO_FUTURES':
            next_expected_action = 'Esperando rebalance Spot -> Futures'
        elif rebalance_info.get('direction') == 'FUTURES_TO_SPOT':
            next_expected_action = 'Esperando rebalance Futures -> Spot'
        else:
            next_expected_action = 'Esperando rebalance'
    elif force_mode == 'short_only' or (trend == 'bearish' and max_shorts and len(shorts) < max_shorts):
        next_expected_action = 'Esperando señal SHORT'
    elif force_mode == 'long_only' or (trend == 'bullish' and max_longs and len(longs) < max_longs):
        next_expected_action = 'Esperando señal LONG'
    else:
        next_expected_action = 'Sin acciones pendientes'

    return {
        'entries_allowed': bool(entries_allowed),
        'entries_status': entries_status,
        'entries_reason': entries_reason,
        'long_entries_status': long_status,
        'long_entries_reason': long_reason,
        'short_entries_status': short_status,
        'short_entries_reason': short_reason,
        'rebalance_status': rebalance_info.get('status'),
        'rebalance_reason': rebalance_info.get('reason'),
        'next_expected_action': next_expected_action,
        'capital_note': capital_note,
        'last_warning': last_warning,
        'last_error': last_error,
    }


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
    capital_note = None
    if total_real is None or total_limit is None or total_limit <= 0:
        total_authorized = None
        if total_limit is None or total_limit <= 0:
            warning = 'BOT_TOTAL_CAPITAL_LIMIT_USDT missing or invalid'
    else:
        total_authorized = min(total_real, total_limit)
        if total_real < total_limit:
            capital_note = 'Capital disponible menor al limite configurado; usando capital disponible.'

    if spot_target is None or futures_target is None:
        spot_target, futures_target = calculate_targets(total_authorized, btc_ctx, config, rebalance)

    max_longs, max_shorts = compute_observable_max_positions(
        spot_target=spot_target,
        futures_target=futures_target,
        max_longs=max_longs,
        max_shorts=max_shorts,
    )

    rebalance_info = _build_rebalance_diagnostics(
        spot_real=spot_real,
        futures_real=futures_real,
        spot_target=spot_target,
        futures_target=futures_target,
        spot_used=spot_used,
        futures_used=futures_used,
        total_authorized=total_authorized,
        capital_note=capital_note or warning,
    )

    diagnostics = _build_diagnostics(
        state=state,
        btc_ctx=btc_ctx,
        spot_real=spot_real,
        futures_real=futures_real,
        spot_target=spot_target,
        futures_target=futures_target,
        total_authorized=total_authorized,
        capital_note=capital_note,
        rebalance_info=rebalance_info,
        longs=longs,
        shorts=shorts,
        max_longs=max_longs,
        max_shorts=max_shorts,
        system_health=system_health,
    )

    wallet_min = _rebalance_wallet_min()
    spot_reserved = _reserved_wallet_amount(spot_real, spot_target, wallet_min)
    futures_reserved = _reserved_wallet_amount(futures_real, futures_target, wallet_min)

    cycle_timestamp = _now_iso()
    last_execution = cycle_timestamp
    live_system = get_system_statuses()
    if live_system.get('bot') != 'UNKNOWN':
        bot_status = live_system.get('bot')
    if live_system.get('guardian') != 'UNKNOWN':
        guardian_status = live_system.get('guardian')
    if live_system.get('dashboard') != 'UNKNOWN':
        dashboard_status = live_system.get('dashboard')
    return {
        'timestamp': cycle_timestamp,
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
            'spot_reserved': _round_or_none(spot_reserved, 4),
            'futures_reserved': _round_or_none(futures_reserved, 4),
            'spot_available_for_bot': _round_or_none(None if spot_target is None else max(0.0, spot_target - spot_used), 4),
            'futures_available_for_bot': _round_or_none(None if futures_target is None else max(0.0, futures_target - futures_used), 4),
            'warning': warning,
            'note': capital_note,
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
        'diagnostics': diagnostics,
        'rebalance': rebalance_info,
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
