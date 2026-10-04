"""Make fake complaint files for trying out intake. Windows only (it draws Hindi with GDI+).

    python scripts/make_sample_complaints.py

Files go to samples/synthetic/ (git-ignored). Everything in them is invented. The layouts are
my guesses, not the office's real formats, except that the Hindi petition imitates the
structure of a real one: a subject line, a signature block with "पुत्र ... निवासीगण ...".
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = HERE / "sample_complaints.json"
OUT = HERE.parent / "samples" / "synthetic"
PAGE_W, PAGE_H = 595, 842
LINE_HEIGHT = 15


def jpeg_size(data: bytes) -> tuple[int, int]:
    i = 2
    while i < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            return int.from_bytes(data[i + 7 : i + 9], "big"), int.from_bytes(
                data[i + 5 : i + 7], "big"
            )
        i += 2 + int.from_bytes(data[i + 2 : i + 4], "big")
    raise ValueError("not a JPEG")


def pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def build_pdf(pages: list[tuple]) -> bytes:
    bodies: list[bytes] = [b"", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]

    def add(body: bytes) -> int:
        bodies.append(body)
        return len(bodies)

    def stream(prefix: bytes, data: bytes) -> bytes:
        return prefix + b"\nstream\n" + data + b"\nendstream"

    page_ids = []
    for page in pages:
        if page[0] == "text":
            ops = [f"BT /F1 11 Tf 56 790 Td {LINE_HEIGHT} TL"]
            ops += [f"({pdf_escape(line)}) Tj T*" for line in page[1]]
            ops.append("ET")
            data = "\n".join(ops).encode("latin-1", "replace")
            content = add(stream(b"<< /Length %d >>" % len(data), data))
            resources = b"<< /Font << /F1 3 0 R >> >>"
        else:
            _, jpg, width, height = page
            image = add(
                stream(
                    b"<< /Type /XObject /Subtype /Image /Width %d /Height %d "
                    b"/ColorSpace /DeviceRGB "
                    b"/BitsPerComponent 8 /Filter /DCTDecode /Length %d >>"
                    % (width, height, len(jpg)),
                    jpg,
                )
            )
            data = b"q %d 0 0 %d 0 0 cm /Im0 Do Q" % (PAGE_W, PAGE_H)
            content = add(stream(b"<< /Length %d >>" % len(data), data))
            resources = b"<< /XObject << /Im0 %d 0 R >> >>" % image
        page_ids.append(
            add(
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Contents %d 0 R "
                b"/Resources %s >>" % (PAGE_W, PAGE_H, content, resources)
            )
        )
    kids = b" ".join(b"%d 0 R" % p for p in page_ids)
    bodies[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    bodies[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(page_ids))

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(bodies, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(bodies) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(bodies) + 1,
        xref,
    )
    return bytes(out)


def main() -> None:
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(HERE / "render_pages.ps1"),
                "-Spec",
                str(SPEC),
                "-OutDir",
                tmp,
            ],
            check=True,
        )
        for doc in spec["documents"]:
            pages = []
            for number, page in enumerate(doc["pages"], 1):
                if page["mode"] == "text":
                    pages.append(("text", page["lines"]))
                else:
                    jpg = (Path(tmp) / f"{doc['name']}-{number}.jpg").read_bytes()
                    pages.append(("image", jpg, *jpeg_size(jpg)))
            if doc["kind"] == "jpg":
                target = OUT / f"{doc['name']}.jpg"
                target.write_bytes(pages[0][1])
            else:
                target = OUT / f"{doc['name']}.pdf"
                target.write_bytes(build_pdf(pages))
            print(f"wrote {target.relative_to(HERE.parent)} ({target.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    sys.exit(main())
