import re
from io import BytesIO

SUPPORTED_EXTENSIONS = {"pdf", "docx", "txt", "md"}


def extract_text(filename: str, data: bytes) -> str:
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext == "pdf":
        return _extract_pdf(data)
    if ext == "docx":
        return _extract_docx(data)
    if ext in ("txt", "md"):
        return data.decode("utf-8", errors="ignore")
    raise ValueError(
        f"Unsupported file type '.{ext}'. Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
    )


def _extract_pdf(data: bytes) -> str:
    import fitz  # PyMuPDF - notably better than pypdf at correctly ordering
    # glyphs for complex/reordering scripts (e.g. Devanagari matras)

    with fitz.open(stream=data, filetype="pdf") as doc:
        return "\n\n".join(page.get_text() for page in doc)


def _extract_docx(data: bytes) -> str:
    import docx

    document = docx.Document(BytesIO(data))
    return "\n".join(p.text for p in document.paragraphs)


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


def _merge_with_overlap(pieces: list[str], chunk_size: int, overlap: int) -> list[str]:
    """Greedily packs boundary-respecting pieces into chunks close to
    chunk_size, carrying whole trailing pieces (not a blind character
    slice, which could re-cut a piece mid-word) from the previous chunk
    into the next one so context isn't lost right at a chunk edge."""
    chunks = []
    current: list[str] = []
    current_len = 0

    for piece in pieces:
        if current and current_len + len(piece) > chunk_size:
            chunks.append("".join(current))

            overlap_pieces: list[str] = []
            overlap_len = 0
            for p in reversed(current):
                if overlap_len >= overlap:
                    break
                overlap_pieces.insert(0, p)
                overlap_len += len(p)

            current = overlap_pieces + [piece]
            current_len = overlap_len + len(piece)
        else:
            current.append(piece)
            current_len += len(piece)

    if current:
        chunks.append("".join(current))
    return chunks


def chunk_text(text: str, chunk_size: int = 1000, overlap: int = 150) -> list[str]:
    """Splits text into overlapping chunks ready to be fed to an LLM as
    context, preferring natural boundaries (paragraphs, then lines,
    then sentences, then clauses, then words) over blindly cutting
    mid-word. Falls back to a hard character split only for a single
    unbroken run of text longer than chunk_size."""
    if not text:
        return []

    pieces = _split_on_boundaries(text, chunk_size, _SEPARATORS)
    return _merge_with_overlap(pieces, chunk_size, overlap)
