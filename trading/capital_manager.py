#!/usr/bin/env python3
"""Capital limit guardrails for order placement and wallet transfers."""
import os
from dataclasses import dataclass

from config_loader import ConfigError, load_dotenv


CAPITAL_ENV_VARS = (
    'BOT_SPOT_CAPITAL_LIMIT_USDT',
    'BOT_FUTURES_CAPITAL_LIMIT_USDT',
    'BOT_MAX_POSITION_PERCENT',
    'BOT_MAX_EXPOSURE_PERCENT',
)


class CapitalLimitError(RuntimeError):
    pass


@dataclass(frozen=True)
class CapitalLimits:
    spot_capital_limit_usdt: float
    futures_capital_limit_usdt: float
    max_position_percent: float
    max_exposure_percent: float


def _float_env(name):
    raw = os.environ.get(name)
    if raw is None or raw == '':
        raise CapitalLimitError(f'Missing required environment variable: {name}')
    try:
        value = float(raw)
    except ValueError as exc:
        raise CapitalLimitError(f'Invalid numeric value for {name}: {raw}') from exc
    return value


def get_limits():
    load_dotenv()
    spot_limit = _float_env('BOT_SPOT_CAPITAL_LIMIT_USDT')
    futures_limit = _float_env('BOT_FUTURES_CAPITAL_LIMIT_USDT')
    max_position = _float_env('BOT_MAX_POSITION_PERCENT')
    max_exposure = _float_env('BOT_MAX_EXPOSURE_PERCENT')

    if spot_limit <= 0:
        raise CapitalLimitError('BOT_SPOT_CAPITAL_LIMIT_USDT must be > 0')
    if futures_limit <= 0:
        raise CapitalLimitError('BOT_FUTURES_CAPITAL_LIMIT_USDT must be > 0')
    if max_position <= 0 or max_position > 100:
        raise CapitalLimitError('BOT_MAX_POSITION_PERCENT must be > 0 and <= 100')
    if max_exposure <= 0 or max_exposure > 100:
        raise CapitalLimitError('BOT_MAX_EXPOSURE_PERCENT must be > 0 and <= 100')

    return CapitalLimits(
        spot_capital_limit_usdt=spot_limit,
        futures_capital_limit_usdt=futures_limit,
        max_position_percent=max_position,
        max_exposure_percent=max_exposure,
    )


def spot_usable_capital(spot_real_usdt, limits=None):
    limits = limits or get_limits()
    return min(max(float(spot_real_usdt or 0), 0.0), limits.spot_capital_limit_usdt)


def futures_usable_capital(futures_real_usdt, limits=None):
    limits = limits or get_limits()
    return min(max(float(futures_real_usdt or 0), 0.0), limits.futures_capital_limit_usdt)


def max_position_size(usable_capital, limits=None):
    limits = limits or get_limits()
    return max(float(usable_capital or 0), 0.0) * limits.max_position_percent / 100.0


def max_exposure(usable_capital, limits=None):
    limits = limits or get_limits()
    return max(float(usable_capital or 0), 0.0) * limits.max_exposure_percent / 100.0


def open_spot_exposure(state):
    return sum(
        float(p.get('entry_price', 0)) * float(p.get('quantity', 0))
        for p in state.get('positions', [])
        if isinstance(p, dict) and p.get('direction') == 'long'
    )


def open_futures_exposure(state):
    exposure = 0.0
    for pos in state.get('positions', []):
        if not isinstance(pos, dict) or pos.get('direction') != 'short':
            continue
        leverage = float(pos.get('leverage') or 1)
        if leverage <= 0:
            leverage = 1
        exposure += float(pos.get('entry_price', 0)) * float(pos.get('quantity', 0)) / leverage
    return exposure


