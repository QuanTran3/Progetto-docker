"""Logica di conversione: ogni categoria di file usa un tool diverso.

- immagini        -> Pillow (+ pillow-heif per le foto HEIC degli iPhone)
- audio e video   -> ffmpeg
- Markdown / HTML -> pandoc (e Gotenberg per arrivare al PDF)
- documenti Office -> Gotenberg (LibreOffice in un container separato)
"""

import os
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

import httpx
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

register_heif_opener()

GOTENBERG_URL = os.environ.get("GOTENBERG_URL", "http://gotenberg:3000")
CONVERSION_TIMEOUT = int(os.environ.get("CONVERSION_TIMEOUT_SECONDS", "900"))

# ---------------------------------------------------------------------------
# Formati supportati: estensione di ingresso -> formati di uscita possibili
# ---------------------------------------------------------------------------

IMAGE_IN = ["png", "jpg", "jpeg", "webp", "gif", "bmp", "tiff", "tif", "heic", "heif", "ico"]
IMAGE_OUT = ["jpg", "png", "webp", "gif", "bmp", "tiff", "ico", "pdf"]

VIDEO_IN = ["mp4", "mov", "mkv", "avi", "webm", "m4v", "flv", "wmv", "3gp", "mpeg", "mpg"]
VIDEO_OUT = ["mp4", "webm", "mkv", "gif", "mp3", "wav", "ogg", "m4a"]

AUDIO_IN = ["mp3", "wav", "ogg", "flac", "m4a", "aac", "opus", "wma", "aiff", "aif"]
AUDIO_OUT = ["mp3", "wav", "ogg", "flac", "m4a", "opus"]

OFFICE_IN = ["docx", "doc", "odt", "rtf", "txt", "pptx", "ppt", "odp", "xlsx", "xls", "ods"]
OFFICE_OUT = ["pdf"]

MARKUP_OUT = {
    "md": ["pdf", "html", "docx", "odt"],
    "markdown": ["pdf", "html", "docx", "odt"],
    "html": ["pdf", "md", "docx", "odt"],
    "htm": ["pdf", "md", "docx", "odt"],
}

CATEGORIES = {
    "immagine": IMAGE_IN,
    "video": VIDEO_IN,
    "audio": AUDIO_IN,
    "documento": OFFICE_IN + list(MARKUP_OUT),
}


def _build_map() -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    for ext in IMAGE_IN:
        mapping[ext] = IMAGE_OUT
    for ext in VIDEO_IN:
        mapping[ext] = VIDEO_OUT
    for ext in AUDIO_IN:
        mapping[ext] = AUDIO_OUT
    for ext in OFFICE_IN:
        mapping[ext] = OFFICE_OUT
    mapping.update(MARKUP_OUT)
    # Non ha senso convertire un file nello stesso formato
    norm = {"jpeg": "jpg", "tif": "tiff", "htm": "html", "markdown": "md", "aif": "aiff"}
    return {
        ext: [t for t in targets if t != norm.get(ext, ext)]
        for ext, targets in mapping.items()
    }


FORMAT_MAP = _build_map()


def category_of(ext: str) -> Optional[str]:
    for name, exts in CATEGORIES.items():
        if ext in exts:
            return name
    return None


class ConversionError(Exception):
    """Errore mostrato all'utente in modo leggibile."""


ProgressCallback = Callable[[Optional[float]], None]


def convert(src: Path, dst: Path, progress: ProgressCallback, title: str = "Documento") -> None:
    """Converte src in dst scegliendo il tool giusto in base alle estensioni."""
    src_ext = src.suffix.lstrip(".").lower()
    dst_ext = dst.suffix.lstrip(".").lower()

    if dst_ext not in FORMAT_MAP.get(src_ext, []):
        raise ConversionError(f"Conversione da .{src_ext} a .{dst_ext} non supportata")

    if src_ext in IMAGE_IN:
        convert_image(src, dst, dst_ext)
    elif src_ext in VIDEO_IN or src_ext in AUDIO_IN:
        convert_media(src, dst, dst_ext, progress)
    elif src_ext in OFFICE_IN:
        office_to_pdf(src, dst)
    elif src_ext in MARKUP_OUT:
        convert_markup(src, dst, src_ext, dst_ext, title)
    else:  # pragma: no cover - già escluso da FORMAT_MAP
        raise ConversionError("Formato non riconosciuto")


# ---------------------------------------------------------------------------
# Immagini
# ---------------------------------------------------------------------------

