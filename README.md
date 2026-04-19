# Apex Retail Store Intelligence

End-to-end pipeline that ingests raw CCTV footage, emits structured behavioural
events, and exposes a containerised FastAPI service with real-time analytics.

Built for the **Purplle Engineering Challenge** using **RT-DETR + ByteTrack**
for CV, **FastAPI + Pydantic v2 + Postgres** for the API, and a **rich
terminal dashboard** for live monitoring.

---

## Prerequisites

| Dependency       | Why                                                |
|------------------|----------------------------------------------------|
| Docker ≥ 24      | Runs the API + Postgres stack                      |
| Python 3.11+     | Host-side CV pipeline (not in API image)           |
| ~2 GB free disk  | Docker images + Postgres volume + model weights    |
| `CCTV Footage/`  | Directory of `CAM *.mp4` clips, beside the repo    |

**Model weights**: the first run of `pipeline/detect.py` will auto-download
`rtdetr-l.pt` (~66 MB) from Ultralytics' CDN into the current working
directory. No manual download needed; just have internet on first run.

**CCTV clips**: place the footage the challenge drop gave you at
`../CCTV Footage/` (sibling of the repo root) — `pipeline/run.sh` globs
`../CCTV Footage/CAM*.mp4`. Override the location with `CCTV_DIR=/path
bash pipeline/run.sh`.

## Quickstart (5 commands)

```bash
git clone https://github.com/Bruhadev45/store-intelligence.git && cd store-intelligence
docker compose up -d --build                                         # API + Postgres
python -m venv .venv && source .venv/bin/activate && pip install -r requirements-pipeline.txt
bash pipeline/run.sh                                                 # emit events from CCTV clips
curl -s http://localhost:8000/stores/STORE_001/metrics | jq          # see live metrics
```

The API is usable the moment `docker compose up` is healthy — endpoints return
empty-but-valid JSON even before the pipeline runs. For the live dashboard,
open <http://localhost:8000/> in a browser, or run
`python dashboard/live.py --store STORE_001` from a terminal.

## Architecture

```
 ┌────────────────┐     ┌──────────────────┐     ┌──────────────┐
 │ CCTV clips     │ ──▶ │ pipeline/        │ ──▶ │ POST /events │
 │ (CAM 1..5.mp4) │     │  RT-DETR + Byte  │     │  /ingest     │
 └────────────────┘     │  Track + zones   │     └──────┬───────┘
                        └──────────────────┘            │
                                                        ▼
                                               ┌──────────────────┐
                                               │ FastAPI (app/)   │
                                               │  Postgres (db)   │
                                               └──────┬───────────┘
                                                      │
                     ┌────────────────────────────────┼──────────────────┐
                     ▼                ▼               ▼                  ▼
                 /metrics         /funnel         /heatmap          /anomalies
                     │
                     ▼
          ┌──────────────────┐
          │ dashboard/live.py│
          └──────────────────┘
```

## Endpoints

| Method | Path                           | Purpose |
|--------|--------------------------------|---------|
| GET    | `/health`                      | Service status, per-store last event, STALE_FEED warnings |
| POST   | `/events/ingest`               | Batch ingest up to 500 events — idempotent on `event_id`, partial success |
| POST   | `/pos/ingest`                  | Batch ingest POS transactions (idempotent on `transaction_id`) |
| GET    | `/stores/{id}/metrics`         | Unique visitors, conversion, dwell, queue depth, abandonment |
| GET    | `/stores/{id}/funnel`          | Session funnel Entry → ZoneVisit → BillingQueue → Purchase |
| GET    | `/stores/{id}/heatmap`         | Zone visit counts, dwell, intensity [0,100], confidence |
| GET    | `/stores/{id}/anomalies`       | `BILLING_QUEUE_SPIKE` / `CONVERSION_DROP` / `DEAD_ZONE` |

## Running the CV pipeline

The pipeline deps (torch, ultralytics, supervision) are heavy and **not**
baked into the API image. Install them on the host:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-pipeline.txt
bash pipeline/run.sh
```

This will:

1. Run RT-DETR-L + ByteTrack on each `CCTV Footage/CAM *.mp4`
2. Apply `config/store_layout.json` zones, emit structured events
3. Write `data/events.jsonl` AND POST batches of 500 to `/events/ingest`
4. Synthesise `data/pos_transactions.csv` (see `docs/CHOICES.md`) and POST to `/pos/ingest`

## Testing

```bash
pytest --cov=app --cov-report=term
```

Coverage target: **≥70%** (brief requirement). Current: **84%** over `app/`
and `pipeline/` (32 tests, all async-SQLite for hermetic isolation). Swap
to Postgres-backed tests with `DATABASE_URL=postgresql+asyncpg://...`.

## Documentation

- [`docs/DESIGN.md`](docs/DESIGN.md) — architecture, schema rationale, AI-assisted decisions
- [`docs/CHOICES.md`](docs/CHOICES.md) — three key design decisions with trade-offs

## Repo layout

```
store-intelligence/
├── app/               # FastAPI service
├── pipeline/          # CV + event emission
├── dashboard/         # Rich terminal live view
├── config/            # store_layout.json, alembic
├── data/              # events.jsonl, pos_transactions.csv
├── tests/             # pytest suite
└── docs/              # DESIGN.md, CHOICES.md
```
