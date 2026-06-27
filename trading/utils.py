#!/usr/bin/env python3
"""
Helpers compartidos: HTTP, firma, alertas, logs, lock.
"""
import hmac, hashlib, time, urllib.request, urllib.parse, urllib.error, re, logging
import json, os, sys, subprocess, socket
from datetime import datetime, timezone

import config

# ── Parseo de errores Binance ─────────────────────────────────────────────────
def _binance_error_msg(http_err):
    """
    Lee el body de un HTTPError de Binance y retorna un string legible.
    Ejemplo: 'code=-2019 msg=Margin is insufficient.'
    """
    details = extract_http_error_details(http_err)
    if details.get('code') is not None or details.get('msg'):
        return f'HTTP {details["status"]} code={details.get("code", "?")} msg={details.get("msg", "")}'
    if details.get('raw_body'):
        return f'HTTP {details["status"]} body={details["raw_body"]}'
    return str(http_err)


def extract_http_error_details(http_err):
    status = getattr(http_err, 'code', None) or getattr(http_err, 'status', None)
    raw_body = ''
    try:
        raw = http_err.read()
        raw_body = raw.decode('utf-8', errors='replace') if isinstance(raw, bytes) else str(raw)
    except Exception:
        raw_body = ''
    details = {
        'status': status,
        'reason': getattr(http_err, 'reason', None),
        'code': None,
        'msg': None,
        'raw_body': raw_body[:1000] if raw_body else '',
    }
    if raw_body:
        try:
            data = json.loads(raw_body)
            if isinstance(data, dict):
                details['code'] = data.get('code')
                details['msg'] = data.get('msg')
        except Exception:
            pass
    return details


def safe_order_context(params):
    if not isinstance(params, dict):
        return {}
    sensitive = {'signature', 'timestamp', 'recvWindow'}
    return {k: v for k, v in params.items() if k not in sensitive and 'key' not in k.lower() and 'secret' not in k.lower()}


def interpret_binance_error(details, params=None):
    code = details.get('code') if isinstance(details, dict) else None
    msg = str((details or {}).get('msg') or (details or {}).get('raw_body') or '').lower()
    params = params if isinstance(params, dict) else {}
    if code in (-1013, -1111) or 'lot_size' in msg or 'step size' in msg or 'quantity' in msg:
        return 'LOT_SIZE/precision/quantity'
    if 'price_filter' in msg or 'tick size' in msg or 'price' in msg and params.get('price'):
        return 'PRICE_FILTER/price precision'
    if 'min_notional' in msg or 'notional' in msg:
        return 'MIN_NOTIONAL'
    if 'would immediately trigger' in msg or 'stop' in msg and params.get('stopPrice'):
        return 'STOP_PRICE would trigger / invalid stop'
    if 'reduceonly' in msg or 'reduce only' in msg:
        return 'reduceOnly conflict'
    if 'insufficient' in msg or code == -2019:
        return 'insufficient balance/margin'
    if 'position' in msg:
        return 'position state/conflict'
    if code in (-2010, -2021):
        return 'order rejected by Binance filters'
    return 'unknown'


def log_binance_http_error(operation, symbol=None, side=None, order_type=None, params=None, error=None):
    details = extract_http_error_details(error) if error is not None else {}
    context = safe_order_context(params or {})
    interpreted = interpret_binance_error(details, context)
    logging.error(
        'BINANCE HTTP ERROR operation=%s symbol=%s side=%s type=%s status=%s code=%s msg=%s interpreted=%s params=%s raw_body=%s',
        operation,
        symbol or context.get('symbol'),
        side or context.get('side'),
        order_type or context.get('type'),
        details.get('status'),
        details.get('code'),
        details.get('msg'),
        interpreted,
        context,
        details.get('raw_body'),
    )
    details['interpreted_reason'] = interpreted
    return details


def count_positions(state, direction):
    return sum(
        1 for p in state.get('positions', [])
        if isinstance(p, dict) and p.get('direction') == direction
    )


def capacity_reject_message(direction, count, max_positions):
    label = 'longs' if direction == 'long' else 'shorts'
    return f'CAPACITY LIMIT REJECT: {label} {count}/{max_positions}'


def validate_position_capacity(state, direction, max_positions):
    try:
        limit = int(max_positions)
    except (TypeError, ValueError):
        limit = 0
    count = count_positions(state, direction)
    if limit <= 0 or count >= limit:
        return False, capacity_reject_message(direction, count, limit), count, limit
    return True, 'OK', count, limit


