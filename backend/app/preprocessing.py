import re
from io import BytesIO

SUPPORTED_EXTENSIONS = {"pdf", "docx", "txt", "md"}


def extract_pages(filename: str, data: bytes) -> list[str]:
    """Extracts text as a list of pages. Non-paginated formats return a
    single 'page', so callers can treat every format uniformly."""
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext == "pdf":
        return _extract_pdf_pages(data)
    if ext == "docx":
        return [_extract_docx(data)]
    if ext in ("txt", "md"):
        return [data.decode("utf-8", errors="ignore")]
    raise ValueError(
        f"Unsupported file type '.{ext}'. Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
    )


def extract_text(filename: str, data: bytes) -> str:
    return "\n\n".join(extract_pages(filename, data))


def _extract_pdf_pages(data: bytes) -> list[str]:
    import fitz  # PyMuPDF - notably better than pypdf at correctly ordering
    # glyphs for complex/reordering scripts (e.g. Devanagari matras)

    with fitz.open(stream=data, filetype="pdf") as doc:
        return [page.get_text() for page in doc]


def _extract_docx(data: bytes) -> str:
    import docx

    document = docx.Document(BytesIO(data))
    return "\n".join(p.text for p in document.paragraphs)


# Formats browsers can render directly. Anything else (notably JPEG 2000,
# which PDFs use heavily but no browser supports) gets converted to PNG.
_WEB_SAFE_IMAGE_FORMATS = {"png", "jpg", "jpeg", "gif", "webp"}

# Textbook figures are nearly always labelled, and the label says what the
# figure actually shows - far more usefully than the surrounding body text.
_CAPTION_PATTERN = re.compile(
    r"^\s*(fig|figure|table|diagram|chart|graph|map|plate|activity"
    r"|चित्र|तालिका|आकृति|मानचित्र)\b[\s.:\-–]*\d",
    re.IGNORECASE,
)


def _looks_like_caption(text: str) -> bool:
    return bool(_CAPTION_PATTERN.match(text))


def _find_caption(
    page,
    rect,
    max_gap: float = 90.0,
    max_length: int = 250,
    overlap_tolerance: float = 25.0,
    line_gap: float = 14.0,
    continuation_max_length: int = 90,
) -> str | None:
    """Finds the text describing an image on the page.

    Prefers a real caption ("Fig. 5.24: Demonstration of the Tyndall
    effect...") sitting just below or above the image, since that states
    what the figure depicts. Falls back to the nearest body text, which
    is weaker but still far more specific than "somewhere on this page".
    """
    blocks = []
    for block in page.get_text("blocks"):
        if len(block) > 6 and block[6] != 0:
            continue  # not a text block
        x0, y0, x1, y1, raw = block[0], block[1], block[2], block[3], block[4]
        text = " ".join(raw.split())
        if not text:
            continue

        # Require horizontal overlap so we don't grab the neighbouring
        # column's text in a two-column layout.
        if min(x1, rect.x1) - max(x0, rect.x0) <= 0:
            continue
        blocks.append((y0, y1, text))

    # An image's rect often extends past the visible artwork, so the
    # caption's first line can start slightly *inside* it. Requiring the
    # text to sit strictly below the rect drops that line and picks up
    # only the continuation - which is how "Fig. 5.24: Demonstration of
    # Tyndall effect in / a sports stadium" was captured as just
    # "a sports stadium", losing the words that identify it.
    below = sorted(
        [b for b in blocks if b[0] >= rect.y1 - overlap_tolerance and b[0] - rect.y1 <= max_gap],
        key=lambda b: b[0],
    )
    above = sorted(
        [b for b in blocks if b[1] <= rect.y0 + overlap_tolerance and rect.y0 - b[1] <= max_gap],
        key=lambda b: -b[1],
    )

    if below:
        # Captions frequently wrap across several lines, and PyMuPDF
        # returns each as its own block, so join consecutive ones - but
        # only genuine continuation lines. Joining anything nearby sweeps
        # in the body paragraph that follows the caption, producing a
        # caption long and generic enough to match almost any question.
        start = next(
            (i for i, (_, _, text) in enumerate(below) if _looks_like_caption(text)), 0
        )
        parts = [below[start][2]]
        previous_bottom = below[start][1]
        for top, bottom, text in below[start + 1 :]:
            if top - previous_bottom > line_gap:
                break  # a new paragraph/section, not a wrapped line
            if len(text) > continuation_max_length:
                break  # a full paragraph, not the rest of a caption
            if sum(len(part) for part in parts) + len(text) > max_length:
                break
            parts.append(text)
            previous_bottom = bottom
        return " ".join(parts)[:max_length]

    if above:
        captionish = next(
            (text for _, _, text in above if _looks_like_caption(text)), above[0][2]
        )
        return captionish[:max_length]

    return None