def _validation_payload(wallet, real_balance, usable, current_exposure, requested_size, limits):
    return {
        'wallet': wallet,
        'real_balance': round(float(real_balance or 0), 8),
        'usable_capital': round(usable, 8),
        'current_exposure': round(current_exposure, 8),
        'requested_size': round(float(requested_size or 0), 8),
        'max_position_size': round(max_position_size(usable, limits), 8),
        'max_exposure': round(max_exposure(usable, limits), 8),
        'max_position_percent': limits.max_position_percent,
        'max_exposure_percent': limits.max_exposure_percent,
    }


def _validate(wallet, real_balance, usable, current_exposure, requested_size, limits):
    payload = _validation_payload(wallet, real_balance, usable, current_exposure, requested_size, limits)
    requested = float(requested_size or 0)
    epsilon = 1e-8

    if usable <= 0:
        return False, f'CAPITAL LIMIT REJECT {wallet}: usable capital is ${usable:.2f}', payload
    if requested <= 0:
        return False, f'CAPITAL LIMIT REJECT {wallet}: requested size is ${requested:.2f}', payload
    if requested > payload['max_position_size'] + epsilon:
        return False, (
            f'CAPITAL LIMIT REJECT {wallet}: requested ${requested:.2f} exceeds '
            f'max position ${payload["max_position_size"]:.2f} '
            f'({limits.max_position_percent:.2f}% of usable ${usable:.2f})'
        ), payload
    if current_exposure + requested > payload['max_exposure'] + epsilon:
        return False, (
            f'CAPITAL LIMIT REJECT {wallet}: exposure ${current_exposure + requested:.2f} exceeds '
            f'max exposure ${payload["max_exposure"]:.2f} '
            f'({limits.max_exposure_percent:.2f}% of usable ${usable:.2f})'
        ), payload
    return True, 'OK', payload


def validate_spot_order(state, spot_real_usdt, requested_usdt):
    limits = get_limits()
    usable = spot_usable_capital(spot_real_usdt, limits)
    return _validate('SPOT', spot_real_usdt, usable, open_spot_exposure(state), requested_usdt, limits)


def validate_futures_order(state, futures_real_usdt, requested_margin_usdt):
    limits = get_limits()
    usable = futures_usable_capital(futures_real_usdt, limits)
    return _validate('FUTURES', futures_real_usdt, usable, open_futures_exposure(state), requested_margin_usdt, limits)


def cap_transfer_amount(destination_wallet, current_destination_real, requested_amount):
    limits = get_limits()
    if destination_wallet == 'SPOT':
        limit = limits.spot_capital_limit_usdt
    elif destination_wallet == 'FUTURES':
        limit = limits.futures_capital_limit_usdt
    else:
        raise CapitalLimitError(f'Unknown destination wallet: {destination_wallet}')
    room = max(0.0, limit - float(current_destination_real or 0))
    return max(0.0, min(float(requested_amount or 0), room))


def snapshot(spot_real_usdt=None, futures_real_usdt=None):
    limits = get_limits()
    spot_real = float(spot_real_usdt or 0)
    futures_real = float(futures_real_usdt or 0)
    spot_usable = spot_usable_capital(spot_real, limits)
    futures_usable = futures_usable_capital(futures_real, limits)
    return {
        'spot_real': spot_real,
        'spot_usable': spot_usable,
        'futures_real': futures_real,
        'futures_usable': futures_usable,
        'spot_capital_limit_usdt': limits.spot_capital_limit_usdt,
        'futures_capital_limit_usdt': limits.futures_capital_limit_usdt,
        'max_position_percent': limits.max_position_percent,
        'max_exposure_percent': limits.max_exposure_percent,
        'spot_max_position': max_position_size(spot_usable, limits),
        'spot_max_exposure': max_exposure(spot_usable, limits),
        'futures_max_position': max_position_size(futures_usable, limits),
        'futures_max_exposure': max_exposure(futures_usable, limits),
    }


def validate_environment():
    try:
        return True, get_limits()
    except (CapitalLimitError, ConfigError) as exc:
        return False, str(exc)
