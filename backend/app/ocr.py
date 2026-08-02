"""Reading text off page images with Gemini.

Only pages that carry no text layer are sent here - see
preprocessing.page_reports. Everything else keeps using the text already
in the file, which is exact and free.

Recognition goes through the same Vertex client as the tutor, so there is
no second provider to configure and no extra IAM: the service account
already has aiplatform.user. Results are cached in storage against the
file's content hash, because recognition is the expensive step and
reprocessing a subject (after a preprocessing change, or to rebuild the
database) would otherwise pay for every page again.
"""
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor

from . import tutor

ENABLED = os.environ.get("OCR_ENABLED", "1") not in ("0", "false", "False")

# 150dpi is enough for handwriting to be legible while keeping a page at
# roughly 1,800 tokens; 300dpi quadruples the pixels for little gain.
DPI = int(os.environ.get("OCR_DPI", "150"))

# A ceiling on how much one upload can spend. A 400-page scanned book is
# an accident more often than an intention, and failing loudly beats
# quietly running up a bill.
MAX_PAGES = int(os.environ.get("OCR_MAX_PAGES", "80"))

# Recognition is network-bound, so pages overlap comfortably. Kept modest
# because the whole material is already holding its file in memory and
# only one material processes at a time.
CONCURRENCY = int(os.environ.get("OCR_CONCURRENCY", "4"))

# Transcription, not interpretation. The tutor presents retrieved text as
# fact and cites it, so a plausible-looking guess at an unclear word
# becomes an authoritative statement the student cannot check. Marking the
# gap keeps the uncertainty visible instead of laundering it into prose.
PROMPT = """Transcribe the text on this page image exactly as it appears.

Rules:
- Output only the transcription. No preamble, no commentary, no markdown fences.
- Preserve the reading order and keep line and paragraph breaks.
- Transcribe the words that are actually written. Do not correct, complete,
  summarise, translate or improve them.
- Where handwriting genuinely cannot be read, write [illegible] in its place.
  Never guess at a word to fill the gap.
- For a diagram, chart or picture, give a short description in square
  brackets, e.g. [diagram: the water cycle].
- If the page has no readable content at all, output nothing."""


class OcrLimitExceeded(Exception):
    """More pages need recognising than the configured ceiling allows."""


def _render_page(doc, page_number: int) -> bytes:
    """One page as a PNG, at the resolution recognition sees."""
    return doc[page_number - 1].get_pixmap(dpi=DPI).tobytes("png")


def _recognise(png: bytes) -> str:
    import base64

    encoded = base64.b64encode(png).decode()
    response = tutor._get_client().chat.completions.create(
        model=tutor._model_name(),
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{encoded}"},
                    },
                ],
            }
        ],
        # Transcription has a right answer; sampling would only invent.
        temperature=0.0,
        max_tokens=4096,
    )
    return (response.choices[0].message.content or "").strip()


def _cache_path(subject_slug: str, raw_bytes: bytes) -> str:
    # Keyed on the file's content, not its name: re-uploading the same
    # scan under a different name reuses the work, and replacing a file
    # with a different one correctly misses.
    digest = hashlib.sha256(raw_bytes).hexdigest()[:32]
    return f"{subject_slug}/ocr/{digest}.json"


def recognise_pages(
    storage, subject_slug: str, raw_bytes: bytes, page_numbers: list[int]
) -> dict[int, str]:
    """Returns {page number: recognised text} for the given pages.

    Pages that come back empty are omitted, so a caller can tell what was
    actually recovered from what merely had nothing on it.
    """
    if not page_numbers:
        return {}
    if len(page_numbers) > MAX_PAGES:
        raise OcrLimitExceeded(
            f"{len(page_numbers)} pages need text recognition, above the limit of "
            f"{MAX_PAGES}. Split the file or raise OCR_MAX_PAGES."
        )

    path = _cache_path(subject_slug, raw_bytes)
    cached: dict[int, str] = {}
    if storage.exists(path):
        try:
            cached = {
                int(page): text
                for page, text in json.loads(storage.read(path).decode("utf-8")).items()
            }
        except Exception:  # noqa: BLE001 - a corrupt cache is not worth failing over
            cached = {}

    missing = [n for n in page_numbers if n not in cached]
    if missing:
        import fitz

        with fitz.open(stream=raw_bytes, filetype="pdf") as doc:
            images = {n: _render_page(doc, n) for n in missing}

        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            done = dict(
                zip(missing, pool.map(lambda n: _recognise(images[n]), missing))
            )

        cached.update(done)
        storage.save(
            path,
            json.dumps({str(k): v for k, v in cached.items()}, ensure_ascii=False).encode(
                "utf-8"
            ),
        )

    return {n: cached[n] for n in page_numbers if cached.get(n, "").strip()}
