from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from complaints.models import Complaint, Document

if TYPE_CHECKING:
    from django.core.files.uploadedfile import UploadedFile

EXTRACT_TIMEOUT_SECONDS = 60
MIN_TEXT_CHARS = 20  # a page with fewer characters than this is treated as having no text layer
HINDI_LANG = "hin"
LATIN_LANG = "eng"
OCR_DPI = 300
LATIN_TEXT_MAX_CHARS = 5000

_MAGIC = (
    (b"%PDF-", "application/pdf"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
)


class UploadError(ValueError):
    pass


class DuplicateDocument(Exception):
    def __init__(self, existing: Document):
        super().__init__("This file is already attached to the complaint.")
        self.existing = existing


@dataclass
class Attachment:
    content: bytes
    filename: str
    mime_type: str
    extracted_text: str = ""
    text_status: str = Document.TextStatus.NONE
    page_count: int | None = None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


def sniff_mime(content: bytes) -> str | None:
    for magic, mime in _MAGIC:
        if content.startswith(magic):
            return mime
    return None


def read_upload(upload: UploadedFile) -> Attachment:
    """Validate size and real file type (not the client's claim) and wrap the bytes."""
    if upload.size > settings.MAX_UPLOAD_BYTES:
        limit_mb = settings.MAX_UPLOAD_BYTES // (1024 * 1024)
        raise UploadError(f"File is larger than {limit_mb} MB.")
    content = upload.read()
    mime = sniff_mime(content)
    if mime not in settings.ALLOWED_UPLOAD_TYPES:
        raise UploadError("Only PDF, JPG and PNG files are allowed.")
    return Attachment(content=content, filename=Path(upload.name).name[:255], mime_type=mime)


def storage_path(sha256: str, mime_type: str) -> str:
    ext = settings.ALLOWED_UPLOAD_TYPES[mime_type]
    return f"documents/{sha256[:2]}/{sha256[2:4]}/{sha256}{ext}"


def store_document(
    complaint: Complaint,
    attachment: Attachment,
    kind: str,
    actor: Any,
    supersedes: Document | None = None,
) -> Document:
    """Save the file under its hash (once on disk) and create an immutable Document row."""
    sha = attachment.sha256
    existing = Document.objects.filter(complaint=complaint, sha256=sha).first()
    if existing is not None:
        raise DuplicateDocument(existing)
    if supersedes is not None and supersedes.complaint_id != complaint.pk:
        raise ValueError("A document can only supersede one of the same complaint.")

    path = storage_path(sha, attachment.mime_type)
    if not default_storage.exists(path):
        default_storage.save(path, ContentFile(attachment.content))

    return Document.objects.create(
        office_id=complaint.office_id,
        complaint=complaint,
        kind=kind,
        file=path,
        sha256=sha,
        original_filename=attachment.filename,
        mime_type=attachment.mime_type,
        size_bytes=len(attachment.content),
        page_count=attachment.page_count,
        extracted_text=attachment.extracted_text,
        text_status=attachment.text_status,
        supersedes=supersedes,
        uploaded_by=actor,
    )


# ------------------------------------------------------------ text extraction


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argument list, no shell
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=EXTRACT_TIMEOUT_SECONDS,
        check=True,
    )


def _ocr(image: Path, lang: str) -> tuple[str, float]:
    """Run Tesseract in one language; returns (text, mean word confidence 0-100)."""
    rows = _run(["tesseract", str(image), "-", "-l", lang, "tsv"]).stdout.splitlines()[1:]
    lines: dict[tuple[str, str, str], list[str]] = {}
    confidences: list[float] = []
    for row in rows:
        cols = row.split("\t")
        if len(cols) < 12 or cols[0] != "5" or not cols[11].strip():
            continue
        confidences.append(float(cols[10]))
        lines.setdefault((cols[2], cols[3], cols[4]), []).append(cols[11].strip())
    out: list[str] = []
    previous = None
    for (block, par, _line), words in lines.items():
        if previous is not None and (block, par) != previous:
            out.append("")  # blank line between paragraphs, as in plain Tesseract output
        out.append(" ".join(words))
        previous = (block, par)
    mean = sum(confidences) / len(confidences) if confidences else 0.0
    # Stray zero-width joiners inside Devanagari words would make search and matching miss them.
    text = "\n".join(out).replace("\u200c", "").replace("\u200d", "")
    return text, mean


