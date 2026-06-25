#!/usr/bin/env python3
"""Resumen de decision_snapshots.jsonl."""
import json
import os
from collections import Counter, defaultdict
from config_loader import load_config


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DECISIONS_FILE = load_config(require_api=False).decision_snapshots_file
IMPORTANT_FIELDS = [
    'score',
    'rsi',
    'atr',
    'atr_pct',
    'ema20',
    'ema50',
    'macd_hist',
    'volume_ratio',
    'btc_correlation',
]


def _float_or_none(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_snapshots(path=DECISIONS_FILE):
    snapshots = []
    if not os.path.exists(path):
        return snapshots
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                snapshots.append(data)
    return snapshots


def main():
    snapshots = _load_snapshots()
    candidates = []
    for snapshot in snapshots:
        for candidate in snapshot.get('candidates') or []:
            if isinstance(candidate, dict):
                candidates.append(candidate)

    accepted = [c for c in candidates if c.get('decision') == 'accepted']
    rejected = [c for c in candidates if c.get('decision') == 'rejected']
    side_counts = Counter(c.get('side') or 'UNKNOWN' for c in candidates)
    reason_counts = Counter(c.get('reason') or 'UNKNOWN' for c in rejected)
    null_counts = Counter()
    scores_by_symbol = defaultdict(list)

    for candidate in candidates:
        for field in IMPORTANT_FIELDS:
            if candidate.get(field) is None:
                null_counts[field] += 1
        score = _float_or_none(candidate.get('score'))
        symbol = candidate.get('symbol')
        if symbol and score is not None:
            scores_by_symbol[symbol].append(score)

    avg_scores = sorted(
        ((symbol, sum(scores) / len(scores), len(scores)) for symbol, scores in scores_by_symbol.items()),
        key=lambda item: item[1],
        reverse=True,
    )

    print(f'Cantidad de snapshots: {len(snapshots)}')
    print(f'Candidatos aceptados: {len(accepted)}')
    print(f'Candidatos rechazados: {len(rejected)}')
    print('Razones de rechazo mas frecuentes:')
    if reason_counts:
        for reason, count in reason_counts.most_common(10):
            print(f'  {reason}: {count}')
    else:
        print('  N/A')
    print('Top simbolos por score promedio:')
    if avg_scores:
        for symbol, avg_score, count in avg_scores[:10]:
            print(f'  {symbol}: {avg_score:.2f} ({count})')
    else:
        print('  N/A')
    print('Distribucion LONG/SHORT:')
    if side_counts:
        for side, count in side_counts.most_common():
            print(f'  {side}: {count}')
    else:
        print('  N/A')
    print('Nulls por campo importante:')
    for field in IMPORTANT_FIELDS:
        print(f'  {field}: {null_counts[field]}')


if __name__ == '__main__':
    main()
