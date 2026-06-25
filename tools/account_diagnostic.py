#!/usr/bin/env python3
"""Read-only Binance account diagnostic for spot/futures/margin funds."""
import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADING_DIR = os.path.join(PROJECT_DIR, 'trading')
sys.path.insert(0, TRADING_DIR)

from config_loader import validate_environment  # noqa: E402


def _request_json(req_or_url, timeout=15):
    with urllib.request.urlopen(req_or_url, timeout=timeout) as response:
        body = response.read().decode('utf-8')
    return json.loads(body) if body else {}


def _server_time(base):
    path = '/fapi/v1/time' if 'fapi' in base else '/api/v3/time'
    try:
        return int(_request_json(f'{base}{path}')['serverTime'])
    except Exception:
        return int(time.time() * 1000)


def _signed(config, base, method, path, params=None):
    params = dict(params or {})
    params['timestamp'] = _server_time(base)
    params.setdefault('recvWindow', 10000)
    query = urllib.parse.urlencode(params)
    signature = hmac.new(config.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    full_query = f'{query}&signature={signature}'
    if method in {'GET', 'DELETE'}:
        url = f'{base}{path}?{full_query}'
        data = None
    else:
        url = f'{base}{path}'
        data = full_query.encode()
    req = urllib.request.Request(url, data=data, method=method, headers={
        'X-MBX-APIKEY': config.api_key,
        'Content-Type': 'application/x-www-form-urlencoded',
        'User-Agent': 'BinanceBotDiagnostic/1.0',
    })
    return _request_json(req)


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _is_nonzero(value):
    return abs(_float(value)) > 0


def _safe_call(label, fn):
    try:
        return fn(), None
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode('utf-8')
        except Exception:
            body = str(exc)
        return None, f'{label}: HTTP {exc.code}: {body}'
    except Exception as exc:
        return None, f'{label}: {exc}'


def _spot_price(config, symbol):
    try:
        data = _request_json(f'{config.spot_base}/api/v3/ticker/price?symbol={urllib.parse.quote(symbol)}')
        return _float(data.get('price'))
    except Exception:
        return None


def _estimate_usdt(config, asset, amount):
    amount = _float(amount)
    if asset == 'USDT':
        return amount
    if amount == 0:
        return 0.0
    price = _spot_price(config, f'{asset}USDT')
    if price is None:
        return None
    return amount * price


def _print_section(title):
    print('')
    print(title)
    print('-' * len(title))


def _print_rows(rows, columns):
    if not rows:
        print('N/A')
        return
    print(' | '.join(columns))
    for row in rows:
        print(' | '.join(str(row.get(col, '')) for col in columns))


def collect(config):
    errors = []

    spot_account, err = _safe_call('spot account', lambda: _signed(config, config.spot_base, 'GET', '/api/v3/account'))
    if err:
        errors.append(err)
        spot_account = {}

    spot_open_orders, err = _safe_call('spot open orders', lambda: _signed(config, config.spot_base, 'GET', '/api/v3/openOrders'))
    if err:
        errors.append(err)
        spot_open_orders = []

    futures_account, err = _safe_call('futures account', lambda: _signed(config, config.futures_base, 'GET', '/fapi/v2/account'))
    if err:
        errors.append(err)
        futures_account = {}

    futures_positions, err = _safe_call('futures positions', lambda: _signed(config, config.futures_base, 'GET', '/fapi/v2/positionRisk'))
    if err:
        errors.append(err)
        futures_positions = []

    futures_open_orders, err = _safe_call('futures open orders', lambda: _signed(config, config.futures_base, 'GET', '/fapi/v1/openOrders'))
    if err:
        errors.append(err)
        futures_open_orders = []

    margin_account, err = _safe_call('cross margin account', lambda: _signed(config, config.spot_base, 'GET', '/sapi/v1/margin/account'))
    if err:
        errors.append(err)
        margin_account = {}

    spot_assets = []
    for balance in spot_account.get('balances', []):
        free = _float(balance.get('free'))
        locked = _float(balance.get('locked'))
        if free or locked:
            asset = balance.get('asset')
            total = free + locked
            spot_assets.append({
                'asset': asset,
                'free': free,
                'locked': locked,
                'estimated_usdt': _estimate_usdt(config, asset, total),
            })

    futures_balances = []
    for asset in futures_account.get('assets', []):
        if any(_is_nonzero(asset.get(field)) for field in ('walletBalance', 'unrealizedProfit', 'marginBalance', 'availableBalance')):
            futures_balances.append({
                'asset': asset.get('asset'),
                'walletBalance': asset.get('walletBalance'),
                'availableBalance': asset.get('availableBalance'),
                'unrealizedProfit': asset.get('unrealizedProfit'),
                'marginBalance': asset.get('marginBalance'),
            })

    open_positions = []
    for pos in futures_positions:
        if _is_nonzero(pos.get('positionAmt')):
            open_positions.append({
                'symbol': pos.get('symbol'),
                'positionAmt': pos.get('positionAmt'),
                'entryPrice': pos.get('entryPrice'),
                'markPrice': pos.get('markPrice'),
                'unRealizedProfit': pos.get('unRealizedProfit'),
                'liquidationPrice': pos.get('liquidationPrice'),
                'marginType': pos.get('marginType'),
                'isolatedMargin': pos.get('isolatedMargin'),
                'leverage': pos.get('leverage'),
            })

    margin_assets = []
    for asset in margin_account.get('userAssets', []):
        fields = ('free', 'locked', 'borrowed', 'interest', 'netAsset')
        if any(_is_nonzero(asset.get(field)) for field in fields):
            margin_assets.append({
                'asset': asset.get('asset'),
                'free': asset.get('free'),
                'locked': asset.get('locked'),
                'borrowed': asset.get('borrowed'),
                'interest': asset.get('interest'),
                'netAsset': asset.get('netAsset'),
            })

    possible_locked = bool(
        any(_float(row.get('locked')) > 0 for row in spot_assets)
        or spot_open_orders
        or open_positions
        or futures_open_orders
        or any(_float(row.get('locked')) > 0 or _float(row.get('borrowed')) > 0 or _float(row.get('interest')) > 0 for row in margin_assets)
    )

    return {
        'spot_assets': spot_assets,
        'spot_open_orders': spot_open_orders,
        'futures_account': futures_account,
        'futures_balances': futures_balances,
        'futures_positions': open_positions,
        'futures_open_orders': futures_open_orders,
        'margin_assets': margin_assets,
        'errors': errors,
        'summary': {
            'spot_non_zero_assets': len(spot_assets),
            'spot_open_orders': len(spot_open_orders),
            'futures_non_zero_balances': len(futures_balances),
            'futures_open_positions': len(open_positions),
            'futures_open_orders': len(futures_open_orders),
            'margin_non_zero_assets': len(margin_assets),
            'possible_locked_funds': possible_locked,
        },
    }


def print_report(data):
    _print_section('SPOT BALANCES')
    _print_rows(data['spot_assets'], ['asset', 'free', 'locked', 'estimated_usdt'])

    _print_section('SPOT OPEN ORDERS')
    _print_rows([
        {
            'symbol': order.get('symbol'),
            'side': order.get('side'),
            'type': order.get('type'),
            'price': order.get('price'),
            'origQty': order.get('origQty'),
            'executedQty': order.get('executedQty'),
            'status': order.get('status'),
        }
        for order in data['spot_open_orders']
    ], ['symbol', 'side', 'type', 'price', 'origQty', 'executedQty', 'status'])

    _print_section('FUTURES USDT-M ACCOUNT')
    account = data['futures_account']
    print(f"availableBalance: {account.get('availableBalance', 'N/A')}")
    print(f"totalWalletBalance: {account.get('totalWalletBalance', 'N/A')}")
    print(f"totalUnrealizedProfit: {account.get('totalUnrealizedProfit', 'N/A')}")
    print(f"totalMarginBalance: {account.get('totalMarginBalance', 'N/A')}")

    _print_section('FUTURES NON-ZERO BALANCES')
    _print_rows(data['futures_balances'], ['asset', 'walletBalance', 'availableBalance', 'unrealizedProfit', 'marginBalance'])

    _print_section('FUTURES OPEN POSITIONS')
    _print_rows(data['futures_positions'], [
        'symbol', 'positionAmt', 'entryPrice', 'markPrice', 'unRealizedProfit',
        'liquidationPrice', 'marginType', 'isolatedMargin', 'leverage',
    ])

    _print_section('FUTURES OPEN ORDERS')
    _print_rows([
        {
            'symbol': order.get('symbol'),
            'side': order.get('side'),
            'type': order.get('type'),
            'price': order.get('price'),
            'origQty': order.get('origQty'),
            'executedQty': order.get('executedQty'),
            'reduceOnly': order.get('reduceOnly'),
            'closePosition': order.get('closePosition'),
            'status': order.get('status'),
        }
        for order in data['futures_open_orders']
    ], ['symbol', 'side', 'type', 'price', 'origQty', 'executedQty', 'reduceOnly', 'closePosition', 'status'])

    _print_section('MARGIN / CROSS')
    _print_rows(data['margin_assets'], ['asset', 'free', 'locked', 'borrowed', 'interest', 'netAsset'])

    if data['errors']:
        _print_section('READ ERRORS')
        for error in data['errors']:
            print(f'- {error}')

    summary = data['summary']
    print('')
    print('ACCOUNT DIAGNOSTIC SUMMARY')
    print(f"- spot non-zero assets: {summary['spot_non_zero_assets']}")
    print(f"- spot open orders: {summary['spot_open_orders']}")
    print(f"- futures non-zero balances: {summary['futures_non_zero_balances']}")
    print(f"- futures open positions: {summary['futures_open_positions']}")
    print(f"- futures open orders: {summary['futures_open_orders']}")
    print(f"- margin non-zero assets: {summary['margin_non_zero_assets']}")
    print(f"- possible locked funds: {'YES' if summary['possible_locked_funds'] else 'NO'}")


def main():
    parser = argparse.ArgumentParser(description='Read-only Binance account diagnostic')
    parser.add_argument('--export-json', help='Optional path to export the diagnostic JSON')
    args = parser.parse_args()

    config = validate_environment(require_api=True)
    data = collect(config)
    print_report(data)

    if args.export_json:
        with open(args.export_json, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write('\n')

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
