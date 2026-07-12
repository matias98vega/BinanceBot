#!/usr/bin/env python3
"""Historical BinanceBot version metadata.

This module is read-only metadata. It does not change trading behavior or
historical files; it only helps classify data by the bot capabilities and known
limitations that existed when records were produced.
"""
import os
from datetime import datetime, timezone


TRADING_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TRADING_DIR)
VERSION_FILE = os.path.join(PROJECT_DIR, 'VERSION')
SCHEMA_VERSION = 1
BOT_VERSION = os.environ.get('BOT_VERSION') or 'v1.2-sizing-v2'
STRATEGY_VERSION = os.environ.get('STRATEGY_VERSION') or 'current'
DATA_SCHEMA_VERSION = os.environ.get('DATA_SCHEMA_VERSION') or 'v1'
VERSION_FIELDS = ('bot_version', 'strategy_version', 'data_schema_version')

UNKNOWN_VERSION = 'unknown'
UNKNOWN_RELIABILITY = 'unknown'


VERSION_HISTORY = [
    {
        'version': 'legacy-pre-history',
        'label': 'Legacy pre-history tracking',
        'started_at': None,
        'ended_at': '2026-06-01T00:00:00Z',
        'capabilities': [
            'Long Spot and Short Futures operation existed in earlier form',
            'Basic analytics and snapshots existed but version metadata was incomplete',
        ],
        'known_bugs': [
            'Historical records may miss bot_version or strategy_version',
            'Partial closes may not always link cleanly to base trade ids',
        ],
        'limitations': [
            'Data reliability must be judged with audit_data_quality.py before use',
            'Do not assume all records have enough context for learning datasets',
        ],
        'fixes': [],
        'data_policy': 'exclude_or_review',
        'confidence': 'low',
    },
    {
        'version': 'v1.0-alpha',
        'label': 'Alpha modular bot',
        'started_at': '2026-06-01T00:00:00Z',
        'ended_at': '2026-07-08T00:00:00Z',
        'capabilities': [
            'Modular cycle with Spot Long and Futures Short flows',
            'Guardian, rebalance, capital manager, Telegram and dashboard observability',
            'History, Feature Store, Timeline, Analytics, Insights and Trade Inspector',
            'Capital Ledger and Capital Accounting infrastructure',
            'Data quality auditor for runtime and historical JSON/JSONL files',
        ],
        'known_bugs': [
            'Earlier alpha records may predate Futures Reconciliation',
            'Earlier alpha Telegram Stats may have treated total_limit gap as trading PnL',
            'Earlier alpha Home/Stats may have used inconsistent PnL sources',
            'Earlier alpha Spot residual OCO recovery may have attempted OCO below NOTIONAL',
            'Known historical debt remains around short_WLDUSDT_1782763085',
        ],
        'limitations': [
            'VERSION is still coarse and does not yet identify every fix as a formal release',
            'Some historical records may need future dry-run repair reports before ML use',
        ],
        'fixes': [
            'Futures Reconciliation observability for exchange-vs-state desync',
            'Spot residual classification for OCO payload below min notional',
            'Telegram Home/Stats PnL source aligned to bot_state.pnl',
            'Capital metrics stopped using total_limit as invested-capital baseline',
        ],
        'data_policy': 'usable_with_audit_flags',
        'confidence': 'medium',
    },
    {
        'version': 'v1.1-observability-hardening',
        'label': 'Runtime metadata and observability hardening',
        'started_at': '2026-07-08T00:00:00Z',
        'ended_at': '2026-07-12T00:00:00Z',
        'capabilities': [
            'New runtime records carry bot_version, strategy_version and data_schema_version',
            'Telegram System/Diagnostics exposes runtime version metadata',
            'Data quality auditor groups records by bot version where metadata exists',
            'repair_data_quality.py can preview auditable version backfill in dry-run mode',
        ],
        'known_bugs': [
            'Historical records before runtime metadata may still be missing bot_version',
            'Known historical debt remains around short_WLDUSDT_1782763085',
        ],
        'limitations': [
            'Version backfill is diagnostic only; no historical rewrite is enabled',
            'Timestamp-based inference has lower confidence than explicit runtime metadata',
        ],
        'fixes': [
            'Runtime version metadata added to new bot state, trade, feature, timeline and status records',
        ],
        'data_policy': 'trusted_if_auditor_clean',
        'confidence': 'high',
    },
    {
        'version': BOT_VERSION,
        'label': 'Sizing v2 capital exposure model',
        'started_at': '2026-07-12T00:00:00Z',
        'ended_at': None,
        'capabilities': [
            'Spot Long entries use target exposure slots instead of a decreasing free-balance percentage',
            'Futures Short entries size by target notional exposure and derive required margin from leverage',
            'Runtime records continue carrying bot_version, strategy_version and data_schema_version',
        ],
        'known_bugs': [
            'Historical records before this version used the previous sizing model',
            'Known historical debt remains around short_WLDUSDT_1782763085',
        ],
        'limitations': [
            'Sizing v2 only applies to new entries; historical trades are not backfilled',
            'Existing legacy positions, if any, keep their original quantities and exposure',
        ],
        'fixes': [
            'Long sizing now targets configured Spot exposure across available slots',
            'Short sizing now caps configured Futures exposure as notional rather than leveraged margin',
        ],
        'data_policy': 'trusted_if_auditor_clean',
        'confidence': 'high',
    },
]


