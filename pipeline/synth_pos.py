"""Synthesize pos_transactions.csv from billing-queue exit events.

Purplle brief implies a POS dataset is provided but none exists, so we
generate a deterministic synthetic one keyed to actual billing-exit events.

Rules:
- For each BILLING_QUEUE_LEAVE event (not ABANDON), emit a POS row with
  probability p_purchase (default 0.45).
- Timestamp: 30..180s after the billing exit.
- basket_value: log-normal(mu=6.8, sigma=0.6) → ~ ₹900 median.
- items_count: geometric-ish 1..7.
- Seeded by event_id for reproducibility.

Writes CSV and (optionally) POSTs to /pos/ingest.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

import httpx


def _seeded_rng(seed_material: str) -> random.Random:
    digest = hashlib.sha256(seed_material.encode()).digest()
    seed_int = int.from_bytes(digest[:8], "big")
    return random.Random(seed_int)


def iter_billing_exits(jsonl_path: Path) -> Iterable[dict]:
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("event_type") in ("BILLING_QUEUE_LEAVE", "BILLING_QUEUE_ABANDON"):
                yield e


def synthesize(
    events_path: Path,
    output_csv: Path,
    p_purchase: float = 0.45,
    master_seed: str = "apex-retail-v1",
) -> list[dict]:
    rows: list[dict] = []
    for idx, e in enumerate(iter_billing_exits(events_path)):
        if e.get("event_type") == "BILLING_QUEUE_ABANDON":
            continue
        rng = _seeded_rng(f"{master_seed}:{e['event_id']}")
        if rng.random() > p_purchase:
            continue
        exit_ts = datetime.fromisoformat(e["timestamp"])
        delay = rng.randint(30, 180)
        basket = max(50.0, math.exp(rng.gauss(6.8, 0.6)))
        items = 1 + int(rng.expovariate(0.6))
        if items > 7:
            items = 7
        rows.append(
            {
                "transaction_id": f"TXN_{e['event_id'][:12]}",
                "store_id": e["store_id"],
                "visitor_id": e["visitor_id"],
                "timestamp": (exit_ts + timedelta(seconds=delay)).isoformat(),
                "basket_value": round(basket, 2),
                "items_count": items,
            }
        )

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(output_csv, "w", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "transaction_id",
                "store_id",
                "visitor_id",
                "timestamp",
                "basket_value",
                "items_count",
            ],
        )
        w.writeheader()
        w.writerows(rows)
    return rows


def post(rows: list[dict], api_url: str) -> int:
    if not rows:
        return 0
    with httpx.Client(timeout=10.0) as c:
        r = c.post(f"{api_url.rstrip('/')}/pos/ingest", json={"transactions": rows})
        r.raise_for_status()
        return int(r.json().get("accepted", 0))


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--events", type=Path, default=Path("data/events.jsonl"))
    p.add_argument("--output", type=Path, default=Path("data/pos_transactions.csv"))
    p.add_argument("--api-url", default="http://localhost:8000")
    p.add_argument("--p-purchase", type=float, default=0.45)
    p.add_argument("--post", action="store_true", help="Also POST to /pos/ingest")
    args = p.parse_args(argv)

    if not args.events.exists():
        print(f"[synth_pos] no events file at {args.events}", file=sys.stderr)
        return 1
    rows = synthesize(args.events, args.output, p_purchase=args.p_purchase)
    print(f"[synth_pos] wrote {len(rows)} rows to {args.output}")
    if args.post:
        accepted = post(rows, args.api_url)
        print(f"[synth_pos] POSTed {accepted} transactions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
