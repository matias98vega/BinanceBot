#!/usr/bin/env python3
"""
Resumen diario del bot de trading. Corre via cron a medianoche UTC.
"""
import json, os, urllib.request, urllib.parse, hmac, hashlib, time, math

STATE_FILE = '/root/.openclaw/workspace/trading/state.json'
TRADES_LOG = '/root/.openclaw/workspace/trading/trades_log.txt'
# Credenciales desde .env (nunca hardcodear en el codigo)
def _load_env():
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    env = {}
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return env
_env = _load_env()
TOOLS_KEY  = _env.get('BINANCE_API_KEY', os.environ.get('BINANCE_API_KEY', ''))
TOOLS_SEC  = _env.get('BINANCE_API_SECRET', os.environ.get('BINANCE_API_SECRET', ''))
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
    today = time.strftime('%Y-%m-%d', time.gmtime(time.time() - 3*3600))

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

    # PnL del dia (usar daily_pnl_usdt, no el acumulado historico)
    pnl_dia   = state.get('daily_pnl_usdt', 0.0)
    pnl_total = state.get('total_pnl_usdt', 0.0)
    capital   = state.get('capital_usdt', 0)
    status    = state.get('status', '?')
    sym       = state.get('symbol', '-')

    # Evaluacion del dia
    if not trades_hoy:
        eval_dia = 'Sin trades hoy.'
    elif pnl_dia >= 0:
        eval_dia = f'✅ Buen día — PnL del día: +${pnl_dia:.4f} USDT'
    elif pnl_dia >= -0.10:
        eval_dia = f'⚠️ Día neutro — pérdida mínima: ${pnl_dia:.4f} USDT'
    else:
        eval_dia = f'🔴 Día difícil — {len(trades_hoy)} trades, PnL: ${pnl_dia:.4f} USDT'

    lines = [
        f"📊 Resumen diario — {today}",
        f"{'─'*35}",
        f"💰 Balance real cuenta: ${total_usdt:.4f} USDT",
        f"📈 PnL hoy: {'+' if pnl_dia>=0 else ''}{pnl_dia:.4f} USDT",
        f"📉 PnL acumulado total: {'+' if pnl_total>=0 else ''}{pnl_total:.4f} USDT",
        f"🤖 Estado bot: {status}" + (f" | Par: {sym}" if status == 'in_position' else ""),
    ]

    if trades_hoy:
        lines.append(f"{'─'*35}")
        lines.append(f"Trades cerrados hoy ({len(trades_hoy)}):")
        for t in trades_hoy:
            parts = [p.strip() for p in t.split('|')]
            if len(parts) >= 5:
                par, res, pnl, cap, hora = parts[1], parts[2], parts[3], parts[4], parts[5] if len(parts) > 5 else ''
                lines.append(f"  • {par} | {res} | {pnl} | {cap} | {hora}")
            else:
                lines.append(f"  • {t}")
    else:
        lines.append("Sin trades cerrados hoy.")

    lines.append(f"{'─'*35}")
    lines.append(eval_dia)
    print('\n'.join(lines))

if __name__ == '__main__':
    main()
