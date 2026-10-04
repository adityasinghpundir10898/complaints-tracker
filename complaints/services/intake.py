"""Intake helpers: staging an uploaded file, pre-filling the form, duplicate warnings."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.contrib.postgres.search import TrigramSimilarity

from complaints.models import Complaint
from complaints.services.documents import Attachment, sniff_mime
from core.models import Office, Village

SUBJECT_SIMILARITY = 0.5
MAX_DUPLICATES = 5
SUBJECT_MAX_LINES = 3
DESCRIPTION_MAX_CHARS = 4000
APPLICANT_BLOCK_MAX_CHARS = 400
NOISE_MIN_DIGITS = 6
NOISE_MAX_LETTERS = 8

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
# OCR scatters zero-width joiners inside Devanagari words ("भट्‌टू"), which breaks matching.
_CLEAN_TEXT = _DIGITS | {0x200C: None, 0x200D: None}

# Indian mobile numbers, possibly written as "93061 76815" or "+91-9306176815".
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?91[\s-]*)?([6-9]\d{4}[\s-]?\d{5})(?!\d)")
_NAME_LABEL_RE = re.compile(
    r"(?im)^[ \t]*(?:(?:शिकायतकर्ता|आवेदक|प्रार्थी|complainant|applicant)[^\n:\-–—]{0,15})?"
    r"(?:नाम|name)[^:\-–—\n]{0,20}[:\-–—]+[ \t]*(.+)$"
)
_SUBJECT_LABEL_RE = re.compile(r"(?im)^[ \t]*(?:subject|sub|विषय)[ \t]*[:\-–—]+[ \t]*")
_SALUTATION_RE = re.compile(
    r"^\s*(?:श्रीमान|महोदय|माननीय|सविनय|(?:Sir|Madam|Respected|Dear)\b)", re.I
)
_EXTERNAL_REF_RE = re.compile(
    r"(?i)(?:grievance|complaint|reference|ref)\s*(?:id|no\.?|number)?\s*[:\-]\s*"
    r"([A-Z0-9][A-Z0-9/\-]{5,})"
)
# Letters usually end with a signature block: "<name> पुत्र <father> ... निवासी <address>".
_APPLICANT_LABEL_RE = re.compile(
    r"प्रार्थी(?:गण|नी)?|प्रार्थगण|आवेदक(?:गण)?|शिकायतकर्ता|Applicants?|Complainants?"
)
_RELATION_RE = re.compile(r"\s(?:पुत्र|पुत्री|सुपुत्र|सुपुत्री|पत्नी|S/o|D/o|W/o)(?=[\s:,.]|$)", re.I)
_RESIDENT_RE = re.compile(r"\s(?:निवासी(?:गण)?|R/o)(?=[\s:,.]|$)", re.I)
_HONORIFIC_RE = re.compile(r"^(?:श्री(?:मती)?|Sh\.?|Smt\.?|Mr\.?|Mrs\.?)\s+", re.I)
_VILLAGE_LABEL_RE = re.compile(
    r"(?:गांव|गाँव|ग्राम|मौजा|village|vill\.?)[ \t]*[:\-]?\s+"
    r"([^\s,।.:;()]+)(?:[ \t]+([^\s,।.:;()]+))?",
    re.I,
)
_NOT_VILLAGE_WORDS = {"में", "के", "का", "की", "से", "को", "और", "है", "तहसील", "जिला", "बाबत"}
_DESCRIPTION_LABEL_RE = re.compile(
    r"(?im)^[ \t]*(?:complaint details|description|details|शिकायत का विवरण|विवरण)"
    r"[ \t]*[:\-–—]+[ \t]*"
)
_GREETING = (
    r"(?:श्रीमान[्]?\s*(?:जी)?|महोदय(?:\s*जी)?"
    r"|(?:respected|dear)\s+(?:sir|madam)|sir|madam)[,.:!\s]*"
)
_GREETING_RE = re.compile(_GREETING, re.I)
_GREETING_LINE_SEARCH_RE = re.compile(r"(?im)^[ \t]*" + _GREETING + r"$")
# Where the body of a letter stops: a closing phrase, or the fields that follow the text.
_CLOSING_RE = re.compile(
    r"(?im)^[ \t]*(?:आपकी अति कृपा|yours (?:faithfully|sincerely|truly)|thanking you|thank you"
    r"|धन्यवाद|(?:name|mobile|phone|village|date|नाम|मोबाइल|गांव)[ \t]*[:\-–—])"
)


def normalise_phone(value: str) -> str:
    """Digits only, without a +91 or leading 0, so the same number always compares equal."""
    digits = re.sub(r"\D", "", value.translate(_DIGITS))
    if len(digits) == 12 and digits.startswith("91"):
        return digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        return digits[1:]
    return digits


# --------------------------------------------------------------- staging


def _staging_dir() -> Path:
    path = Path(settings.MEDIA_ROOT) / "_staging"
    path.mkdir(parents=True, exist_ok=True)
    return path


def stage_upload(attachment: Attachment) -> str:
    """Keep an uploaded file on disk until the clerk saves; nothing is a Complaint yet."""
    sha = attachment.sha256
    target = _staging_dir() / f"{sha}{settings.ALLOWED_UPLOAD_TYPES[attachment.mime_type]}"
    if not target.exists():
        target.write_bytes(attachment.content)
    return sha


def load_staged(sha: str, meta: dict) -> Attachment | None:
    """Rebuild the Attachment for a staged hash; `meta` comes from the session."""
    if not _SHA_RE.match(sha or ""):
        return None
    for ext in settings.ALLOWED_UPLOAD_TYPES.values():
        path = _staging_dir() / f"{sha}{ext}"
        if path.exists():
            content = path.read_bytes()
            mime = sniff_mime(content)
            if mime is None:
                return None
            return Attachment(
                content=content,
                filename=meta.get("filename", path.name),
                mime_type=mime,
                extracted_text=meta.get("extracted_text", ""),
                text_status=meta.get("text_status", "none"),
                page_count=meta.get("page_count"),
            )
    return None


# --------------------------------------------------------------- pre-fill


@dataclass
class Prefill:
    name: str = ""
    phone: str = ""
    address: str = ""
    external_ref: str = ""
    subject: str = ""
    description: str = ""
    village_id: int | None = None
    village_text: str = ""  # village named in the letter but missing from the village list

    def as_initial(self) -> dict:
        return {
            "name": self.name,
            "phone": self.phone,
            "address": self.address,
            "external_ref": self.external_ref,
            "subject": self.subject,
            "description": self.description,
            "village": self.village_id,
        }


def _phones(text: str) -> list[str]:
    seen: list[str] = []
    for match in _PHONE_RE.finditer(text):
        number = normalise_phone(match.group(1))
        if number not in seen:
            seen.append(number)
    return seen


def _subject_with_end(text: str) -> tuple[str, int]:
    """The text after 'विषय :-' / 'Subject:', and where it ends in `text`.

    Letters often wrap the subject over two or three lines. Returns ("", 0) without a label.
    """
    label = _SUBJECT_LABEL_RE.search(text)
    if label is None:
        return "", 0
    parts: list[str] = []
    position = end = label.end()
    for raw in text[label.end() :].splitlines(keepends=True):
        position += len(raw)
        line = raw.strip()
        if not line:
            continue
        if parts and _SALUTATION_RE.match(line):
            break
        parts.append(line)
        end = position
        if line.endswith(("।", ".", "|")) or len(parts) >= SUBJECT_MAX_LINES:  # OCR reads । as |
            break
    return " ".join(parts).lstrip(" .:-–—")[:300], end


def _body_start(text: str, subject_end: int) -> int | None:
    """Where the letter body begins: after the subject and any 'Respected Sir,' line."""
    if subject_end == 0:
        greeting = _GREETING_LINE_SEARCH_RE.search(text)
        return greeting.end() if greeting else None
    offset = subject_end
    for raw in text[subject_end:].splitlines(keepends=True):
        stripped = raw.strip()
        if stripped and not _GREETING_RE.fullmatch(stripped):
            break
        offset += len(raw)
    return offset


def _description(text: str, subject_end: int) -> str:
    """A 'Description:' / 'विवरण:' field, else the letter body up to its closing lines."""
    labelled = _DESCRIPTION_LABEL_RE.search(text)
    start = labelled.end() if labelled else _body_start(text, subject_end)
    if start is None:
        return ""
    body = text[start:]
    if closing := _CLOSING_RE.search(body):
        body = body[: closing.start()]
    return re.sub(r"\s+", " ", body).strip()[:DESCRIPTION_MAX_CHARS]


def _applicant_block(text: str) -> str:
    """Text after the last 'प्रार्थी' / 'Applicant' label, if it looks like a signature block."""
    labels = list(_APPLICANT_LABEL_RE.finditer(text))
    if not labels:
        return ""
    last = labels[-1]
    block = text[last.end() :].strip()[:APPLICANT_BLOCK_MAX_CHARS]
    return block if _RELATION_RE.search(block) or _RESIDENT_RE.search(block) else ""


def _applicant_name(block: str) -> str:
    marker = _RELATION_RE.search(block) or _RESIDENT_RE.search(block)
    if marker is None:
        return ""
    before = block[: marker.start()].strip().splitlines()
    name = _HONORIFIC_RE.sub("", before[-1].strip()) if before else ""
    return name if 0 < len(name.split()) <= 5 else ""


def _village_from_labels(text: str, villages: list[Village]) -> tuple[int | None, str]:
    """Look at the word after 'गांव' / 'मौजा' / 'village'; returns (village id, unmatched word)."""
    by_name = {}
    for village in villages:
        by_name[village.name.casefold()] = village.pk
        if village.name_hi:
            by_name[village.name_hi.casefold()] = village.pk
    matches = [m for m in _VILLAGE_LABEL_RE.finditer(text) if m.group(1) not in _NOT_VILLAGE_WORDS]
    for m in matches:
        # Try the two-word name first: "Bhattu Kalan" before "Bhattu".
        candidates = [f"{m.group(1)} {m.group(2)}"] if m.group(2) else []
        for candidate in [*candidates, m.group(1)]:
            if candidate.casefold() in by_name:
                return by_name[candidate.casefold()], ""
    unmatched = [m.group(1) for m in matches]
    # OCR garbles some words, so trust the candidate the letter repeats most often.
    best = max(unmatched, key=text.count) if unmatched else ""
    return None, best


def _village_from_scan(text: str, villages: list[Village]) -> int | None:
    """Fallback: a village from the list is named anywhere in the text (longest name wins)."""
    lowered = text.lower()
    matches = [
        v for v in villages if v.name.lower() in lowered or (v.name_hi and v.name_hi in text)
    ]
    return max(matches, key=lambda v: len(v.name)).pk if matches else None


def _is_scanner_noise(line: str) -> bool:
    """A line that is mostly digits and bars: a handwritten date or a misread phone number."""
    digits_and_bars = sum(ch.isdigit() or ch == "|" for ch in line)
    return (
        digits_and_bars >= NOISE_MIN_DIGITS
        and sum(ch.isalpha() for ch in line) <= NOISE_MAX_LETTERS
    )


def guess_fields(text: str, office: Office, latin_text: str = "") -> Prefill:
    """Simple pattern guesses. They only suggest; the clerk confirms before saving.

    `latin_text` is the English-only OCR pass: Tesseract reads digits correctly there even
    when the Hindi pass garbles them, so phone numbers are taken from it first.
    """
    prefill = Prefill()
    if not text and not latin_text:
        return prefill
    text = text.translate(_CLEAN_TEXT)
    latin_text = latin_text.translate(_CLEAN_TEXT)

    block = _applicant_block(text)
    prefill.name = _applicant_name(block)
    if not prefill.name and (m := _NAME_LABEL_RE.search(text)):
        prefill.name = m.group(1).strip()[:200]

    phones = _phones(latin_text) or _phones(block) or _phones(text)
    if phones:
        prefill.phone = phones[0]
    if block:
        address = _PHONE_RE.sub("", block)
        address = "\n".join(ln for ln in address.splitlines() if not _is_scanner_noise(ln))
        # What is left of a phone line ("मो. 9306 7685,") is a misread number; drop it.
        address = re.sub(
            r"(?:मो\.?|मोबाइल|Mob(?:ile)?\.?|Ph(?:one)?\.?)[\s.:\-,]*[\d\s,\-+]*$", "", address
        )
        address = re.sub(r"\s+", " ", address).strip(" ,:-")
        if len(phones) > 1:
            address += f" (Other phone: {', '.join(phones[1:])})"
        prefill.address = address[:300]

    prefill.subject, subject_end = _subject_with_end(text)
    prefill.description = _description(text, subject_end)
    if m := _EXTERNAL_REF_RE.search(text):
        prefill.external_ref = m.group(1).strip()

    villages = list(Village.objects.filter(office=office, is_active=True))
    prefill.village_id, prefill.village_text = _village_from_labels(text, villages)
    if prefill.village_id is None:
        prefill.village_id = _village_from_scan(text, villages)
        if prefill.village_id is not None:
            prefill.village_text = ""
    return prefill


# ------------------------------------------------------------- duplicates


def find_possible_duplicates(
    office: Office, *, external_ref: str = "", phone: str = "", subject: str = "", village=None
) -> list[tuple[Complaint, str]]:
    """Same external ref, same phone, or similar subject in the same village."""
    found: dict[int, tuple[Complaint, list[str]]] = {}

    def add(complaint: Complaint, reason: str) -> None:
        found.setdefault(complaint.pk, (complaint, []))[1].append(reason)

    base = Complaint.objects.filter(office=office).select_related("complainant")
    if external_ref:
        for c in base.filter(external_ref__iexact=external_ref):
            add(c, "same external reference")
    if phone:
        for c in base.filter(complainant__phone=phone):
            add(c, "same phone number")
    if subject and village is not None:
        similar = (
            base.filter(village=village)
            .annotate(similarity=TrigramSimilarity("subject", subject))
            .filter(similarity__gte=SUBJECT_SIMILARITY)
        )
        for c in similar:
            add(c, "similar subject in the same village")

    results = [(c, ", ".join(reasons)) for c, reasons in found.values()]
    return results[:MAX_DUPLICATES]