PIL_FORMAT = {
    "jpg": "JPEG", "png": "PNG", "webp": "WEBP", "gif": "GIF",
    "bmp": "BMP", "tiff": "TIFF", "ico": "ICO", "pdf": "PDF",
}


def _flatten(im: Image.Image) -> Image.Image:
    """Toglie la trasparenza mettendo uno sfondo bianco (per JPG, BMP, PDF)."""
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.getchannel("A"))
        return bg
    return im.convert("RGB")


def convert_image(src: Path, dst: Path, dst_ext: str) -> None:
    try:
        with Image.open(src) as im:
            animated = getattr(im, "is_animated", False)

            # GIF/WebP animati restano animati se il formato di uscita lo permette
            if animated and dst_ext in ("gif", "webp"):
                im.save(dst, PIL_FORMAT[dst_ext], save_all=True, loop=0)
                return

            im = ImageOps.exif_transpose(im)  # rispetta la rotazione delle foto
            options: dict = {}

            if dst_ext in ("jpg", "bmp", "pdf"):
                im = _flatten(im)
                if dst_ext == "jpg":
                    options = {"quality": 90, "optimize": True}
                if dst_ext == "pdf":
                    options = {"resolution": 150}
            elif dst_ext == "webp":
                options = {"quality": 85}
            elif dst_ext == "ico":
                # Le icone sono quadrate: centra l'immagine su uno sfondo trasparente
                im = im.convert("RGBA")
                side = max(im.size)
                square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
                square.paste(im, ((side - im.width) // 2, (side - im.height) // 2))
                im = square
                im.thumbnail((256, 256))
                options = {"sizes": [(s, s) for s in (16, 32, 48, 64, 128, 256) if s <= max(im.size)]}
            elif dst_ext == "gif":
                im = im.convert("P", palette=Image.Palette.ADAPTIVE)
            elif im.mode not in ("RGB", "RGBA", "L", "LA"):
                im = im.convert("RGBA")

            im.save(dst, PIL_FORMAT[dst_ext], **options)
    except (OSError, ValueError) as exc:
        raise ConversionError("Il file non è un'immagine valida oppure è danneggiato") from exc


# ---------------------------------------------------------------------------
# Audio e video (ffmpeg)
# ---------------------------------------------------------------------------

EVEN_SIZE = "scale=trunc(iw/2)*2:trunc(ih/2)*2"

FFMPEG_ARGS = {
    "mp4": ["-vf", EVEN_SIZE, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart"],
    "mkv": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-b:a", "160k"],
    "webm": ["-c:v", "libvpx-vp9", "-crf", "33", "-b:v", "0", "-deadline", "good",
             "-cpu-used", "5", "-row-mt", "1", "-c:a", "libopus", "-b:a", "128k"],
    "gif": ["-vf", "fps=12,scale='min(480,iw)':-1:flags=lanczos,split[a][b];[a]palettegen[p];[b][p]paletteuse",
            "-loop", "0", "-an"],
    "mp3": ["-vn", "-c:a", "libmp3lame", "-q:a", "2"],
    "wav": ["-vn", "-c:a", "pcm_s16le"],
    "ogg": ["-vn", "-c:a", "libvorbis", "-q:a", "5"],
    "flac": ["-vn", "-c:a", "flac"],
    "m4a": ["-vn", "-c:a", "aac", "-b:a", "192k"],
    "opus": ["-vn", "-c:a", "libopus", "-b:a", "128k"],
}


def media_duration(src: Path) -> Optional[float]:
    """Durata in secondi letta con ffprobe (serve per la barra di avanzamento)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(src)],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout.strip()
        return float(out) if out and out != "N/A" else None
    except (subprocess.SubprocessError, ValueError):
        return None


def has_audio(src: Path) -> bool:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(src)],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        return bool(out)
    except subprocess.SubprocessError:
        return True  # nel dubbio lascia provare ffmpeg


def convert_media(src: Path, dst: Path, dst_ext: str, progress: ProgressCallback) -> None:
    if dst_ext in AUDIO_OUT and not has_audio(src):
        raise ConversionError("Il file non contiene una traccia audio")
    duration = media_duration(src)
    # Gli argomenti sono una lista: il nome del file non passa mai da una shell
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
           "-i", str(src), *FFMPEG_ARGS[dst_ext],
           "-progress", "pipe:1", "-nostats", str(dst)]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    start = time.monotonic()
    assert proc.stdout is not None
    for line in proc.stdout:
        if time.monotonic() - start > CONVERSION_TIMEOUT:
            proc.kill()
            raise ConversionError("Conversione troppo lunga, interrotta")
        key, _, value = line.strip().partition("=")
        if key == "out_time_us" and duration and value.isdigit():
            progress(min(int(value) / 1_000_000 / duration, 0.99))

    proc.wait()
    if proc.returncode != 0:
        err = (proc.stderr.read() if proc.stderr else "").strip().splitlines()
        msg = err[-1] if err else "errore sconosciuto"
        raise ConversionError(f"ffmpeg: {msg}")


# ---------------------------------------------------------------------------
# Documenti (Gotenberg + pandoc)
# ---------------------------------------------------------------------------

def _gotenberg(route: str, filename: str, data: bytes, dst: Path) -> None:
    try:
        resp = httpx.post(
            f"{GOTENBERG_URL}{route}",
            files={"files": (filename, data)},
            timeout=CONVERSION_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise ConversionError(
            "Il servizio per i documenti (Gotenberg) non risponde. È avviato?"
        ) from exc
    if resp.status_code != 200:
        raise ConversionError(f"Gotenberg ha risposto con errore {resp.status_code}: {resp.text[:200]}")
    dst.write_bytes(resp.content)


def office_to_pdf(src: Path, dst: Path) -> None:
    # LibreOffice (dentro Gotenberg) sceglie il filtro in base all'estensione
    _gotenberg("/forms/libreoffice/convert", f"documento{src.suffix.lower()}", src.read_bytes(), dst)


def html_to_pdf(html: bytes, dst: Path) -> None:
    # La route Chromium di Gotenberg vuole un file chiamato esattamente index.html
    _gotenberg("/forms/chromium/convert/html", "index.html", html, dst)


PANDOC_FORMAT = {"md": "markdown", "markdown": "markdown", "html": "html", "htm": "html",
                 "docx": "docx", "odt": "odt"}

HTML_STYLE = """<style>
body{font-family:Georgia,serif;max-width:46rem;margin:2rem auto;padding:0 1rem;line-height:1.6;color:#222}
pre,code{font-family:Menlo,Consolas,monospace;background:#f4f4f4;border-radius:4px}
pre{padding:.8rem;overflow-x:auto} code{padding:.1rem .3rem}
table{border-collapse:collapse} td,th{border:1px solid #ccc;padding:.3rem .6rem}
img{max-width:100%}
</style>"""


def _pandoc(src: Path, dst: Path, src_fmt: str, dst_fmt: str, extra: list[str] | None = None) -> None:
    cmd = ["pandoc", str(src), "-f", src_fmt, "-t", dst_fmt, "-o", str(dst), *(extra or [])]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=True)
    except subprocess.CalledProcessError as exc:
        raise ConversionError(f"pandoc: {exc.stderr.strip()[:200]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ConversionError("pandoc ci sta mettendo troppo") from exc


def convert_markup(src: Path, dst: Path, src_ext: str, dst_ext: str, title: str) -> None:
    src_fmt = PANDOC_FORMAT[src_ext]

    if dst_ext == "pdf":
        if src_fmt == "html":
            html_to_pdf(src.read_bytes(), dst)
            return
        # Markdown -> HTML completo (pandoc) -> PDF (Chromium in Gotenberg)
        tmp_html = dst.with_suffix(".tmp.html")
        header = dst.with_suffix(".style.html")
        header.write_text(HTML_STYLE, encoding="utf-8")
        _pandoc(src, tmp_html, src_fmt, "html", ["--standalone", "--metadata", f"pagetitle={title}",
                                                  "--include-in-header", str(header)])
        try:
            html_to_pdf(tmp_html.read_bytes(), dst)
        finally:
            tmp_html.unlink(missing_ok=True)
            header.unlink(missing_ok=True)
        return

    target = {"md": "gfm"}.get(dst_ext, PANDOC_FORMAT.get(dst_ext, dst_ext))
    if dst_ext == "html":
        header = dst.with_suffix(".style.html")
        header.write_text(HTML_STYLE, encoding="utf-8")
        try:
            _pandoc(src, dst, src_fmt, target, ["--standalone", "--metadata", f"pagetitle={title}",
                                                "--include-in-header", str(header)])
        finally:
            header.unlink(missing_ok=True)
        return
    _pandoc(src, dst, src_fmt, target)
