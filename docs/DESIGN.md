# Apex Retail Store Intelligence — Design

## 1. Problem

Turn raw CCTV footage (five 1080p clips from a single Purplle store) into
structured behavioural events and real-time retail analytics exposed via a
containerised FastAPI service, with sub-second per-request latency under
typical load.

## 2. Architecture

```
┌────────────────┐    ┌──────────────────────────┐    ┌─────────────────┐
│ CCTV clips     │──▶ │ pipeline/                │──▶ │ POST /events/   │
│ CAM 1..5.mp4   │    │  RT-DETR-L (Ultralytics) │    │      ingest     │
│                │    │  ByteTrack (supervision) │    │ idempotent      │
│                │    │  zones.py (geometry)     │    │ partial-success │
│                │    │  staff.py / reentry.py   │    └────────┬────────┘
└────────────────┘    │  emit.py (JSONL + POST)  │             │
                      └──────────────────────────┘             ▼
                                                    ┌────────────────────────┐
                                                    │ FastAPI  (app/)        │
                                                    │   Pydantic v2 Event    │
                                                    │   structured logging   │
                                                    │   error middleware     │
                                                    └────────────┬───────────┘
                                                                 │
                                                                 ▼
                                                    ┌────────────────────────┐
                                                    │ Postgres 16 (asyncpg)  │
                                                    │  events  (PK=event_id) │
                                                    │  pos_transactions      │
                                                    └────────────┬───────────┘
                                                                 │
                            ┌──────────────┬──────────────┬──────┴──────┬──────────────┐
                            ▼              ▼              ▼             ▼              ▼
                       /metrics       /funnel        /heatmap      /anomalies       /health
                            │
                            ▼
                 ┌────────────────────────┐
                 │ dashboard/live.py      │
                 │ rich terminal, 2s poll │
                 └────────────────────────┘
```

## 3. Data flow

1. `pipeline/run.sh` iterates clips in the order defined in
   `config/store_layout.json`. Each clip is assigned a `camera_id` and role
   (entry | floor | stockroom).
2. For every sampled frame (5 fps, tunable), RT-DETR-L returns person
   bounding boxes. ByteTrack assigns persistent `track_id` per camera.
3. `pipeline/zones.py` converts track positions → events: `ENTRY / EXIT` via
   signed line crossing, `ZONE_ENTER / EXIT / DWELL` via point-in-polygon
   with a 30 s dwell cadence, `BILLING_QUEUE_JOIN / LEAVE / ABANDON` via the
   billing polygon + 5 s minimum residency.
4. `pipeline/reentry.py` maintains a 5-minute appearance-histogram cache;
   any ENTRY that matches a recent EXIT is reclassified as `REENTRY`.
5. `pipeline/staff.py` classifies each visitor using HSV uniform match plus
   a dwell-pattern heuristic (>2 distinct zones crossed in <30 s).
6. `pipeline/emit.py` writes every event to `data/events.jsonl` AND posts
   batches of 500 to `POST /events/ingest`. JSONL is the durable source of
   truth; the API is the query surface.
7. `POST /events/ingest` validates against the Pydantic `Event` schema,
   dedupes on `event_id` via `ON CONFLICT DO NOTHING` (PK-enforced), and
   returns `{accepted, duplicates, rejected}` for partial-success semantics.
8. `pipeline/synth_pos.py` reads billing-exit events and generates a
   deterministic, seed-stable POS dataset (see `CHOICES.md` §3).

## 4. Event schema rationale

The Pydantic `Event` in `app/models.py` is the single canonical type. Its
shape is optimised for the four endpoint families:

| Endpoint family | Fields used                                                                          |
|-----------------|--------------------------------------------------------------------------------------|
| `/metrics`      | `event_type=ENTRY`, `is_staff`, `zone_id`+`dwell_ms` for ZONE_DWELL, `metadata.queue_depth` |
| `/funnel`       | `event_type ∈ {ENTRY, REENTRY, ZONE_ENTER, BILLING_QUEUE_JOIN}` + POS join            |
| `/heatmap`      | `zone_id`, `event_type ∈ {ZONE_ENTER, ZONE_DWELL}`, `dwell_ms`                        |
| `/anomalies`    | `metadata.queue_depth`, `event_type=BILLING_QUEUE_JOIN` timestamps, missing ZONE_ENTER|

