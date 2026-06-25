#!/usr/bin/env python3
"""
Telemetria estructurada para trades.

El archivo JSONL es append-only para no reescribirlo en cada cierre. Cada linea
representa un evento/snapshot de trade; export_csv reconstruye el estado final.
"""
import argparse
import csv
import json
import os
import time
from datetime import datetime, timezone
from config_loader import load_config


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_runtime_config = load_config(require_api=False)
ANALYTICS_FILE = _runtime_config.analytics_file
DECISIONS_FILE = _runtime_config.decision_snapshots_file
REPORTS_DIR = _runtime_config.reports_dir
CSV_FILE = _runtime_config.csv_file


CSV_COLUMNS = [
    'trade_id',
    'symbol',
    'side',
    'market_regime',
    'entry_time',
    'exit_time',
    'entry_price',
    'exit_price',
    'score',
    'rsi',
    'atr',
    'atr_pct',
    'ema20',
    'ema50',
    'macd_hist',
    'volume_ratio',
    'btc_correlation',
    'reject_reason',
    'reject_reasons',
    'exit_reason',
    'pnl_usdt',
    'pnl_pct',
    'duration_minutes',
]


class AnalyticsLogger:
    def __init__(self, path=ANALYTICS_FILE):
        self.path = path

    def log_trade_open(
        self,
        trade_id,
        symbol,
        side,
        entry_price,
        market_regime=None,
        score=None,
        rsi=None,
        atr=None,
        ema20=None,
        ema50=None,
        volume_ratio=None,
        macd_hist=None,
        atr_pct=None,
        btc_correlation=None,
        reject_reason=None,
        reject_reasons=None,
        capital_at_entry=None,
        entry_time=None,
    ):
        record = {
            'trade_id': trade_id,
            'symbol': symbol,
            'side': side,
            'entry_time': self._iso(entry_time),
            'entry_price': self._float_or_none(entry_price),
            'market_regime': market_regime,
            'score': self._float_or_none(score),
            'rsi': self._float_or_none(rsi),
            'atr': self._float_or_none(atr),
            'ema20': self._float_or_none(ema20),
            'ema50': self._float_or_none(ema50),
            'volume_ratio': self._float_or_none(volume_ratio),
            'macd_hist': self._float_or_none(macd_hist),
            'atr_pct': self._float_or_none(atr_pct),
            'btc_correlation': self._float_or_none(btc_correlation),
            'reject_reason': reject_reason,
            'reject_reasons': reject_reasons,
            'capital_at_entry': self._float_or_none(capital_at_entry),
            'status': 'OPEN',
        }
        self._append(record)
        return record

    def log_trade_close(
        self,
        trade_id,
        exit_price,
        exit_reason,
        pnl_usdt,
        entry_price=None,
        entry_time=None,
        exit_time=None,
        symbol=None,
        side=None,
        **extra,
    ):
        exit_iso = self._iso(exit_time)
        entry_iso = self._iso(entry_time) if entry_time is not None else None
        duration_minutes = self._duration_minutes(entry_iso, exit_iso)
        pnl_pct = self._pnl_pct(side, entry_price, exit_price)

        record = {
            'trade_id': trade_id,
            'symbol': symbol,
            'side': side,
            'entry_time': entry_iso,
            'entry_price': self._float_or_none(entry_price),
            'exit_time': exit_iso,
            'exit_price': self._float_or_none(exit_price),
            'exit_reason': exit_reason,
            'pnl_usdt': self._float_or_none(pnl_usdt),
            'pnl_pct': pnl_pct,
            'duration_minutes': duration_minutes,
            'status': 'CLOSED',
        }
        record.update({k: v for k, v in extra.items() if v is not None})
        self._append(record)
        return record

    def log_event(self, event_type, **fields):
        record = {
            'event_type': event_type,
            'event_time': self._iso(fields.pop('event_time', None)),
        }
        record.update(fields)
        self._append(record)
        return record

    def export_csv(self, csv_path=CSV_FILE):
        trades = self._merged_trades()
        os.makedirs(os.path.dirname(csv_path), exist_ok=True)
        with open(csv_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            for trade in trades.values():
                writer.writerow({col: trade.get(col) for col in CSV_COLUMNS})
        return csv_path

    def load_closed_trades(self):
        return [t for t in self._merged_trades().values() if t.get('status') == 'CLOSED']

    def _merged_trades(self):
        trades = {}
        if not os.path.exists(self.path):
            return trades
        with open(self.path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                trade_id = record.get('trade_id')
                if not trade_id:
                    continue
                current = trades.setdefault(trade_id, {})
                current.update({k: v for k, v in record.items() if v is not None})
        return trades

    def _append(self, record):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n')

    @staticmethod
    def _iso(value=None):
        if value is None:
            return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
        if isinstance(value, str):
            return value
        try:
            return datetime.fromtimestamp(float(value), timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
        except Exception:
            return None

    @staticmethod
    def _float_or_none(value):
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _duration_minutes(entry_iso, exit_iso):
        if not entry_iso or not exit_iso:
            return None
        try:
            entry = datetime.fromisoformat(entry_iso.replace('Z', '+00:00'))
            exit_ = datetime.fromisoformat(exit_iso.replace('Z', '+00:00'))
            return round((exit_ - entry).total_seconds() / 60, 4)
        except Exception:
            return None

    @classmethod
    def _pnl_pct(cls, side, entry_price, exit_price):
        entry = cls._float_or_none(entry_price)
        exit_ = cls._float_or_none(exit_price)
        if not entry or exit_ is None:
            return None
        side_u = (side or '').upper()
        if side_u == 'SHORT':
            return round((entry - exit_) / entry * 100, 4)
        return round((exit_ - entry) / entry * 100, 4)


class DecisionSnapshotLogger:
    def __init__(self, path=DECISIONS_FILE):
        self.path = path

    def log_snapshot(
        self,
        market_regime=None,
        btc_change_1h=None,
        btc_change_4h=None,
        capital_total=None,
        spot_balance=None,
        futures_balance=None,
        mode=None,
        candidates=None,
        timestamp=None,
    ):
        record = {
            'timestamp': AnalyticsLogger._iso(timestamp),
            'market_regime': market_regime,
            'btc_change_1h': AnalyticsLogger._float_or_none(btc_change_1h),
            'btc_change_4h': AnalyticsLogger._float_or_none(btc_change_4h),
            'capital_total': AnalyticsLogger._float_or_none(capital_total),
            'spot_balance': AnalyticsLogger._float_or_none(spot_balance),
            'futures_balance': AnalyticsLogger._float_or_none(futures_balance),
            'mode': mode,
            'candidates': candidates or [],
        }
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n')
        return record


def main():
    parser = argparse.ArgumentParser(description='Trade analytics utilities')
    parser.add_argument('--export', action='store_true', help='Export analytics JSONL to reports/trades.csv')
    args = parser.parse_args()

    if args.export:
        path = AnalyticsLogger().export_csv()
        print(path)


if __name__ == '__main__':
    main()
