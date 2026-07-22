# Archive and retention policy

`data/history/pre_entry_gate_evidence.jsonl` is forward-only durable operational evidence. Keep at least 90 days hot and at least 12 months total. Do not delete evidence before ENFORCE activation and its post-activation review.

Future archival may close monthly JSONL shards, gzip closed shards and register them in the versioned archive manifest. This task creates no production manifest, shard, rotation or destructive rewrite. Migration requires checksum/count validation, logical fingerprint equivalence, concurrent append safety and rollback by ignoring the manifest.