def _ocr_both(image: Path) -> tuple[str, str]:
    """(best text, English-only text).

    Tesseract mixes up scripts when given hin+eng together (Latin junk inside Hindi words,
    broken digits), so each language runs alone and the more confident one is the text.
    The English pass is always kept for reading phone numbers, which it gets right.
    """
    hindi, hindi_confidence = _ocr(image, HINDI_LANG)
    latin, latin_confidence = _ocr(image, LATIN_LANG)
    return (hindi if hindi_confidence >= latin_confidence else latin), latin


def _ocr_pdf_page(source: Path, page: int, workdir: Path) -> tuple[str, str]:
    prefix = workdir / f"page{page}"
    args = ["pdftoppm", "-r", str(OCR_DPI), "-f", str(page), "-l", str(page), "-singlefile", "-png"]
    _run([*args, str(source), str(prefix)])
    return _ocr_both(prefix.with_suffix(".png"))


@dataclass
class Extraction:
    text: str = ""
    status: str = Document.TextStatus.NONE
    pages: int | None = None
    # English-only OCR of the scanned pages; used to read phone numbers, never stored.
    latin_text: str = ""


def extract(content: bytes, mime_type: str) -> Extraction:
    """Read text from an upload.

    PDFs use pdftotext first. Pages without a text layer (scans, photos, screenshots) go
    through Tesseract one by one, up to settings.INTAKE_OCR_MAX_PAGES. If the needed tools
    are not installed the status stays "none" so the clerk can still type the details.
    """
    is_pdf = mime_type == "application/pdf"
    needed = ["pdftotext"] if is_pdf else ["tesseract"]
    if not all(shutil.which(tool) for tool in needed):
        return Extraction()

    try:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / ("upload.pdf" if is_pdf else "upload.img")
            source.write_bytes(content)
            if not is_pdf:
                text, latin = (t.strip() for t in _ocr_both(source))
                status = Document.TextStatus.DONE if text else Document.TextStatus.NONE
                return Extraction(text, status, 1, latin[:LATIN_TEXT_MAX_CHARS])

            pages = _run(["pdftotext", "-layout", str(source), "-"]).stdout.split("\f")
            if pages and not pages[-1]:
                pages.pop()  # pdftotext ends every page, including the last, with a form feed
            can_ocr = bool(shutil.which("pdftoppm") and shutil.which("tesseract"))
            budget = settings.INTAKE_OCR_MAX_PAGES
            latin_parts: list[str] = []
            for number, page_text in enumerate(pages, start=1):
                if len(page_text.strip()) >= MIN_TEXT_CHARS or not can_ocr or budget <= 0:
                    continue
                budget -= 1
                try:
                    pages[number - 1], latin = _ocr_pdf_page(source, number, Path(tmp))
                except (subprocess.SubprocessError, OSError):
                    continue  # keep what the other pages gave us
                latin_parts.append(latin.strip())
            text = "\n".join(p.strip() for p in pages if p.strip())
            status = Document.TextStatus.DONE if text else Document.TextStatus.NONE
            latin_text = "\n".join(latin_parts)[:LATIN_TEXT_MAX_CHARS]
            return Extraction(text, status, len(pages) or None, latin_text)
    except (subprocess.SubprocessError, OSError):
        return Extraction(status=Document.TextStatus.FAILED)


def extract_text(content: bytes, mime_type: str) -> tuple[str, str, int | None]:
    """(text, text_status, page_count) of an upload; see extract()."""
    result = extract(content, mime_type)
    return result.text, result.status, result.pages
