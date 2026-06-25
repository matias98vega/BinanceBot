#!/usr/bin/env python3
"""Environment-based configuration loader for BinanceBot."""
import os
import tempfile
from dataclasses import dataclass


TRADING_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TRADING_DIR)
ENV_FILES = [
    os.path.join(PROJECT_DIR, '.env'),
    os.path.join(TRADING_DIR, '.env'),
]


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeConfig:
    api_key: str
    api_secret: str
    spot_base: str
    futures_base: str
    state_file: str
    trades_log: str
    analysis_log: str
    analytics_file: str
    decision_snapshots_file: str
    reports_dir: str
    csv_file: str
    cycle_baseline_file: str
    lock_file: str
    alert_target: str
    env_present: bool


def load_dotenv():
    loaded = False
    for path in ENV_FILES:
        if not os.path.exists(path):
            continue
        loaded = True
        with open(path, encoding='utf-8') as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                os.environ.setdefault(key, value)
    return loaded


def _env(name, default=None, required=False):
    value = os.environ.get(name)
    if value is None or value == '':
        if required:
            raise ConfigError(f'Missing required environment variable: {name}')
        return default
    return value


def _path(value, default):
    raw = value or default
    if os.path.isabs(raw):
        return raw
    return os.path.abspath(os.path.join(PROJECT_DIR, raw))


def _lock_path(value, default):
    raw = value or default
    if os.name == 'nt' and raw.startswith('/tmp/'):
        return os.path.join(tempfile.gettempdir(), os.path.basename(raw))
    return raw


def load_config(require_api=False):
    env_present = load_dotenv()
    api_key = _env('BINANCE_API_KEY', '', required=require_api)
    api_secret = _env('BINANCE_API_SECRET', '', required=require_api)
    spot_base = _env('BINANCE_SPOT_BASE', 'https://api.binance.com')
    futures_base = _env('BINANCE_FUTURES_BASE', 'https://fapi.binance.com')

    if not spot_base.startswith('https://'):
        raise ConfigError('BINANCE_SPOT_BASE must start with https://')
    if not futures_base.startswith('https://'):
        raise ConfigError('BINANCE_FUTURES_BASE must start with https://')
    default_lock_file = '/tmp/trading_bot.lock'
    if os.name == 'nt':
        default_lock_file = os.path.join(tempfile.gettempdir(), 'trading_bot.lock')

    return RuntimeConfig(
        api_key=api_key,
        api_secret=api_secret,
        spot_base=spot_base.rstrip('/'),
        futures_base=futures_base.rstrip('/'),
        state_file=_path(_env('STATE_FILE'), 'trading/state.json'),
        trades_log=_path(_env('TRADES_LOG'), 'trading/trades_log.txt'),
        analysis_log=_path(_env('ANALYSIS_LOG'), 'trading/analysis_log.txt'),
        analytics_file=_path(_env('ANALYTICS_FILE'), 'trading/trade_analytics.jsonl'),
        decision_snapshots_file=_path(_env('DECISION_SNAPSHOTS_FILE'), 'trading/decision_snapshots.jsonl'),
        reports_dir=_path(_env('REPORTS_DIR'), 'trading/reports'),
        csv_file=_path(_env('CSV_FILE'), 'trading/reports/trades.csv'),
        cycle_baseline_file=_path(_env('CYCLE_BASELINE_FILE'), 'trading/.cycle_baseline.json'),
        lock_file=_lock_path(_env('LOCK_FILE'), default_lock_file),
        alert_target=_env('ALERT_TARGET', ''),
        env_present=env_present,
    )


def validate_environment(require_api=True):
    config = load_config(require_api=require_api)
    missing = []
    if require_api:
        if not config.api_key:
            missing.append('BINANCE_API_KEY')
        if not config.api_secret:
            missing.append('BINANCE_API_SECRET')
    if missing:
        raise ConfigError('Missing required environment variables: ' + ', '.join(missing))
    return config