def current_version():
    return BOT_VERSION


def file_version():
    try:
        with open(VERSION_FILE, encoding='utf-8') as f:
            return f.read().strip() or UNKNOWN_VERSION
    except Exception:
        return UNKNOWN_VERSION


def get_version_history():
    return {
        'schema_version': SCHEMA_VERSION,
        'current_version': current_version(),
        'versions': [dict(item) for item in VERSION_HISTORY],
    }


def get_current_version_metadata():
    return {
        'bot_version': BOT_VERSION,
        'strategy_version': STRATEGY_VERSION,
        'data_schema_version': DATA_SCHEMA_VERSION,
    }


def attach_version_metadata(record, overwrite=False):
    if not isinstance(record, dict):
        return record
    for key, value in get_current_version_metadata().items():
        if overwrite or record.get(key) in (None, ''):
            record[key] = value
    return record


def has_top_level_version_metadata(record):
    if not isinstance(record, dict):
        return False
    return all(record.get(key) not in (None, '') for key in VERSION_FIELDS)


def _parse_timestamp(value):
    if value in (None, ''):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _within_range(timestamp, started_at, ended_at):
    dt = _parse_timestamp(timestamp)
    if dt is None:
        return False
    start = _parse_timestamp(started_at)
    end = _parse_timestamp(ended_at)
    if start is not None and dt < start:
        return False
    if end is not None and dt >= end:
        return False
    return True


def version_for_timestamp(timestamp):
    for item in VERSION_HISTORY:
        if _within_range(timestamp, item.get('started_at'), item.get('ended_at')):
            return dict(item)
    return None


def classify_record(record):
    if not isinstance(record, dict):
        return {
            'version': UNKNOWN_VERSION,
            'reliability': UNKNOWN_RELIABILITY,
            'reason': 'record_not_object',
            'known_bugs': [],
            'limitations': [],
        }

    explicit_version = record.get('bot_version')
    timestamp = (
        record.get('timestamp')
        or record.get('recorded_at')
        or record.get('opened_at')
        or record.get('closed_at')
        or record.get('entry_time')
        or record.get('exit_time')
        or record.get('updated_at')
        or record.get('last_seen')
    )
    metadata = None
    if explicit_version:
        metadata = next((item for item in VERSION_HISTORY if item.get('version') == explicit_version), None)
    if metadata is None:
        metadata = version_for_timestamp(timestamp)

    if metadata is None:
        return {
            'version': explicit_version or UNKNOWN_VERSION,
            'reliability': UNKNOWN_RELIABILITY,
            'reason': 'no_matching_version_range',
            'known_bugs': [],
            'limitations': ['Missing timestamp or version range metadata'],
        }

    return {
        'version': metadata.get('version'),
        'reliability': metadata.get('data_policy'),
        'confidence': metadata.get('confidence'),
        'reason': 'matched_explicit_version' if explicit_version else 'matched_timestamp_range',
        'known_bugs': list(metadata.get('known_bugs') or []),
        'limitations': list(metadata.get('limitations') or []),
    }