# ── HTTP con retry ────────────────────────────────────────────────────────────
def _urlopen(req_or_url, timeout=10):
    last_err = None
    for attempt in range(1, config.NET_RETRIES + 1):
        try:
            with urllib.request.urlopen(req_or_url, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (429, 418):  # rate limit
                time.sleep(10 * attempt)
            elif e.code in (500, 502, 503, 504):  # error servidor Binance
                time.sleep(config.NET_RETRY_DELAY * attempt)
            else:
                raise  # errores 4xx (bad request, etc.) no tiene sentido reintentar
        except (urllib.error.URLError, socket.timeout, OSError) as e:
            last_err = e
            if attempt < config.NET_RETRIES:
                time.sleep(config.NET_RETRY_DELAY * attempt)
    raise last_err

def _server_time(base):
    path = '/fapi/v1/time' if 'fapi' in base else '/api/v3/time'
    req = urllib.request.Request(f'{base}{path}', headers={'User-Agent': 'Mozilla/5.0'})
    return _urlopen(req)['serverTime']

def _sign(params, secret):
    qs = urllib.parse.urlencode(params)
    sig = hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    return f'{qs}&signature={sig}'

# ── Spot ─────────────────────────────────────────────────────────────────────
def spot_public(path, params=None):
    qs = urllib.parse.urlencode(params or {})
    url = f'{config.SPOT_BASE}{path}?{qs}' if qs else f'{config.SPOT_BASE}{path}'
    return _urlopen(url)

def spot_signed(method, path, params=None):
    params = params or {}
    try:
        params['timestamp'] = _server_time(config.SPOT_BASE)
    except Exception:
        params['timestamp'] = int(time.time() * 1000)
    params.setdefault('recvWindow', 10000)
    full_qs = _sign(params, config.API_SECRET)
    if method in ('GET', 'DELETE'):
        url  = f'{config.SPOT_BASE}{path}?{full_qs}'
        data = None
    else:
        url  = f'{config.SPOT_BASE}{path}'
        data = full_qs.encode()
    req = urllib.request.Request(url, data=data, method=method,
          headers={'X-MBX-APIKEY': config.API_KEY,
                   'Content-Type': 'application/x-www-form-urlencoded'})
    return _urlopen(req)

# ── Futures ──────────────────────────────────────────────────────────────────
def fut_public(path, params=None):
    qs = urllib.parse.urlencode(params or {})
    url = f'{config.FUTURES_BASE}{path}?{qs}' if qs else f'{config.FUTURES_BASE}{path}'
    return _urlopen(url)

def fut_signed(method, path, params=None):
    params = params or {}
    try:
        params['timestamp'] = _server_time(config.FUTURES_BASE)
    except Exception:
        params['timestamp'] = int(time.time() * 1000)
    params.setdefault('recvWindow', 10000)
    full_qs = _sign(params, config.API_SECRET)
    if method in ('GET', 'DELETE'):
        url  = f'{config.FUTURES_BASE}{path}?{full_qs}'
        data = None
    else:
        url  = f'{config.FUTURES_BASE}{path}'
        data = full_qs.encode()
    req = urllib.request.Request(url, data=data, method=method,
          headers={'X-MBX-APIKEY': config.API_KEY,
                   'Content-Type': 'application/x-www-form-urlencoded'})
    return _urlopen(req)

# ── Precio ───────────────────────────────────────────────────────────────────
def get_spot_price(symbol):
    d = spot_public('/api/v3/ticker/price', {'symbol': symbol})
    return float(d['price'])

def get_fut_price(symbol):
    d = fut_public('/fapi/v1/ticker/price', {'symbol': symbol})
    return float(d['price'])

# ── Balance ──────────────────────────────────────────────────────────────────
def get_spot_account(retries=3):
    """Obtiene el account spot con reintentos. Centraliza todos los llamados a /api/v3/account."""
    for attempt in range(retries):
        try:
            return spot_signed('GET', '/api/v3/account')
        except Exception as e:
            if attempt < retries - 1:
                import time as _t
                _t.sleep(3 * (attempt + 1))
            else:
                raise
    return {}

def get_usdt_spot():
    d = get_spot_account()
    for b in d.get('balances', []):
        if b['asset'] == 'USDT':
            return float(b['free'])
    return 0.0

def get_asset_spot(asset):
    d = get_spot_account()
    for b in d.get('balances', []):
        if b['asset'] == asset:
            return float(b['free'])
    return 0.0

def get_usdt_futures():
    """Retorna walletBalance + uPnL — balance real incluyendo posiciones abiertas."""
    d = fut_signed('GET', '/fapi/v2/account')
    wallet  = float(d.get('totalWalletBalance', 0))
    upnl    = float(d.get('totalUnrealizedProfit', 0))
    return wallet + upnl

def get_total_futures():
    """Alias de get_usdt_futures para compatibilidad."""
    return get_usdt_futures()

def get_futures_summary():
    """Retorna (wallet_total, disponible, en_margen) de la cuenta futures."""
    import urllib.error, time
    
    d = None
    last_err = None
    for _attempt in range(3):
        try:
            d = fut_signed('GET', '/fapi/v2/account', {})
            break
        except urllib.error.HTTPError as e:
            last_err = e
            if _attempt < 2:
                _delay = 5 * (_attempt + 1)  # 5s, 10s
                import logging
                logging.warning(f'Futures summary: intento {_attempt+1} fallido ({e}), reintentando en {_delay}s')
                time.sleep(_delay)
    if d is None:
        raise last_err
    
    total     = float(d.get('totalWalletBalance', 0))
    available = float(d.get('availableBalance', 0))
    in_margin = float(d.get('totalInitialMargin', 0))
    return total, available, in_margin

# ── Exchange info ─────────────────────────────────────────────────────────────
_spot_info_cache    = {}
_futures_info_cache = {}

def get_spot_filters(symbol):
    if symbol not in _spot_info_cache:
        _spot_info_cache[symbol] = spot_public('/api/v3/exchangeInfo', {'symbol': symbol})
    info = _spot_info_cache[symbol]
    filters = info['symbols'][0]['filters']
    result = {}
    for f in filters:
        if f['filterType'] == 'LOT_SIZE':
            result['step_size'] = float(f['stepSize'])
            result['min_qty']   = float(f['minQty'])
        if f['filterType'] == 'MIN_NOTIONAL':
            result['min_notional'] = float(f.get('minNotional', f.get('notional', 5)))
        if f['filterType'] == 'PRICE_FILTER':
            result['tick_size'] = float(f['tickSize'])
    return result

def get_futures_filters(symbol):
    if symbol not in _futures_info_cache:
        # Pedir solo el símbolo específico para no cargar todo el exchange info
        info = fut_public('/fapi/v1/exchangeInfo', {'symbol': symbol})
        for s in info.get('symbols', []):
            if s['symbol'] == symbol:
                result = {}
                for f in s.get('filters', []):
                    if f['filterType'] == 'LOT_SIZE':
                        result['step_size'] = float(f['stepSize'])
                        result['min_qty']   = float(f['minQty'])
                    if f['filterType'] == 'MIN_NOTIONAL':
                        result['min_notional'] = float(f.get('notional', 5))
                    if f['filterType'] == 'PRICE_FILTER':
                        result['tick_size'] = float(f['tickSize'])
                _futures_info_cache[symbol] = result
                break
        else:
            return {}
    return _futures_info_cache[symbol]

def round_step(qty, step):
    if step == 0:
        return qty
    import math
    precision = max(0, -int(round(math.log10(step))))
    return round(math.floor(qty / step) * step, precision)

def round_tick(price, tick):
    if tick == 0:
        return price
    import math
    precision = max(0, -int(round(math.log10(tick))))
    return round(round(price / tick) * tick, precision)

# ── State ─────────────────────────────────────────────────────────────────────
def load_state():
    if os.path.exists(config.STATE_FILE):
        with open(config.STATE_FILE, encoding='utf-8') as f:
            return json.load(f)
    return _default_state()

def save_state(s):
    s['last_update'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    with open(config.STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(s, f, indent=2)

def _default_state():
    return {
        'positions': [],          # lista de posiciones activas
        'trade_count': 0,
        'total_pnl_usdt': 0.0,
        'daily_pnl_usdt': 0.0,
        'pnl_date': '',
        'daily_start_capital': 0.0,
        'consec_sl': 0,
        'cooldown_symbols': {},   # {symbol: expiry_timestamp}
        'status': 'active',       # active | paused
        'last_ctx_alert_time': 0,
        'last_ctx_alert_reason': '',
        'last_update': '',
    }

def add_cooldown(state, symbol):
    """Agrega un símbolo al cooldown con timestamp de expiry."""
    cooldowns = state.get('cooldown_symbols', {})
    if isinstance(cooldowns, list):   # migrar formato viejo si es necesario
        cooldowns = {s: 0 for s in cooldowns}
    cooldowns[symbol] = int(time.time()) + config.COOLDOWN_HOURS * 3600
    state['cooldown_symbols'] = cooldowns

def remove_cooldown(state, symbol):
    """Remueve un símbolo del cooldown (ej: tras un TP)."""
    cooldowns = state.get('cooldown_symbols', {})
    if isinstance(cooldowns, dict):
        cooldowns.pop(symbol, None)
    elif isinstance(cooldowns, list) and symbol in cooldowns:
        cooldowns.remove(symbol)
    state['cooldown_symbols'] = cooldowns

def get_active_cooldowns(state):
    """Retorna set de símbolos que todavía están en cooldown activo."""
    cooldowns = state.get('cooldown_symbols', {})
    now = int(time.time())
    if isinstance(cooldowns, list):   # formato viejo
        return set(cooldowns)
    return {sym for sym, expiry in cooldowns.items() if expiry == 0 or expiry > now}

# ── Alertas ───────────────────────────────────────────────────────────────────

def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _money(value):
    value = _num(value)
    return 'N/D' if value is None else f'{value:.2f} USDT'


def _signed_money(value):
    value = _num(value)
    if value is None:
        return 'N/D'
    return f'{value:+.2f} USDT'


def _pct(value):
    value = _num(value)
    return 'N/D' if value is None else f'{value:+.2f}%'


def _duration_text(entry_time, now=None):
    entry = _num(entry_time)
    if entry is None:
        return None
    seconds = max(0, int((now or time.time()) - entry))
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours:
        return f'{hours}h {minutes}m'
    return f'{minutes}m'


def format_trade_open_alert(pos, candidate=None, market_regime=None):
    pos = pos if isinstance(pos, dict) else {}
    candidate = candidate if isinstance(candidate, dict) else {}
    side = str(pos.get('direction') or '').upper()
    symbol = pos.get('symbol') or 'N/D'
    entry = _num(pos.get('entry_price'))
    qty = _num(pos.get('quantity'))
    tp = _num(pos.get('tp'))
    sl = _num(pos.get('sl'))
    leverage = _num(pos.get('leverage')) or 1.0
    exposure = entry * qty if entry is not None and qty is not None else None
    capital_used = exposure / leverage if exposure is not None and side == 'SHORT' else exposure

    tp_pct = sl_pct = gain_tp = loss_sl = rr = None
    if entry and tp is not None and sl is not None and qty is not None:
        if side == 'SHORT':
            tp_pct = (tp - entry) / entry * 100
            sl_pct = (sl - entry) / entry * 100
            gain_tp = max(0.0, (entry - tp) * qty)
            loss_sl = -abs((sl - entry) * qty)
        else:
            tp_pct = (tp - entry) / entry * 100
            sl_pct = (sl - entry) / entry * 100
            gain_tp = max(0.0, (tp - entry) * qty)
            loss_sl = -abs((entry - sl) * qty)
        risk = abs(loss_sl or 0)
        reward = abs(gain_tp or 0)
        rr = reward / risk if risk > 0 else None

    icon = '\U0001F7E2' if side == 'LONG' else '\U0001F534'
    title = 'LONG abierto' if side == 'LONG' else 'SHORT abierto'
    lines = [
        f'{icon} {title}',
        '',
        str(symbol),
        '',
        'Capital:' if side == 'LONG' else 'Margen:',
        _money(capital_used),
        '',
        'SL:',
        _pct(sl_pct),
        '',
        'TP:',
        _pct(tp_pct),
    ]

    reason = []
    if candidate.get('score') is not None:
        reason.append(f'Score {candidate.get("score")}')
    if market_regime:
        reason.append(f'mercado {market_regime}')
    if candidate.get('reasons'):
        raw_reasons = candidate.get('reasons')
        if isinstance(raw_reasons, (list, tuple)):
            reason.append(', '.join(str(r) for r in raw_reasons[:3]))
        else:
            reason.append(str(raw_reasons))
    if reason:
        lines.extend(['', 'Motivo:', ' / '.join(reason)])
    return '\n'.join(lines)


def format_trade_close_alert(pos, exit_price, exit_reason, pnl_usdt):
    pos = pos if isinstance(pos, dict) else {}
    side = str(pos.get('direction') or '').upper() or 'N/D'
    symbol = pos.get('symbol') or 'N/D'
    pnl = _num(pnl_usdt)
    result = 'BREAKEVEN'
    if pnl is not None and pnl > 0:
        result = 'WIN'
    elif pnl is not None and pnl < 0:
        result = 'LOSS'
    entry = _num(pos.get('entry_price'))
    exit_ = _num(exit_price)
    pnl_pct = None
    if entry and exit_ is not None:
        if side == 'SHORT':
            pnl_pct = (entry - exit_) / entry * 100
        else:
            pnl_pct = (exit_ - entry) / entry * 100
    qty = _num(pos.get('quantity'))
    leverage = _num(pos.get('leverage')) or 1.0
    exposure = entry * qty if entry is not None and qty is not None else None
    capital_used = exposure / leverage if exposure is not None and side == 'SHORT' else exposure
    icon = '\u2705' if result == 'WIN' else '\u274c' if result == 'LOSS' else '\u26AA'
    lines = [
        f'{icon} Trade cerrado',
        '',
        'Simbolo:',
        str(symbol),
        '',
        'Direccion:',
        side,
        '',
        'Resultado:',
        result,
        '',
        'PnL:',
        f'{_signed_money(pnl)} ({_pct(pnl_pct)})',
    ]
    if capital_used is not None:
        lines.extend(['', 'Capital usado:', _money(capital_used)])
    duration = _duration_text(pos.get('entry_time'))
    if duration:
        lines.extend(['', 'Duracion:', duration])
    lines.extend(['', 'Motivo:', str(exit_reason or 'N/D')])
    return '\n'.join(lines)


def format_rebalance_alert(message):
    text = str(message or '')
    lower = text.lower()
    if 'rebalanceo' not in lower:
        return text
    direction = 'N/D'
    if 'spot' in lower and 'futures' in lower:
        direction = 'Spot -> Futures' if lower.find('spot') < lower.find('futures') else 'Futures -> Spot'
    amount = None
    match = re.search(r'\$?([0-9]+(?:[.,][0-9]+)?)', text)
    if match:
        amount = match.group(1).replace(',', '.')
    if any(token in lower for token in ('error', 'fallo', 'bloqueado')):
        return '\n'.join([
            '\U0001F6A8 Rebalance fallo',
            '',
            'Direccion:',
            direction,
            '',
            'Monto intentado:',
            _money(amount),
            '',
            'Motivo:',
            text,
        ])
    return '\n'.join([
        '\U0001F504 Rebalance ejecutado',
        '',
        'Direccion:',
        direction,
        '',
        'Transferido:',
        _money(amount),
    ])


def send_alert(msg):
    try:
        from telegram_alerts import send_telegram_alert
        lower = str(msg).lower()
        level = 'INFO'
        kind = 'INFO'
        if any(token in lower for token in ('intervencion urgente', 'sin stops', 'critical', 'critico', 'urgente')):
            level = 'CRITICAL'
            kind = 'CRITICAL'
        elif any(token in lower for token in ('error', 'fallo', 'no pude recolocar', 'requiere intervenci', 'api error', 'binance api')):
            level = 'ERROR'
            kind = 'ERROR'
        elif 'rehabilitado desde blacklist' in lower:
            level = 'INFO'
            kind = 'BLACKLIST'
        elif 'agregado a blacklist' in lower or 'auto-blacklist' in lower:
            level = 'WARNING'
            kind = 'BLACKLIST'
        elif any(token in lower for token in ('cierre preventivo', 'pausado', 'limite diario', 'rechaz', 'guardrail')):
            level = 'WARNING'
            kind = 'WARNING'
        elif 'rebalanceo' in lower:
            level = 'INFO'
            kind = 'REBALANCE'
        elif 'parcial' in lower:
            level = 'INFO'
            kind = 'PARTIAL'
        elif 'abierto' in lower:
            level = 'INFO'
            kind = 'OPEN'
        elif 'cerrado' in lower:
            level = 'INFO'
            kind = 'CLOSE'
        elif 'tp' in lower:
            level = 'INFO'
            kind = 'TP'
        elif any(token in lower for token in ('abierto', 'cerrado', 'rebalanceo', 'parcial', 'tp')):
            level = 'INFO'
        elif 'sl' in lower:
            level = 'WARNING'
            kind = 'SL'
        send_telegram_alert(level, 'BinanceBot', msg, notification_type=kind)
    except Exception:
        pass
    try:
        subprocess.run([
            'openclaw', 'message', 'send',
            '--channel', 'jarvis',
            '--target', config.ALERT_TARGET,
            '--message', msg
        ], timeout=10, capture_output=True)
    except Exception:
        pass

# ── Logs ──────────────────────────────────────────────────────────────────────
def log_trade(trade_num, symbol, direction, result, pnl, capital_after):
    pair = symbol.replace('USDT', '/USDT')
    date = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())
    pnl_str = f'+${pnl:.4f}' if pnl >= 0 else f'-${abs(pnl):.4f}'
    dir_str = '📈L' if direction == 'long' else '📉S'
    line = f'{trade_num:<4}| {dir_str} {pair:<12}| {result:<8}| {pnl_str:<12}| ${capital_after:<10.4f}| {date}\n'
    try:
        with open(config.TRADES_LOG, 'a', encoding='utf-8') as f:
            f.write(line)
    except Exception as e:
        print(f'Log trade write failed: {e}')

def log_analysis(direction, chosen, descarte):
    now = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())
    tag = 'LONG' if direction == 'long' else 'SHORT'
    try:
        with open(config.ANALYSIS_LOG, 'a', encoding='utf-8') as f:
            if chosen:
                f.write(f'[{now}] {tag} ELEGIDO: {chosen["symbol"]} score={chosen["score"]} RSI={chosen["rsi"]:.0f} ATR={chosen["atr_pct"]:.2f}%\n')
            else:
                motivo = descarte.get('MERCADO', 'sin candidatos')
                # No loguear en el log principal si es por modo direccional (ruido)
                if 'modo direccional' in motivo:
                    pass  # silencioso
                else:
                    f.write(f'[{now}] {tag} SIN CANDIDATO — {motivo}\n')
            for sym, motivo in descarte.items():
                if sym != 'MERCADO':
                    # No loguear símbolos individuales si es por modo direccional
                    if 'modo direccional' not in motivo:
                        f.write(f'  ✗ {sym}: {motivo}\n')
    except Exception:
        pass

# ── Klines / indicadores ─────────────────────────────────────────────────────
def get_klines(symbol, interval='1h', limit=50, futures=False):
    if futures:
        return fut_public('/fapi/v1/klines', {'symbol': symbol, 'interval': interval, 'limit': limit})
    return spot_public('/api/v3/klines', {'symbol': symbol, 'interval': interval, 'limit': limit})

def ema(prices, period):
    k = 2 / (period + 1)
    e = [prices[0]]
    for p in prices[1:]:
        e.append(p * k + e[-1] * (1 - k))
    return e

def rsi(prices, period=14):
    gains, losses = [], []
    for i in range(1, len(prices)):
        d = prices[i] - prices[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    if len(gains) < period:
        return 50
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    if al == 0:
        return 100
    rs = ag / al
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
        rs = ag / al if al else float('inf')
    return 100 - 100 / (1 + rs)

def macd_hist(prices):
    e12 = ema(prices, 12)
    e26 = ema(prices, 26)
    line = [a - b for a, b in zip(e12, e26)]
    signal = ema(line, 9)
    return line[-1] - signal[-1]

def pearson_corr(a, b):
    n = len(a)
    if n < 2:
        return 0.0
    ma, mb = sum(a) / n, sum(b) / n
    num  = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    dena = sum((x - ma) ** 2 for x in a) ** 0.5
    denb = sum((y - mb) ** 2 for y in b) ** 0.5
    if dena == 0 or denb == 0:
        return 0.0
    return num / (dena * denb)

def atr(highs, lows, period=14):
    trs = [h - l for h, l in zip(highs[-period:], lows[-period:])]
    return sum(trs) / len(trs) if trs else 0

# ── Lock ──────────────────────────────────────────────────────────────────────
if os.name == 'nt':
    import msvcrt
else:
    import fcntl

def get_spot_risk_pct(usdt_free, consec_sl=0):
    """
    Retorna el % de capital a usar por posición según capital disponible.
    Con poco capital: todo en una sola posición.
    Con más capital: divide para diversificar.
    """
    if consec_sl >= config.MAX_CONSEC_SL:
        return config.SPOT_RISK_REDUCED
    if usdt_free >= config.DIVERSIFY_THRESHOLD_2:
        return config.DIVERSIFY_RISK_3
    if usdt_free >= config.DIVERSIFY_THRESHOLD_1:
        return config.DIVERSIFY_RISK_2
    return config.SPOT_RISK_PCT


def get_futures_risk_pct(usdt_available):
    """
    Retorna el % de capital futures a usar por posición según disponible.
    Umbrales más altos que en spot porque en futures el buffer libre es mantenimiento.
    """
    if usdt_available >= config.DIVERSIFY_THRESHOLD_2 * 1.5:  # > $120
        return config.DIVERSIFY_RISK_3   # 30%
    if usdt_available >= config.DIVERSIFY_THRESHOLD_2:        # > $80
        return config.DIVERSIFY_RISK_2   # 45%
    return config.FUTURES_RISK_PCT        # 50% (default)


def get_futures_capital_per_position(state):
    """
    Retorna el capital a usar por posicion short usando el mismo presupuesto
    por operacion que valida capital_manager.
    """
    import capital_manager

    total, available, _ = get_futures_summary()
    limits = capital_manager.get_limits()
    usable = capital_manager.futures_usable_capital(total, limits)
    max_shorts = get_max_short_positions(usable)
    capital = capital_manager.max_margin_per_position(
        usable, max_shorts, limits.max_exposure_percent
    )
    logging.debug(
        'POSITION SIZING:\n'
        'wallet=futures\n'
        'usable=%.2f\n'
        'exposure_limit=%.2f%%\n'
        'slots=%s\n'
        'margin_per_position=%.2f',
        usable,
        limits.max_exposure_percent,
        max_shorts,
        capital,
    )
    return min(capital, available * 0.95)


def get_spot_capital_per_position(state, spot_free=None):
    """
    Retorna el capital a usar por posicion long usando el mismo presupuesto
    por operacion que valida capital_manager.
    """
    import capital_manager

    if spot_free is None:
        spot_free = get_usdt_spot()
    spot_in_pos = capital_manager.open_spot_exposure(state)
    spot_total = float(spot_free or 0) + spot_in_pos
    limits = capital_manager.get_limits()
    usable = capital_manager.spot_usable_capital(spot_total, limits)
    max_longs = get_max_long_positions(usable)
    capital = capital_manager.max_margin_per_position(
        usable, max_longs, limits.max_exposure_percent
    )
    logging.debug(
        'POSITION SIZING:\n'
        'wallet=spot\n'
        'usable=%.2f\n'
        'exposure_limit=%.2f%%\n'
        'slots=%s\n'
        'margin_per_position=%.2f',
        usable,
        limits.max_exposure_percent,
        max_longs,
        capital,
    )
    return min(capital, float(spot_free or 0) * 0.95)


def get_max_short_positions(usdt_available):
    """
    Retorna cuántas posiciones short simultáneas se permiten según capital futures.
    """
    if usdt_available >= config.DIVERSIFY_THRESHOLD_2 * 1.5:
        return 4
    if usdt_available >= config.DIVERSIFY_THRESHOLD_2:
        return 3
    return config.MAX_SHORT_POSITIONS   # 2 (default)


def get_max_long_positions(usdt_free):
    """
    Retorna cuántas posiciones long simultáneas se permiten según capital.
    """
    if usdt_free >= config.DIVERSIFY_THRESHOLD_2:
        return 3
    if usdt_free >= config.DIVERSIFY_THRESHOLD_1:
        return 2
    return 1


def clean_dust(dry_run=False):
    """
    Convierte activos residuales (polvo) a BNB via /sapi/v1/asset/dust.
    Solo convierte si el valor total supera DUST_MIN_VALUE_USD.
    Procesa UN activo por llamada (rate limit: 1 conversión/hora).
    Retorna (convertidos, mensaje).
    """
    try:
        acc = get_spot_account()
    except Exception as e:
        return [], f'Error al obtener balance: {e}'

    # Precios en batch
    try:
        req = urllib.request.Request(
            f'{config.SPOT_BASE}/api/v3/ticker/price',
            headers={'User-Agent': 'Mozilla/5.0'}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            all_prices = {p['symbol']: float(p['price']) for p in json.loads(r.read())}
    except Exception:
        all_prices = {}

    dust_assets = []
    total_usd   = 0.0

    for b in acc.get('balances', []):
        asset  = b['asset']
        free   = float(b['free'])
        locked = float(b['locked'])
        total  = free + locked

        if total < 0.00001:
            continue
        if asset in config.DUST_PROTECTED:
            continue

        sym = asset + 'USDT'
        price = all_prices.get(sym, 0)
        if price == 0:
            continue

        usd_val = total * price
        if usd_val < 5.0:   # es polvo (no alcanza el notional mínimo)
            dust_assets.append(asset)
            total_usd += usd_val

    if not dust_assets:
        return [], 'Sin polvo para convertir'

    if total_usd < config.DUST_MIN_VALUE_USD:
        return [], f'Polvo insuficiente (${total_usd:.3f} < ${config.DUST_MIN_VALUE_USD})'

    if dry_run:
        return dust_assets, f'[DRY] Convertiría: {", ".join(dust_assets)} (~${total_usd:.3f})'

    # Procesar UN activo por llamada (rate limit 1/hora)
    # Intentar cada uno hasta que uno funcione
    for asset in dust_assets:
        try:
            result = spot_signed('POST', '/sapi/v1/asset/dust', {'asset': asset})
            bnb = float(result.get('totalTransfered', 0))
            restantes = [a for a in dust_assets if a != asset]
            msg = f'{asset} → {bnb:.6f} BNB'
            if restantes:
                msg += f' | Pendientes: {", ".join(restantes)} (próximos ciclos)'
            return [asset], msg
        except urllib.error.HTTPError as e:
            body = b''
            try: body = e.read()
            except Exception: pass
            err_body = body.decode() if body else str(e)
            if '32110' in err_body:  # rate limit activo
                return [], f'Rate limit activo, reintentando en el próximo ciclo'
            # Otro error 400 (saldo insuficiente, par no soportado) → saltar
            continue
        except Exception:
            continue

    return [], 'Ningún activo pudo convertirse en este ciclo'

def _pid_exists(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _read_lock_info(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _lock_is_stale(path):
    if not os.path.exists(path):
        return False
    info = _read_lock_info(path)
    return not _pid_exists(info.get('pid'))


def _remove_stale_lock(path):
    if _lock_is_stale(path):
        try:
            os.remove(path)
            return True
        except OSError:
            return False
    return False


def _write_lock_info(lock_fd):
    info = {
        'pid': os.getpid(),
        'created_at': datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z'),
        'path': config.LOCK_FILE,
    }
    lock_fd.seek(0)
    lock_fd.truncate()
    json.dump(info, lock_fd, separators=(',', ':'))
    lock_fd.write('\n')
    lock_fd.flush()
    try:
        os.fsync(lock_fd.fileno())
    except OSError:
        pass
    lock_fd._lock_path = config.LOCK_FILE
    lock_fd._lock_pid = os.getpid()


def _lock_owned_by_current_process(path):
    info = _read_lock_info(path)
    return info.get('pid') == os.getpid()


def acquire_lock():
    lock_dir = os.path.dirname(config.LOCK_FILE)
    if lock_dir:
        os.makedirs(lock_dir, exist_ok=True)
    _remove_stale_lock(config.LOCK_FILE)
    lock_fd = open(config.LOCK_FILE, 'a+', encoding='utf-8')
    try:
        lock_fd.seek(0)
        if os.name == 'nt':
            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _write_lock_info(lock_fd)
        return lock_fd
    except (BlockingIOError, OSError):
        lock_fd.close()
        return None

def release_lock(lock_fd):
    if lock_fd:
        path = getattr(lock_fd, '_lock_path', config.LOCK_FILE)
        owned = getattr(lock_fd, '_lock_pid', None) == os.getpid()
        removed = False
        try:
            if owned and os.name != 'nt':
                os.remove(path)
                removed = True
        except OSError:
            removed = False
        try:
            if os.name == 'nt':
                lock_fd.seek(0)
                msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            lock_fd.close()
        if owned and not removed and os.path.exists(path):
            for _ in range(5):
                try:
                    if _lock_owned_by_current_process(path):
                        os.remove(path)
                    break
                except OSError:
                    time.sleep(0.1)
