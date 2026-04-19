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

## 3. POS data — **synthesised, seeded, documented** over faked-inline

**The issue**
The brief's expected input set implies a `pos_transactions.csv` exists
in the drop. It doesn't. Three options:
- **Skip POS entirely** — would render `/funnel` Purchase stage
  meaningless and conversion_rate unusable.
- **Hard-code a tiny CSV** (LLM's first suggestion) — "throw in 20 rows
  and move on". Hides the assumption from reviewers.
- **Synthesise transparently, with a seed, and document** (chosen).

**What AI initially suggested**
A one-liner: `pd.DataFrame([{"txn": "TXN1", ...}, ...]).to_csv(...)`.
This would ship a mystery CSV into the repo with no explanation of
where the rows came from.

**What we chose and why**
`pipeline/synth_pos.py` reads the actual `BILLING_QUEUE_LEAVE` events
emitted by the CV pipeline and, for each, rolls a seeded RNG (seeded by
`sha256(master_seed + event_id)`) to decide whether that visitor
converted (p = 0.45 by default). Basket value is log-normal(μ=6.8,
σ=0.6) (≈ ₹900 median, realistic for Purplle), items_count is geometric.

Because the seed is deterministic, two developers who run the pipeline
on the same footage get the same POS rows — reproducibility without
committing a "secret" CSV.

This **exposes the assumption explicitly** to any reviewer: conversion
rates are grounded on synthetic POS, but the coupling between billing
exits and POS rows is realistic (only people who queued get billed, with
a 30-180 s delay).

**Trade-offs accepted**
- If a real POS file ever lands, synth_pos.py becomes dead. Acceptable —
  it's replaceable in <10 lines.
- Reviewer needs to read this section to understand where POS came from.
  That's the *point* — transparency over convenience.

(Word count: ~700.)