def extract_images(
    data: bytes,
    min_dimension: int = 120,
    min_bytes_per_pixel: float = 0.01,
    max_aspect_ratio: float = 8.0,
    max_repeat_pages: int = 3,
) -> list[dict]:
    """Pulls figures/diagrams out of a PDF, one entry per (page, image).

    Textbook PDFs are full of non-content images - page-background
    textures, header/footer decorations, rule lines - so this filters
    aggressively:

    - `min_dimension` drops icons and thin rules.
    - `min_bytes_per_pixel` drops flat backgrounds: a 2480x3508 page
      texture compressing to ~8KB is essentially blank, while a real
      figure of that size would be orders of magnitude larger.
    - `max_aspect_ratio` drops sliver/divider graphics.
    - `max_repeat_pages` drops anything appearing on many pages (logos,
      running headers). Keyed on *content hash* rather than PDF xref,
      because the same background is often stored as a separate object
      per page and so has a different xref each time.
    """
    import hashlib

    import fitz

    occurrences: dict[str, list[int]] = {}
    payloads: dict[str, dict] = {}
    captions: dict[tuple[str, int], str] = {}

    with fitz.open(stream=data, filetype="pdf") as doc:
        for page_number, page in enumerate(doc, start=1):
            for image in page.get_images(full=True):
                xref = image[0]
                try:
                    base = doc.extract_image(xref)
                except Exception:  # noqa: BLE001 - skip anything unreadable
                    continue

                digest = hashlib.sha256(base["image"]).hexdigest()
                occurrences.setdefault(digest, []).append(page_number)
                if digest not in payloads:
                    payloads[digest] = {
                        "xref": xref,
                        "data": base["image"],
                        "ext": base["ext"].lower(),
                        "width": base["width"],
                        "height": base["height"],
                    }

                if (digest, page_number) not in captions:
                    try:
                        rects = page.get_image_rects(xref)
                    except Exception:  # noqa: BLE001
                        rects = []
                    for rect in rects:
                        caption = _find_caption(page, rect)
                        if caption:
                            captions[(digest, page_number)] = caption
                            break

        results = []
        for digest, pages in occurrences.items():
            payload = payloads[digest]
            width, height = payload["width"], payload["height"]

            if width < min_dimension or height < min_dimension:
                continue
            if len(pages) > max_repeat_pages:
                continue
            if max(width, height) / max(min(width, height), 1) > max_aspect_ratio:
                continue
            if len(payload["data"]) / max(width * height, 1) < min_bytes_per_pixel:
                continue

            image_data, ext = payload["data"], payload["ext"]
            if ext not in _WEB_SAFE_IMAGE_FORMATS:
                try:
                    pixmap = fitz.Pixmap(doc, payload["xref"])
                    if pixmap.n - pixmap.alpha >= 4:  # CMYK -> RGB
                        pixmap = fitz.Pixmap(fitz.csRGB, pixmap)
                    image_data, ext = pixmap.tobytes("png"), "png"
                except Exception:  # noqa: BLE001 - skip unconvertible images
                    continue

            for page_number in sorted(set(pages)):
                results.append(
                    {
                        "page": page_number,
                        "data": image_data,
                        "ext": ext,
                        "width": width,
                        "height": height,
                        "digest": digest,
                        "caption": captions.get((digest, page_number)),
                    }
                )

    results.sort(key=lambda item: (item["page"], -item["width"] * item["height"]))
    return results


def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# Tried in priority order: paragraph breaks first, then lines, then
# sentence boundaries (including the Devanagari '।' terminator, since
# a plain '.' doesn't end a Hindi/Sanskrit sentence), then clauses,
# then words. Only falls through to a hard character split (handled
# separately) if none of these appear within a chunk_size window.
_SEPARATORS = ["\n\n", "\n", "। ", ". ", "; ", ", ", " "]


def _split_keeping_separator(text: str, separator: str) -> list[str]:
    parts = text.split(separator)
    return [p + separator for p in parts[:-1]] + [parts[-1]]


