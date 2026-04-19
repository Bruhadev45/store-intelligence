"""Store metrics computation.

All queries exclude is_staff=true. All numeric outputs default to 0 (never null)
so `GET /stores/{id}/metrics` is safe even when the store has zero data.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, select

from .config import SETTINGS
from .db import events_table, pos_transactions_table, session_scope


def _today_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    now = now or datetime.now(timezone.utc)
    start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return start, end


@dataclass(frozen=True)
class StoreMetrics:
    store_id: str
    window_start: str
    window_end: str
    unique_visitors: int
    conversion_rate: float
    abandonment_rate: float
    avg_dwell_per_zone_ms: dict[str, float]
    current_queue_depth: int
    pos_transactions: int
    generated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "store_id": self.store_id,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "unique_visitors": self.unique_visitors,
            "conversion_rate": round(self.conversion_rate, 4),
            "abandonment_rate": round(self.abandonment_rate, 4),
            "avg_dwell_per_zone_ms": self.avg_dwell_per_zone_ms,
            "current_queue_depth": self.current_queue_depth,
            "pos_transactions": self.pos_transactions,
            "generated_at": self.generated_at,
        }


async def compute_store_metrics(store_id: str, now: datetime | None = None) -> StoreMetrics:
    start, end = _today_window(now)
    now = now or datetime.now(timezone.utc)
    e = events_table.c
    p = pos_transactions_table.c

    async with session_scope() as s:
        # Unique (non-staff) visitors who crossed ENTRY today.
        uv_q = select(func.count(func.distinct(e.visitor_id))).where(
            and_(
                e.store_id == store_id,
                e.timestamp >= start,
                e.timestamp < end,
                e.event_type == "ENTRY",
                e.is_staff.is_(False),
            )
        )
        unique_visitors = int((await s.execute(uv_q)).scalar() or 0)

        # Visitors that entered the billing zone (=billing queue join).
        billed_q = select(func.distinct(e.visitor_id)).where(
            and_(
                e.store_id == store_id,
                e.timestamp >= start,
                e.timestamp < end,
                e.event_type == "BILLING_QUEUE_JOIN",
                e.is_staff.is_(False),
            )
        )
        billed_visitors = {r[0] for r in (await s.execute(billed_q)).all()}

        # Visitors that abandoned the queue.
        abandon_q = select(func.count()).where(
            and_(
                e.store_id == store_id,
                e.timestamp >= start,
                e.timestamp < end,
                e.event_type == "BILLING_QUEUE_ABANDON",
                e.is_staff.is_(False),
            )
        )
        abandons = int((await s.execute(abandon_q)).scalar() or 0)
        joins_q = select(func.count()).where(
            and_(
                e.store_id == store_id,
                e.timestamp >= start,
                e.timestamp < end,
                e.event_type == "BILLING_QUEUE_JOIN",
                e.is_staff.is_(False),
            )
        )
        joins = int((await s.execute(joins_q)).scalar() or 0)
        abandonment_rate = (abandons / joins) if joins > 0 else 0.0

        # POS transactions today attributed to visitors.
        pos_q = select(p.visitor_id, p.timestamp).where(
            and_(p.store_id == store_id, p.timestamp >= start, p.timestamp < end)
        )
        pos_rows = (await s.execute(pos_q)).all()
        pos_visitors = {row[0] for row in pos_rows if row[0]}

        # Conversion = billed_visitors ∩ pos_visitors / unique_visitors.
        converted = billed_visitors & pos_visitors
        conversion_rate = (len(converted) / unique_visitors) if unique_visitors > 0 else 0.0

        # Avg dwell per zone from ZONE_DWELL events.
        dwell_q = select(e.zone_id, func.avg(e.dwell_ms)).where(
            and_(
                e.store_id == store_id,
                e.timestamp >= start,
                e.timestamp < end,
                e.event_type == "ZONE_DWELL",
                e.is_staff.is_(False),
                e.zone_id.isnot(None),
            )
        ).group_by(e.zone_id)
        avg_dwell = {
            row[0]: round(float(row[1] or 0), 2)
            for row in (await s.execute(dwell_q)).all()
        }

        # Current queue depth = max queue_depth in metadata for last 5 min.
        cutoff = now - timedelta(minutes=5)
        q_metadata_q = select(e.metadata_json).where(
            and_(
                e.store_id == store_id,
                e.timestamp >= cutoff,
                e.event_type == "BILLING_QUEUE_JOIN",
            )
        )
        rows = (await s.execute(q_metadata_q)).all()
        depths = [int(r[0].get("queue_depth", 0)) for r in rows if isinstance(r[0], dict)]
        current_queue_depth = max(depths) if depths else 0

    return StoreMetrics(
        store_id=store_id,
        window_start=start.isoformat(),
        window_end=end.isoformat(),
        unique_visitors=unique_visitors,
        conversion_rate=conversion_rate,
        abandonment_rate=abandonment_rate,
        avg_dwell_per_zone_ms=avg_dwell,
        current_queue_depth=current_queue_depth,
        pos_transactions=len(pos_rows),
        generated_at=now.isoformat(),
    )


_ = SETTINGS  # keep import alive for future tuning
