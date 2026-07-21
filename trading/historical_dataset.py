#!/usr/bin/env python3
"""Read-only helpers for stable snapshots of actively appended JSONL files."""
import hashlib
import json
import os
import time

import archive_manifest


class ConcurrentRewriteError(RuntimeError):
    """The source was truncated, rotated or rewritten during a capture."""


def capture_consistent_prefix(path, retries=8, retry_delay=0.05):
    """Capture a verified complete-line prefix without locking its writer.

    Appends after the initial stat are allowed. In-place rewrite, inode change or
    truncation causes a retry. The source is never opened for writing.
    """
    last_reason = 'source unavailable'
    for attempt in range(1, retries + 1):
        before = os.stat(path)
        size = before.st_size
        with open(path, 'rb') as handle:
            payload = handle.read(size)
        after = os.stat(path)
        if before.st_ino != after.st_ino:
            last_reason = 'inode changed during capture'
        elif after.st_size < size:
            last_reason = 'source shrank during capture'
        elif payload and not payload.endswith(b'\n'):
            last_reason = 'captured prefix ends in an incomplete line'
        else:
            with open(path, 'rb') as handle:
                verification = handle.read(size)
            if verification == payload:
                return {
                    'payload': payload, 'bytes': len(payload),
                    'sha256': hashlib.sha256(payload).hexdigest(),
                    'inode': before.st_ino, 'mtime_ns': before.st_mtime_ns,
                    'source_size_before': size, 'source_size_after': after.st_size,
                    'append_observed_during_capture': after.st_size > size,
                    'attempts': attempt,
                }
            last_reason = 'captured prefix changed during verification'
        if attempt < retries:
            time.sleep(retry_delay)
    raise ConcurrentRewriteError(last_reason)


def decode_jsonl(payload):
    records = []
    for line_number, raw in enumerate(payload.splitlines(), 1):
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except Exception as exc:
            raise ValueError(f'line {line_number}: invalid JSON: {exc}') from exc
        if not isinstance(record, dict):
            raise ValueError(f'line {line_number}: record is not an object')
        records.append(record)
    return records


def logical_fingerprint_records(records):
    digest = hashlib.sha256()
    count = 0
    for record in records:
        digest.update(archive_manifest._canonical(record))
        count += 1
    return {'algorithm': 'sha256-canonical-jsonl-v1', 'sha256': digest.hexdigest(), 'record_count': count}


def compare_prefix(earlier, later):
    """Classify two verified captures from the same active path."""
    first, second = earlier['payload'], later['payload']
    if second == first:
        return {'status': 'UNCHANGED', 'prefix_preserved': True, 'appended_bytes': 0}
    if second.startswith(first):
        return {'status': 'APPEND_OBSERVED', 'prefix_preserved': True,
                'appended_bytes': len(second) - len(first)}
    return {'status': 'ROTATION_OR_REWRITE_OBSERVED', 'prefix_preserved': False,
            'appended_bytes': None}
