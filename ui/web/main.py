"""
Tableau to Power BI Migration Accelerator — FastAPI Backend
Mastech Digital | Office of the CTO

Run from project root:
    uvicorn ui.web.main:app --reload --port 8000
"""
import asyncio
import io
import json
import os
import re
import sys
import threading
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
import uvicorn

# ── Paths ─────────────────────────────────────────────────────────────────────
STATIC_DIR   = Path(__file__).parent / "static"
INPUT_FOLDER = ROOT / "input_workbooks"
OUTPUT_FOLDER = ROOT / "pbi_output"
LOGO_PATH    = ROOT / "ui" / "ui_template" / "mastech-logo.png"

for d in (STATIC_DIR, INPUT_FOLDER, OUTPUT_FOLDER):
    d.mkdir(exist_ok=True)

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Tableau → Power BI Accelerator")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ── Job store ─────────────────────────────────────────────────────────────────
jobs: Dict[str, Dict[str, Any]] = {}

# ── Helpers ───────────────────────────────────────────────────────────────────
_INVALID_PATH_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

def safe_name(name: str, max_len: int = 80) -> str:
    s = _INVALID_PATH_CHARS.sub("_", name)
    s = re.sub(r"_+", "_", s).strip("_. ")
    return s[:max_len] or "output"


# ── Static routes ─────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = STATIC_DIR / "index.html"
    if not html_path.exists():
        return HTMLResponse("<h1>UI not built yet.</h1>", status_code=503)
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/logo")
async def logo():
    if LOGO_PATH.exists():
        return FileResponse(LOGO_PATH, media_type="image/png")
    raise HTTPException(404, "Logo not found")


# ── API: status ───────────────────────────────────────────────────────────────

@app.get("/api/status")
async def api_status():
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    return {"api_active": bool(api_key)}


# ── API: files ────────────────────────────────────────────────────────────────

@app.get("/api/files")
async def list_files():
    seen: set = set()
    files: List[Dict] = []
    for pattern in ("*.tw*", "*.td*"):
        for p in sorted(INPUT_FOLDER.glob(pattern), key=lambda x: x.name.lower()):
            if p.is_file() and p.name not in seen:
                seen.add(p.name)
                files.append({
                    "name": p.name,
                    "size": p.stat().st_size,
                })
    return {"files": files}


@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    allowed = {".twb", ".twbx", ".tds", ".tdsx"}
    ext = Path(file.filename).suffix.lower()
    if ext not in allowed:
        raise HTTPException(400, f"Unsupported type: {ext}")
    content = await file.read()
    (INPUT_FOLDER / file.filename).write_bytes(content)
    return {"name": file.filename, "size": len(content), "ok": True}


@app.delete("/api/files/{name}")
async def delete_file(name: str):
    p = INPUT_FOLDER / name
    if not p.exists():
        raise HTTPException(404, "File not found")
    p.unlink()
    return {"ok": True}


# ── API: migration ────────────────────────────────────────────────────────────

@app.post("/api/migrate")
async def start_migration(body: dict):
    files: List[str] = body.get("files", [])
    if not files:
        raise HTTPException(400, "No files specified")

    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": "running",
        "events": [],
        "file_results": [],
        "files": files,
        "project_name": body.get("project_name", "MigratedReport"),
        "output_format": body.get("output_format", "pbip"),
        "created_at": datetime.now().isoformat(),
    }

    thread = threading.Thread(
        target=_run_migration,
        args=(job_id,),
        daemon=True,
    )
    thread.start()
    return {"job_id": job_id}


def _emit(job_id: str, event: dict) -> None:
    if job_id in jobs:
        jobs[job_id]["events"].append(event)


