#!/usr/bin/env python3
"""Read-only Binance API permission diagnostic using project config/utils."""
import json
import os
import sys
import urllib.error


PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADING_DIR = os.path.join(PROJECT_DIR, 'trading')
sys.path.insert(0, TRADING_DIR)

from config_loader import validate_environment  # noqa: E402
import utils  # noqa: E402


def _parse_error(exc):
    body = ''
    try:
        body = exc.read().decode('utf-8')
    except Exception:
        body = ''
    code = None
    msg = str(exc)
    if body:
        try:
            data = json.loads(body)
            code = data.get('code')
            msg = data.get('msg', msg)
        except json.JSONDecodeError:
            msg = body
    return code, msg


def _print_result(label, call):
    print(label)
    try:
        call()
        print('HTTP Status: 200')
        print('Binance Code: OK')
        print('Binance Message: OK')
    except urllib.error.HTTPError as exc:
        code, msg = _parse_error(exc)
        print(f'HTTP Status: {exc.code}')
        print(f'Binance Code: {code if code is not None else "N/A"}')
        print(f'Binance Message: {msg}')
    except Exception as exc:
        print('HTTP Status: N/A')
        print('Binance Code: N/A')
        print(f'Binance Message: {exc}')
    print('')


def main():
    try:
        validate_environment(require_api=True)
    except Exception as exc:
        print('CONFIG')
        print('HTTP Status: N/A')
        print('Binance Code: N/A')
        print(f'Binance Message: {exc}')
        return 1

    _print_result('SPOT GET /api/v3/account', lambda: utils.spot_signed('GET', '/api/v3/account'))
    _print_result('USDT-M FUTURES GET /fapi/v2/account', lambda: utils.fut_signed('GET', '/fapi/v2/account'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