Open-ended `metadata: dict[str, Any]` lets us carry type-specific extras
(`queue_depth`, `sku_zone`, `session_seq`) without schema churn — Pydantic
still enforces the outer contract. `event_id` is a `UUID` so the pipeline
can mint IDs offline and the API can dedupe without coordination.

## 5. Storage & idempotency

Postgres is chosen over Redis / flat files because:
- **Primary-key idempotency is free** — `PRIMARY KEY (event_id)` plus
  `ON CONFLICT DO NOTHING` gives bit-exact re-run safety. A re-runnable
  pipeline is essential for a demo environment.
- **Secondary indexes on `(store_id, timestamp)` and `(event_type, ...)`**
  make the analytics endpoints O(log n) rather than full scans.
- **Asyncpg + SQLAlchemy async** keeps the FastAPI event loop responsive
  under concurrent ingest + reads.
- Tests run against SQLite-async (`aiosqlite`) for zero-setup isolation;
  schema is defined once in `app/db.py` and replayed on both dialects.

## 6. Observability

Every request emits one JSON log line with `trace_id`, `endpoint`,
`store_id`, `latency_ms`, `event_count`, `status_code`. `x-trace-id` is
propagated on response headers for end-to-end correlation. `/health`
reports per-store last-event timestamps and flags `STALE_FEED` for any
store whose feed is >10 min silent.

## 7. Error handling

Four global exception handlers in `app/errors.py` never leak a stack
trace:

- `RequestValidationError` → 422 with per-field `detail`.
- `SQLAlchemyError` → **503 with `request_id`** — the API stays up even if
  the DB blips.
- `HTTPException` and the catch-all `Exception` → safe `{error, request_id}`.

## 8. Testing strategy

- **Pure-Python unit tests** for geometry, line crossing, zone state
  machines, reentry cache, emitter buffering — these have zero dependency
  on torch/opencv and run in <1 s.
- **In-process HTTP tests** for every endpoint via `httpx.AsyncClient`
  over `ASGITransport`, with a fresh SQLite DB per test for isolation.
- **Partial-success, 413, 422, idempotency, and staff-exclusion** all
  covered as named cases.
- Coverage is **84%** against `app/ + pipeline/`, with CV-runtime modules
  (`detect.py`, `tracker.py`, `synth_pos.py`, `staff.py`) excluded from
  the line-count because they depend on heavy ML wheels and are exercised
  by the end-to-end demo, not unit tests.

## 9. AI-Assisted Decisions

This codebase was designed and implemented with LLM collaboration. Three
places where the LLM's first suggestion was overridden:

1. **Event schema — LLM proposed a separate `PersonEvent` and `ZoneEvent`
   table split**. We collapsed them into a single `events` row keyed by
   `event_id` with open `metadata`, because the analytics queries
   repeatedly join across event types (e.g. `ENTRY` → `BILLING_QUEUE_JOIN`
   → POS) and a split schema would force unions in every endpoint.

2. **Staff detection — LLM defaulted to "run CLIP zero-shot on each
   bounding box"**. At 5 fps × 5 cameras this would dominate runtime and
   require a second GPU-sized model. We chose HSV uniform match + a
   dwell-pattern heuristic (noted in `CHOICES.md`), keeping CLIP as an
   optional fallback.

3. **Re-entry handling — LLM proposed per-frame cosine similarity against
   every active track**. We bounded the search to a 5-minute sliding
   cache so the cost is O(live_candidates) not O(all_history). This also
   matches the business intent of "if a shopper re-enters within a short
   window, don't double-count".

## 10. What's intentionally out of scope

- **Cross-store federation** — the brief focuses on one store; the schema
  scales (every event is `store_id`-keyed), but there's no store-to-store
  reconciliation logic.
- **Authentication** — challenge didn't ask for it; `/health` is open.
- **Training a custom detector** — pretrained RT-DETR-L is sufficient for
  a person-only class and gives better boxes than YOLOv8 on crowded frames.
- **Kubernetes / tracing / Prometheus** — we stop at structured JSON logs.

(Word count: ~760.)
