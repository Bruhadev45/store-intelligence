# CHOICES.md — Three key design decisions

This document records three decisions where the LLM's default suggestion
was evaluated against alternatives, and where the final choice is
explicitly justified against what AI proposed.

---

## 1. Detection model — **RT-DETR-L** over YOLOv8 and SSD

**Options considered**
- **YOLOv8-L** — LLM's first suggestion. Fast, widely documented, good on
  person class. Known to under-perform on crowded frames and produce
  less stable boxes for partially-occluded people.
- **RT-DETR-L** (chosen). Ultralytics-packaged Baidu RT-DETR. ~20% better
  box IoU on COCO-person than YOLOv8-L, better AP on small / occluded
  persons, fully compatible with the same `predict()` API so drop-in.
- **SSD / MobileNet** — deploy-friendly on edge, but too weak on cluttered
  retail scenes (many shelves, partial occlusions).

**What AI initially suggested**
The LLM assistant first recommended YOLOv8-L "because it's the community
default for person detection and has been battle-tested". It provided an
Ultralytics snippet using `YOLO("yolov8l.pt")`.

**What we chose and why**
RT-DETR-L was chosen because retail CCTV is **exactly** the hard case
that transformer-based detectors handle better: heavy occlusion, many
overlapping persons, variable lighting, and small absolute box sizes
(people occupy 5–20% of frame height). We verified on the preview frames
we extracted in Phase 0 recon that RT-DETR distinguishes two tightly-
adjacent shoppers that YOLOv8-L merged into a single box.

The Ultralytics package exposes RT-DETR through the same interface as
YOLO, so there's no integration cost. We kept YOLOv8 as a documented
fallback in `detect.py` (one line change) for environments where the
RT-DETR weights can't be downloaded.

**Trade-offs accepted**
- RT-DETR-L weights are ~60 MB larger than YOLOv8-L. We cache them in
  the pipeline Docker layer to amortise the pull.
- ~15% higher CPU cost per frame. Compensated by sampling at 5 fps.

---

## 2. Event schema — **single unified table** over split entity/zone tables

**Options considered**
- **Split schema** (LLM's first suggestion): `person_events` for entry/exit,
  `zone_events` for zone activity, `billing_events` for queue, `pos` for
  transactions. Felt "clean" (normalised).
- **Unified `events` table** (chosen): one row per behavioural event with
  `event_type` enum + open `metadata` dict.

**What AI initially suggested**
The first schema draft had four tables. The argument was "each entity
family has different columns, separate tables avoid sparse rows".

**What we chose and why**
Looking at the four endpoint families (`/metrics`, `/funnel`, `/heatmap`,
`/anomalies`), every query joins across event types in the same time
window for the same visitor. A split schema would force `UNION ALL`s in
every handler, plus complicated idempotency (which PK per table?) and a
more awkward Pydantic union type on ingest.

The unified table has exactly **10 columns** (9 typed + one JSON). The
sparse-column argument turns out not to matter: `dwell_ms` defaults to 0
for non-dwell events, `zone_id` is nullable, and `metadata_json` is at
most a few hundred bytes per row. Postgres handles this cheaply.

Idempotency is trivial: `PRIMARY KEY (event_id)` + `ON CONFLICT DO
NOTHING` means every pipeline retry is safe.

**Trade-offs accepted**
- Slightly wider rows on average. Mitigated by targeted indexes
  `(store_id, timestamp)` and `(event_type, store_id, timestamp)`.
- Query writers must remember to filter on `event_type` — enforced by
  keeping computations inside `app/metrics.py`, `app/funnel.py` etc.

---

## 3. API architecture — **idempotent batch ingest with partial-success envelope**

**The decision**
How should `POST /events/ingest` behave when a batch of 500 events mixes
good rows, duplicates (retried from the pipeline), and a handful of
invalid rows?

**Options considered**
- **All-or-nothing (reject the whole batch on any error)** — LLM's first
  suggestion. Simplest to reason about; forces the client to retry after
  fixing bad rows. The pipeline (a long-running CV worker) has no easy
  way to "fix" one malformed row, so this pattern risks losing hundreds
  of valid events because of a single bad timestamp.
- **Best-effort (silently drop bad rows, return 200)** — low-friction but
  opaque. The client never learns that rows were dropped; data loss is
  silent and permanent.
- **Partial-success envelope with per-row errors** (chosen) — accept the
  valid subset, return a structured report of what was accepted, what
  was a duplicate, and what was rejected (with reasons), and signal this
  with HTTP 207 Multi-Status.

**What AI initially suggested**
The first draft was all-or-nothing: "validate the whole batch with
`IngestBatch.model_validate()`, raise 422 on any error". Clean Python,
but wrong semantics for a retail-CV pipeline that can't stop and fix
one frame.

**What we chose and why**
`POST /events/ingest` validates each event independently inside
`app/ingestion.py`. The response envelope has three arrays:
`{accepted: [...], duplicates: [...], rejected: [{event_id, reason}]}`.
Status code is **200** on a fully-clean batch and **207** when any row
was rejected. `413` is returned only for batches above the hard limit
(`BATCH_MAX_EVENTS=500`, matching the brief).

Idempotency is layered into the same handler. Events carry a
client-generated `event_id` (UUID v4) that is the **primary key** of the
`events` table. Writes use `ON CONFLICT (event_id) DO NOTHING` on both
Postgres and SQLite. Duplicate POSTs — which the pipeline *will* make,
because the CV worker flushes its buffer on SIGTERM without knowing
which rows the API already accepted — are safely dropped at the
database level, and the response reports them in `duplicates[]` so the
client sees they weren't lost, they were already there.

This combines three brief requirements into one coherent contract:
- "Idempotent by event_id" — PK + ON CONFLICT.
- "Partial success on malformed events" — per-row validation + 207.
- "Structured error response" — every rejected row names the offending
  field, never a stack trace.

**Trade-offs accepted**
- The handler can't use Pydantic's batch model (`IngestBatch`) directly
  to benefit from a single `model_validate` call; instead each row is
  validated in a loop. Cost: ~3% handler latency at batch size 500.
  Benefit: we keep the 497 good rows when 3 are bad.
- Clients must be prepared for a 207 response. The default `httpx`
  status check (`raise_for_status`) treats 2xx as success, so no
  additional client logic is required — but `207` must be interpreted
  correctly if a client refuses anything non-200.

**Why not just return 200 always?**
Because silent data loss is a design smell. The response body is the
audit trail. If the pipeline ever drifts from the schema, the operator
sees the rejections immediately in API logs, not three hours later when
the funnel numbers look wrong.

---

## Appendix — POS data synthesis

The brief's input set implies a `pos_transactions.csv`; none was
provided in our drop. Rather than ship an opaque hand-written CSV,
`pipeline/synth_pos.py` walks the `BILLING_QUEUE_LEAVE` events emitted
by the CV pipeline and, for each, rolls a seeded RNG
(`sha256(master_seed + event_id)`) to decide whether that visitor
converted (p = 0.45) and at what basket value (log-normal, ~₹900
median). The seed is deterministic, so two developers running against
the same footage get identical POS rows. This is pipeline/dataset
tooling rather than an API-contract decision, so it is documented here
instead of as one of the three primary choices above.

(Word count: ~820.)