def _split_on_boundaries(text: str, chunk_size: int, separators: list[str]) -> list[str]:
    """Recursively breaks text into pieces no larger than chunk_size,
    preferring the earliest separator in the list that actually splits
    it. Concatenating the result reproduces the original text exactly."""
    if len(text) <= chunk_size:
        return [text] if text else []

    if not separators:
        return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]

    separator, *rest = separators
    pieces = _split_keeping_separator(text, separator)
    if len(pieces) == 1:
        return _split_on_boundaries(text, chunk_size, rest)

    result = []
    for piece in pieces:
        if len(piece) > chunk_size:
            result.extend(_split_on_boundaries(piece, chunk_size, rest))
        elif piece:
            result.append(piece)
    return result


def _merge_with_overlap(
    pieces: list[str], chunk_size: int, overlap: int
) -> list[tuple[str, int]]:
    """Greedily packs boundary-respecting pieces into chunks close to
    chunk_size, carrying whole trailing pieces (not a blind character
    slice, which could re-cut a piece mid-word) from the previous chunk
    into the next one so context isn't lost right at a chunk edge.

    Returns (chunk_text, start_offset) pairs; the offset is into the
    original text, which is what lets a chunk be traced back to the page
    it came from."""
    offsets: list[int] = []
    running = 0
    for piece in pieces:
        offsets.append(running)
        running += len(piece)

    chunks: list[tuple[str, int]] = []
    current: list[int] = []
    current_len = 0

    def flush() -> None:
        chunks.append(("".join(pieces[i] for i in current), offsets[current[0]]))

    for index, piece in enumerate(pieces):
        if current and current_len + len(piece) > chunk_size:
            flush()

            overlap_indices: list[int] = []
            overlap_len = 0
            for i in reversed(current):
                if overlap_len >= overlap:
                    break
                overlap_indices.insert(0, i)
                overlap_len += len(pieces[i])

            current = overlap_indices + [index]
            current_len = overlap_len + len(piece)
        else:
            current.append(index)
            current_len += len(piece)

    if current:
        flush()
    return chunks


def chunk_text_with_offsets(
    text: str, chunk_size: int = 1000, overlap: int = 150
) -> list[tuple[str, int]]:
    """Splits text into overlapping chunks ready to be fed to an LLM as
    context, preferring natural boundaries (paragraphs, then lines,
    then sentences, then clauses, then words) over blindly cutting
    mid-word. Falls back to a hard character split only for a single
    unbroken run of text longer than chunk_size. Each chunk is paired
    with its start offset in `text`."""
    if not text:
        return []

    pieces = _split_on_boundaries(text, chunk_size, _SEPARATORS)
    return _merge_with_overlap(pieces, chunk_size, overlap)


def chunk_text(text: str, chunk_size: int = 1000, overlap: int = 150) -> list[str]:
    return [chunk for chunk, _ in chunk_text_with_offsets(text, chunk_size, overlap)]


def _page_for_offset(spans: list[tuple[int, int, int]], offset: int) -> int:
    """Maps a character offset back to the page it falls in."""
    if not spans:
        return 1

    for start, end, page_number in spans:
        if start <= offset < end:
            return page_number

    # The offset landed in the separator between two pages, so the chunk
    # opens with that whitespace and its real content is the *next*
    # page's. Attributing it to the page that just ended would pull in
    # the wrong page's images.
    for start, _end, page_number in spans:
        if start > offset:
            return page_number

    return spans[-1][2]


def chunk_pages(
    pages: list[str], chunk_size: int = 1000, overlap: int = 150
) -> tuple[str, list[dict]]:
    """Cleans and joins per-page text, then chunks it while tracking
    which page each chunk starts on. Returns (full_text, chunks) where
    each chunk is {"text": ..., "page": ...}.

    Chunking runs over the joined text rather than page by page, so a
    passage continuing across a page break still chunks naturally."""
    full_text = ""
    spans: list[tuple[int, int, int]] = []

    for page_number, raw in enumerate(pages, start=1):
        cleaned = clean_text(raw)
        if not cleaned:
            continue
        start = len(full_text)
        full_text += cleaned
        spans.append((start, len(full_text), page_number))
        full_text += "\n\n"

    full_text = full_text.strip()

    chunks = [
        {"text": chunk, "page": _page_for_offset(spans, offset)}
        for chunk, offset in chunk_text_with_offsets(full_text, chunk_size, overlap)
    ]
    return full_text, chunks
