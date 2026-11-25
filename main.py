"""Convertitore di file: API web + frontend statico.

Flusso:
  1. il browser carica un file con POST /api/jobs (file + formato di destinazione)
  2. il server lo salva e mette la conversione in una coda interna (thread pool)
  3. il browser chiede lo stato con GET /api/jobs/{id} finché non è "done"
  4. il file convertito si scarica da GET /api/jobs/{id}/download
"""

import os
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import converters

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "500"))
FILE_TTL_MINUTES = int(os.environ.get("FILE_TTL_MINUTES", "60"))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "2"))

DATA_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Convertitore di file")
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)


@dataclass
class Job:
    id: str
    original_name: str
    src_ext: str
    target: str
    folder: Path
    status: str = "in coda"          # in coda | in corso | completato | errore
    progress: Optional[float] = None  # 0..1 quando si conosce (audio/video)
    error: Optional[str] = None
    created: float = field(default_factory=time.time)

    @property
    def src(self) -> Path:
        return self.folder / f"input.{self.src_ext}"

    @property
    def dst(self) -> Path:
        return self.folder / f"output.{self.target}"

    @property
    def download_name(self) -> str:
        return f"{Path(self.original_name).stem}.{self.target}"

    def public(self) -> dict:
        return {
            "id": self.id,
            "name": self.original_name,
            "target": self.target,
            "status": self.status,
            "progress": self.progress,
            "error": self.error,
            "download_name": self.download_name,
        }


jobs: dict[str, Job] = {}
jobs_lock = threading.Lock()


def get_job(job_id: str) -> Job:
    with jobs_lock:
        job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Conversione non trovata (forse è scaduta)")
    return job


def run_job(job: Job) -> None:
    job.status = "in corso"

    def on_progress(value: Optional[float]) -> None:
        job.progress = value

    try:
        converters.convert(job.src, job.dst, on_progress, title=Path(job.original_name).stem)
        if not job.dst.exists() or job.dst.stat().st_size == 0:
            raise converters.ConversionError("La conversione non ha prodotto alcun file")
        job.progress = 1.0
        job.status = "completato"
    except converters.ConversionError as exc:
        job.status, job.error = "errore", str(exc)
    except Exception as exc:  # non deve mai far cadere il worker
        job.status, job.error = "errore", f"Errore inatteso: {exc}"
    finally:
        job.src.unlink(missing_ok=True)  # l'originale non serve più


def cleanup_loop() -> None:
    """Cancella periodicamente le conversioni più vecchie di FILE_TTL_MINUTES."""
    while True:
        limit = time.time() - FILE_TTL_MINUTES * 60
        with jobs_lock:
            expired = [j for j in jobs.values() if j.created < limit and j.status in ("completato", "errore")]
            for job in expired:
                jobs.pop(job.id, None)
        for job in expired:
            shutil.rmtree(job.folder, ignore_errors=True)
        # Cartelle orfane (ad esempio dopo un riavvio del container)
        for folder in DATA_DIR.iterdir():
            if folder.is_dir() and folder.name not in jobs and folder.stat().st_mtime < limit:
                shutil.rmtree(folder, ignore_errors=True)
        time.sleep(300)


threading.Thread(target=cleanup_loop, daemon=True).start()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/formats")
def formats() -> dict:
    return {
        "map": converters.FORMAT_MAP,
        "categories": {ext: converters.category_of(ext) for ext in converters.FORMAT_MAP},
        "max_upload_mb": MAX_UPLOAD_MB,
        "ttl_minutes": FILE_TTL_MINUTES,
    }


@app.post("/api/jobs", status_code=201)
async def create_job(file: UploadFile = File(...), target: str = Form(...)) -> dict:
    name = Path(file.filename or "file").name
    src_ext = Path(name).suffix.lstrip(".").lower()
    target = target.lower().lstrip(".")

    if src_ext not in converters.FORMAT_MAP:
        raise HTTPException(415, f"Il formato .{src_ext or '?'} non è supportato")
    if target not in converters.FORMAT_MAP[src_ext]:
        raise HTTPException(400, f"Non posso convertire .{src_ext} in .{target}")

    job_id = uuid.uuid4().hex
    folder = DATA_DIR / job_id
    folder.mkdir()
    job = Job(id=job_id, original_name=name, src_ext=src_ext, target=target, folder=folder)

    # Salvataggio a pezzi, controllando la dimensione massima
    limit = MAX_UPLOAD_MB * 1024 * 1024
    written = 0
    with job.src.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            written += len(chunk)
            if written > limit:
                out.close()
                shutil.rmtree(folder, ignore_errors=True)
                raise HTTPException(413, f"File troppo grande (massimo {MAX_UPLOAD_MB} MB)")
            out.write(chunk)

    with jobs_lock:
        jobs[job_id] = job
    executor.submit(run_job, job)
    return job.public()


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    return get_job(job_id).public()


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str) -> FileResponse:
    job = get_job(job_id)
    if job.status != "completato":
        raise HTTPException(409, "Il file non è ancora pronto")
    return FileResponse(job.dst, filename=job.download_name)


@app.delete("/api/jobs/{job_id}", status_code=204)
def delete_job(job_id: str) -> None:
    job = get_job(job_id)
    if job.status in ("in coda", "in corso"):
        raise HTTPException(409, "Conversione ancora in corso")
    with jobs_lock:
        jobs.pop(job_id, None)
    shutil.rmtree(job.folder, ignore_errors=True)


# Il frontend (HTML/CSS/JS) è servito dalla stessa app
app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
