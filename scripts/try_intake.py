"""Show what intake would read from files: extracted text, English pass, and the form guesses.

Run inside the web container (it has pdftotext and Tesseract):

    docker compose --profile web run --rm web python scripts/try_intake.py samples/synthetic

Pass files or folders; the report is printed as UTF-8.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import django

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

from complaints.services import intake  # noqa: E402
from complaints.services.documents import extract, sniff_mime  # noqa: E402
from core.models import Office  # noqa: E402


def files(paths: list[str]):
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            yield from sorted(
                p for p in path.iterdir() if p.suffix.lower() in {".pdf", ".jpg", ".jpeg", ".png"}
            )
        else:
            yield path


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    office = Office.objects.first()
    for path in files(sys.argv[1:] or ["samples"]):
        data = path.read_bytes()
        mime = sniff_mime(data)
        started = time.time()
        result = extract(data, mime)
        guess = intake.guess_fields(result.text, office, result.latin_text)
        print("=" * 78)
        print(
            f"{path.name}: status={result.status} pages={result.pages} {time.time() - started:.1f}s"
        )
        print("-- text --")
        print(result.text)
        print("-- guess --")
        for name, value in vars(guess).items():
            print(f"{name:13} {value!r}")


if __name__ == "__main__":
    main()
