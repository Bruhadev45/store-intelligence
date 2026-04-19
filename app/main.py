"""FastAPI entrypoint for Apex Retail Store Intelligence.

Endpoints:
  GET  /health
  POST /events/ingest
  GET  /stores/{id}/metrics
  GET  /stores/{id}/funnel
  GET  /stores/{id}/heatmap
  GET  /stores/{id}/anomalies
  POST /pos/ingest                (synth POS rows during demo)
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .anomalies import detect_anomalies
from .db import create_all, dispose, pos_transactions_table, session_scope
from .errors import register_error_handlers
from .funnel import compute_funnel
from .health import health_snapshot
from .heatmap import compute_heatmap
from .ingestion import BatchTooLarge, ingest_events
from .logging_mw import StructuredLoggingMiddleware, configure_logging
from .metrics import compute_store_metrics
from .models import POSTransaction


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    await create_all()
    yield
    await dispose()


app = FastAPI(
    title="Apex Retail Store Intelligence",
    version="0.1.0",
    lifespan=lifespan,
)
app.add_middleware(StructuredLoggingMiddleware)
register_error_handlers(app)

# Mount the web dashboard at "/" if present.
_DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard" / "web"
if _DASHBOARD_DIR.is_dir():
    @app.get("/", include_in_schema=False)
    async def _dashboard_index() -> FileResponse:
        return FileResponse(_DASHBOARD_DIR / "index.html")

    app.mount(
        "/static",
        StaticFiles(directory=str(_DASHBOARD_DIR)),
        name="dashboard-static",
    )


@app.get("/health")
async def health() -> JSONResponse:
    snap = await health_snapshot()
    # Brief: "DB unavailable → 503 with structured body".
    status_code = 503 if snap.get("database") != "ok" else 200
    return JSONResponse(status_code=status_code, content=snap)


@app.post("/events/ingest")
async def events_ingest(payload: dict[str, Any], request: Request) -> JSONResponse:
    # Accept either {"events": [...]} or a bare list for flexibility.
    events_list = payload.get("events") if isinstance(payload, dict) else payload
    if not isinstance(events_list, list):
        raise HTTPException(status_code=422, detail="body must contain an events list")

    try:
        result = await ingest_events(events_list)
    except BatchTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc

    request.state.event_count = result.accepted
    status_code = 200 if not result.rejected else 207  # multi-status on partial
    return JSONResponse(status_code=status_code, content=result.model_dump(mode="json"))


@app.post("/pos/ingest")
async def pos_ingest(payload: dict[str, Any]) -> dict[str, Any]:
    rows = payload.get("transactions") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise HTTPException(status_code=422, detail="body must contain a transactions list")
    validated = [POSTransaction.model_validate(r) for r in rows]
    if not validated:
        return {"accepted": 0}

    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    values = [
        {
            "transaction_id": t.transaction_id,
            "store_id": t.store_id,
            "visitor_id": t.visitor_id,
            "timestamp": t.timestamp,
            "basket_value": t.basket_value,
            "items_count": t.items_count,
        }
        for t in validated
    ]
    async with session_scope() as s:
        dialect = s.bind.dialect.name if s.bind else ""
        stmt: Any
        if dialect == "postgresql":
            stmt = pg_insert(pos_transactions_table).values(values).on_conflict_do_nothing(
                index_elements=[pos_transactions_table.c.transaction_id]
            )
        elif dialect == "sqlite":
            stmt = sqlite_insert(pos_transactions_table).values(values).on_conflict_do_nothing(
                index_elements=[pos_transactions_table.c.transaction_id]
            )
        else:
            stmt = pos_transactions_table.insert().values(values)
        await s.execute(stmt)
    return {"accepted": len(validated)}


@app.get("/stores/{store_id}/metrics")
async def store_metrics(store_id: str) -> dict[str, Any]:
    m = await compute_store_metrics(store_id)
    return m.to_dict()


@app.get("/stores/{store_id}/funnel")
async def store_funnel(store_id: str) -> dict[str, Any]:
    return await compute_funnel(store_id)


@app.get("/stores/{store_id}/heatmap")
async def store_heatmap(store_id: str) -> dict[str, Any]:
    return await compute_heatmap(store_id)


@app.get("/stores/{store_id}/anomalies")
async def store_anomalies(store_id: str) -> dict[str, Any]:
    anomalies = await detect_anomalies(store_id)
    return {"store_id": store_id, "anomalies": anomalies, "count": len(anomalies)}