def _run_migration(job_id: str) -> None:
    job   = jobs[job_id]
    files = job["files"]
    project_name  = job["project_name"]
    output_format = job["output_format"]

    # Deferred imports so server starts even if accelerator has missing deps
    try:
        from accelerator.ingestion           import ingest
        from accelerator.parser              import parse
        from accelerator.analyzer            import analyze
        from accelerator.pipeline            import MigrationPipeline
        from accelerator.generators          import generate_tmdl, generate_m, generate_report, generate_security
        from accelerator.generators.packager import package, build_migration_report
    except ImportError as exc:
        _emit(job_id, {"type": "fatal", "message": f"Import error: {exc}"})
        job["status"] = "error"
        return

    STEPS = [
        "Ingesting file",
        "Parsing Tableau XML",
        "Analyzing complexity & anti-patterns",
        "Translating — deterministic rules",
        "Translating — AI (LOD / table calcs → DAX)",
        "Generating TMDL · Power Query M · Report JSON",
        "Packaging output",
        "Migration complete",
    ]

    for file_idx, file_name in enumerate(files):
        file_path = INPUT_FOLDER / file_name

        def push(step: int, msg: str = "", status: str = "running") -> None:
            _emit(job_id, {
                "type": "step",
                "file": file_name,
                "file_index": file_idx,
                "total_files": len(files),
                "step": step,
                "step_name": STEPS[step],
                "status": status,
                "message": msg,
            })

        if not file_path.exists():
            _emit(job_id, {"type": "file_error", "file": file_name,
                            "message": "File not found"})
            job["file_results"].append({"file": file_name, "success": False,
                                         "error": "File not found"})
            continue

        try:
            # 0 — Ingest
            push(0, f"Reading {file_name}…")
            artifact = ingest(file_path)
            push(0, f"{file_path.stat().st_size // 1024} KB read", "done")

            # 1 — Parse
            push(1, "Extracting data sources, worksheets, dashboards…")
            unit = parse(artifact)
            ds_n  = len(unit.data_sources)
            ws_n  = len(unit.worksheets)
            db_n  = len(unit.dashboards)
            cf_n  = sum(len([c for c in ds.columns if c.is_calculated]) for ds in unit.data_sources)
            push(1, f"{ds_n} sources · {ws_n} worksheets · {db_n} dashboards · {cf_n} calc fields", "done")

            # 2 — Analyze
            push(2, "Scoring complexity, detecting anti-patterns, inferring schema…")
            analysis = analyze(unit)
            ap_n = len(analysis.anti_patterns)
            push(2, f"{ap_n} anti-patterns detected", "done")

            # 3 — Deterministic translation
            push(3, "Mapping connections → Power Query M · visuals → PBI JSON…")
            pipeline     = MigrationPipeline(
                output_base=str(OUTPUT_FOLDER),
                project_name=project_name,
                output_format=output_format,
            )
            translations = pipeline.translate(unit)
            det_n = sum(1 for t in translations if t.method == "deterministic")
            push(3, f"{det_n} artifacts translated deterministically", "done")

            # 4 — LLM translation
            push(4, "Claude Sonnet translating LOD expressions & table calcs → DAX…")
            llm_n    = sum(1 for t in translations if t.method == "llm")
            manual_n = sum(1 for t in translations if t.method == "manual")
            if llm_n:
                push(4, f"{llm_n} via LLM · {manual_n} queued for review", "done")
            elif manual_n:
                push(4, f"No API key — {manual_n} complex formula(s) → review queue", "done")
            else:
                push(4, "No complex formulas requiring LLM", "done")

            # 5 — Generate
            push(5, "Writing TMDL semantic model · Power Query M scripts · report pages…")
            out_dir = OUTPUT_FOLDER / safe_name(file_path.stem)
            out_dir.mkdir(parents=True, exist_ok=True)
            translations += generate_tmdl(unit, out_dir)
            translations += generate_m(unit, out_dir)
            translations += generate_report(unit, out_dir)
            translations += generate_security(unit, out_dir)
            push(5, f"Output directory: {out_dir.name}/", "done")

            # 6 — Package
            push(6, "Assembling package · review queue · migration report…")
            report = build_migration_report(unit, translations, out_dir)
            package(project_name, unit, translations, out_dir)

            pbit_path: Optional[str] = None
            if output_format in ("pbit", "both"):
                try:
                    from accelerator.generators.pbit_generator import build_pbit
                    pbit_path = str(build_pbit(project_name, unit, translations, out_dir))
                except Exception:
                    pass

            # 7 — Done
            push(7, "All artifacts written successfully!", "done")

            stats  = report.get("translation_stats", {})
            inv    = report.get("inventory", {})
            rq_sum = report.get("review_queue_summary", {})

            job["file_results"].append({
                "file": file_name,
                "success": True,
                "output_dir": str(out_dir),
                "report": report,
                "pbit_path": pbit_path,
                "stats": {
                    "data_sources":       inv.get("data_sources", 0),
                    "worksheets":         inv.get("worksheets", 0),
                    "dashboards":         inv.get("dashboards", 0),
                    "calculated_fields":  inv.get("calculated_fields", 0),
                    "coverage_pct":       stats.get("coverage_pct", 0),
                    "average_confidence": stats.get("average_confidence", 0),
                    "blockers":           rq_sum.get("blockers", 0),
                    "needs_review":       rq_sum.get("needs_review", 0),
                },
            })

        except Exception as exc:
            import traceback as tb_mod
            tb_str = tb_mod.format_exc()
            _emit(job_id, {
                "type": "file_error",
                "file": file_name,
                "message": str(exc),
                "traceback": tb_str,
            })
            job["file_results"].append({
                "file": file_name,
                "success": False,
                "error": str(exc),
                "traceback": tb_str,
            })

    job["status"] = "done"
    _emit(job_id, {"type": "all_done"})


@app.get("/api/migrate/{job_id}/progress")
async def migration_progress(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")

    async def stream():
        cursor = 0
        while True:
            job    = jobs.get(job_id, {})
            events = job.get("events", [])
            if len(events) > cursor:
                for evt in events[cursor:]:
                    yield f"data: {json.dumps(evt)}\n\n"
                cursor = len(events)
            if job.get("status") in ("done", "error"):
                break
            await asyncio.sleep(0.12)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/migrate/{job_id}/result")
async def migration_result(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    job = jobs[job_id]
    return {"status": job["status"], "files": job.get("file_results", [])}


# ── API: downloads ────────────────────────────────────────────────────────────

@app.get("/api/download/{job_id}/{file_name}/zip")
async def download_zip(job_id: str, file_name: str):
    result = _get_result(job_id, file_name)
    out_dir = Path(result["output_dir"])

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in out_dir.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(out_dir.parent))
    buf.seek(0)

    stem = Path(file_name).stem
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe_name(stem)}_pbi_output.zip"'},
    )


@app.get("/api/download/{job_id}/{file_name}/pbit")
async def download_pbit(job_id: str, file_name: str):
    result = _get_result(job_id, file_name)
    pbit_p = result.get("pbit_path")
    if not pbit_p or not Path(pbit_p).exists():
        raise HTTPException(404, "PBIT file not found")
    return FileResponse(pbit_p, media_type="application/octet-stream",
                        filename=Path(pbit_p).name)


def _get_result(job_id: str, file_name: str) -> dict:
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    result = next(
        (r for r in jobs[job_id].get("file_results", []) if r["file"] == file_name),
        None,
    )
    if not result or not result.get("success"):
        raise HTTPException(404, "Result not available")
    return result


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
