"""HTTP API + static UI. One process serves everything: run `uvicorn app.main:app`."""

import asyncio
import hashlib
import json
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
import fitz
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import db, rules
from app.config import get_settings
from app.master import load_master
from app.pipeline import MODES, STAGES, review_run, run_pipeline

MAX_UPLOAD = 10 * 1024 * 1024

SAMPLES = {
    "01_happy_sharma_office_supplies.pdf": ("Happy path", "Sharma Office Supplies",
                                            "Clean digital invoice, explicit PO, prices and GST exactly as ordered."),
    "02_edge_split_po_overbilled_kaveri.pdf": ("Edge case 1", "Kaveri Logistics - part 2 of a split PO",
                                               "Fine on its own, but together with part 1 it bills more than the PO."),
    "03_edge_duplicate_resend_iyer.pdf": ("Edge case 2", "Iyer Facility - re-sent invoice",
                                          "Already-paid invoice re-sent with a new date and the number written differently."),
    "04_edge_scanned_no_po_deccan.pdf": ("Edge case 3", "Deccan Packaging - scanned, no PO",
                                         "Image-only scan, prices include GST, no PO number printed."),
    "05_edge_vendor_impersonation_sharma.pdf": ("Edge case 4", "\"Sharma Office Supplies\" - new bank",
                                                "Known vendor name, but a different GSTIN and a 'changed' bank account."),
    "06_edge_price_creep_mehta.pdf": ("Bonus", "Mehta Electricals - price creep",
                                      "One line priced 6% above the PO; the other lines are exact."),
}

settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="Ledgerline - AP invoice desk", lifespan=lifespan)


def _start_run(data: bytes, file_name: str, source: str, mode: str) -> int:
    if mode not in MODES:
        raise HTTPException(400, f"mode must be one of {list(MODES)}")
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, "Please upload a PDF file.")
    digest = hashlib.sha256(data).hexdigest()
    path = settings.uploads_dir / f"{digest}.pdf"
    path.write_bytes(data)
    run_id = db.create_run(file_name, digest, str(path), source, mode)
    threading.Thread(target=run_pipeline, args=(run_id, data, digest, mode), daemon=True).start()
    return run_id


@app.post("/api/runs")
async def create_run(file: UploadFile | None = File(None), sample: str | None = Form(None),
                     mode: str = Form("auto")) -> dict:
    if sample:
        if sample not in SAMPLES:
            raise HTTPException(404, "unknown sample")
        data, name, source = (settings.samples_dir / sample).read_bytes(), sample, "sample"
    elif file is not None:
        data = await file.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, "File is larger than 10 MB.")
        name, source = Path(file.filename or "upload.pdf").name, "upload"
    else:
        raise HTTPException(400, "Send a PDF file or choose a sample.")
    return {"id": _start_run(data, name, source, mode)}


@app.get("/api/runs")
def list_runs() -> list[dict]:
    return db.list_runs()


@app.get("/api/runs/{run_id}")
def get_run(run_id: int) -> dict:
    run = db.get_run(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    run["events"] = db.events_after(run_id, 0)
    return run


@app.get("/api/runs/{run_id}/events")
async def stream_events(run_id: int) -> StreamingResponse:
    """Server-Sent Events: each stage event is pushed as soon as the pipeline writes it."""
    if db.get_run(run_id) is None:
        raise HTTPException(404, "run not found")

    async def generate():
        last = 0
        while True:
            for event in db.events_after(run_id, last):
                last = event["id"]
                event.pop("data_json", None)
                yield f"data: {json.dumps(event)}\n\n"
            run = db.get_run(run_id)
            if run["status"] != "running" and not db.events_after(run_id, last):
                yield f"event: done\ndata: {json.dumps({'status': run['status']})}\n\n"
                return
            await asyncio.sleep(0.15)

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/runs/{run_id}/pdf")
def run_pdf(run_id: int) -> FileResponse:
    run = db.get_run(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    return FileResponse(settings.uploads_dir / f"{run['file_hash']}.pdf", media_type="application/pdf")


class ReviewBody(BaseModel):
    action: str
    note: str = ""


@app.post("/api/runs/{run_id}/review")
def review(run_id: int, body: ReviewBody) -> dict:
    if body.action not in ("approve", "reject"):
        raise HTTPException(400, "action must be approve or reject")
    try:
        return review_run(run_id, body.action, body.note.strip()[:500])
    except LookupError:
        raise HTTPException(404, "run not found")
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@app.get("/api/meta")
def meta() -> dict:
    master = load_master()
    return {
        "company": master.company,
        "ai_configured": settings.ai_configured,
        "modes": MODES,
        "stages": [{"key": key, "label": label} for key, label, _ in STAGES],
        "policy": [{"rule": rule, "text": text} for rule, text in rules.POLICY],
        "samples": [{"file": name, "tag": tag, "title": title, "about": about}
                    for name, (tag, title, about) in SAMPLES.items()],
    }


@app.get("/api/reference")
def reference() -> dict:
    master = load_master()
    pos = []
    for po in master.purchase_orders.values():
        used = db.po_consumption(po.po_number)
        pos.append({
            "po_number": po.po_number, "vendor_id": po.vendor_id,
            "vendor": master.vendors[po.vendor_id].legal_name, "date": po.po_date, "status": po.status,
            "description": po.description, "owner": po.owner, "value": po.subtotal,
            "approved": used["approved_subtotal"], "pending": used["pending_subtotal"], "invoices": used["invoices"],
            "lines": [{**line.__dict__, "amount": line.amount, "billed": used["approved_qty_by_line"].get(line.line_no, 0)}
                      for line in po.lines],
        })
    vendors = [{k: v for k, v in vendor.__dict__.items()} for vendor in master.vendors.values()]
    return {"vendors": vendors, "purchase_orders": pos, "ledger": db.all_ledger()}


def _page_png(path: Path) -> Response:
    """First page as PNG, so the UI can show the document and stamp the decision on it."""
    document = fitz.open(path)
    png = document[0].get_pixmap(dpi=110).tobytes("png")
    return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=3600"})


@app.get("/api/runs/{run_id}/page.png")
def run_page(run_id: int) -> Response:
    run = db.get_run(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    try:
        return _page_png(settings.uploads_dir / f"{run['file_hash']}.pdf")
    except Exception:
        raise HTTPException(422, "this file cannot be rendered")


@app.get("/api/samples/{name}/page.png")
def sample_page(name: str) -> Response:
    if name not in SAMPLES:
        raise HTTPException(404, "unknown sample")
    return _page_png(settings.samples_dir / name)


@app.get("/api/samples/{name}")
def sample_pdf(name: str) -> FileResponse:
    if name not in SAMPLES:
        raise HTTPException(404, "unknown sample")
    return FileResponse(settings.samples_dir / name, media_type="application/pdf")


@app.post("/api/reset")
def reset() -> dict:
    db.reset_demo_data()
    return {"ok": True}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(settings.web_dir / "index.html")


app.mount("/static", StaticFiles(directory=settings.web_dir), name="static")
