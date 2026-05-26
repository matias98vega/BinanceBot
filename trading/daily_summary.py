#!/usr/bin/env python3
"""
Resumen diario del bot de trading. Corre via cron a medianoche UTC.
"""
import json, urllib.request, urllib.parse, hmac, hashlib, time, math

STATE_FILE = '/root/.openclaw/workspace/trading/state.json'
TRADES_LOG = '/root/.openclaw/workspace/trading/trades_log.txt'
TOOLS_KEY  = '0DwLCZ1RnGhfnWygp3PUxPrLGLjLByukBFvjEo06p5fVQpsICjdcKBLBRwXzOnVr'
TOOLS_SEC  = 'VCMhz7vCQZGgwAIV4PDY74bpRGOxDY0gT4rh6a5cLJmh2mCfcJF1uQu3qhzcQWmM'
BASE       = 'https://api.binance.com'

def signed_request(path, params=None):
    params = params or {}
    params['timestamp'] = int(time.time() * 1000)
    qs = urllib.parse.urlencode(params)
    sig = hmac.new(TOOLS_SEC.encode(), qs.encode(), hashlib.sha256).hexdigest()
    url = f"{BASE}{path}?{qs}&signature={sig}"
    req = urllib.request.Request(url, headers={'X-MBX-APIKEY': TOOLS_KEY})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def get_price(symbol):
    with urllib.request.urlopen(f"{BASE}/api/v3/ticker/price?symbol={symbol}", timeout=8) as r:
        return float(json.loads(r.read())['price'])

def main():
    state = json.load(open(STATE_FILE))
    today = time.strftime('%Y-%m-%d', time.gmtime())

    # Leer trades del dia de hoy en el log
    trades_hoy = []
    try:
        with open(TRADES_LOG) as f:
            for line in f:
                if line.startswith('#') or not line.strip():
                    continue
                if today in line:
                    trades_hoy.append(line.strip())
    except:
        pass

    # Balance real de la cuenta
    account = signed_request('/api/v3/account')
    balances = {b['asset']: float(b['free']) + float(b['locked'])
                for b in account['balances']
                if float(b['free']) + float(b['locked']) > 0.0001}

    total_usdt = balances.get('USDT', 0)
    for asset, qty in balances.items():
        if asset == 'USDT': continue
        try:
            total_usdt += qty * get_price(f'{asset}USDT')
        except:
            pass

    # PnL del dia (desde el estado)
    pnl_total = state.get('total_pnl_usdt', 0)
    capital   = state.get('capital_usdt', 0)
    status    = state.get('status', '?')
    sym       = state.get('symbol', '-')

    lines = [
        f"📊 Resumen diario — {today}",
        f"{'─'*35}",
        f"💰 Balance real cuenta: ${total_usdt:.4f} USDT",
        f"📈 PnL acumulado (desde reset): {'+' if pnl_total>=0 else ''}{pnl_total:.4f} USDT",
        f"🤖 Estado bot: {status}" + (f" | Par: {sym}" if status == 'in_position' else ""),
    ]

    if trades_hoy:
        lines.append(f"{'─'*35}")
        lines.append(f"Trades cerrados hoy ({len(trades_hoy)}):")
        for t in trades_hoy:
            lines.append(f"  {t}")
    else:
        lines.append("Sin trades cerrados hoy.")

    lines.append(f"{'─'*35}")
    print('\n'.join(lines))

if __name__ == '__main__':
    main()
