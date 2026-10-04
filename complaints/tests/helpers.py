from complaints.services.documents import Attachment

_MAGIC = {
    "application/pdf": b"%PDF-1.4\n",
    "image/png": b"\x89PNG\r\n\x1a\n",
    "image/jpeg": b"\xff\xd8\xff",
}


def fake_attachment(label: str = "doc", mime: str = "application/pdf") -> Attachment:
    """Distinct bytes per label so the sha256 differs."""
    return Attachment(
        content=_MAGIC[mime] + label.encode(), filename=f"{label}.pdf", mime_type=mime
    )
