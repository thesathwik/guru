"""The actual tutor: answers a student's question about one subject by
retrieving relevant chunks (scoped to that subject only, via
embeddings.search_chunks) and asking an LLM to answer grounded in them.
"""
import os

from . import embeddings

_client = None

SYSTEM_PROMPT_TEMPLATE = """You are a patient, encouraging tutor helping a student study {subject_name}.

Answer the student's question using the reference material below, which comes from
their own {subject_name} materials. If the material doesn't contain enough
information to answer, say so honestly rather than guessing or using outside
knowledge. Keep answers clear and appropriately detailed for a student.

Reference material:
{context}
"""


class TutorNotConfigured(Exception):
    pass


def _get_client():
    global _client
    if _client is not None:
        return _client

    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    api_key = os.environ.get("AZURE_OPENAI_API_KEY")
    if not endpoint or not api_key:
        raise TutorNotConfigured(
            "Azure OpenAI is not configured - set AZURE_OPENAI_ENDPOINT, "
            "AZURE_OPENAI_API_KEY, and AZURE_OPENAI_DEPLOYMENT in .env"
        )

    # Azure AI Foundry's unified "v1" endpoint (https://<resource>.services
    # .ai.azure.com/openai/v1) is OpenAI-API-compatible, including for
    # non-OpenAI models in its catalog (e.g. Kimi K2) - so this uses the
    # plain OpenAI client with a custom base_url, not the AzureOpenAI
    # client (which targets the older *.openai.azure.com resource shape
    # and builds a different URL path).
    from openai import OpenAI

    _client = OpenAI(base_url=endpoint, api_key=api_key)
    return _client


def _build_context(matches: list[dict]) -> str:
    if not matches:
        return "(No material has been uploaded for this subject yet.)"
    return "\n\n---\n\n".join(f"[From {m['filename']}]\n{m['text']}" for m in matches)


# A relevant figure has to clear a low absolute floor *and* stand out
# from the subject's own baseline (see _relevant_images). The margin is
# what actually does the work: absolute cosine values vary a lot by
# embedding model, so a hard threshold has to be re-guessed whenever the
# model changes, while "clearly better than this subject's typical
# caption" holds regardless of the scale the model happens to produce.
MIN_IMAGE_SCORE = float(os.environ.get("TUTOR_MIN_IMAGE_SCORE", "0.20"))
IMAGE_STANDOUT_MARGIN = float(os.environ.get("TUTOR_IMAGE_MARGIN", "0.08"))
IMAGE_KEEP_RATIO = float(os.environ.get("TUTOR_IMAGE_KEEP_RATIO", "0.85"))
MAX_IMAGES = int(os.environ.get("TUTOR_MAX_IMAGES", "3"))


# Captions are short, and the embedding model in use is a *paraphrase*
# model - built for sentence-vs-sentence similarity, not question ->
# description retrieval. On short captions its cosine scores are close
# to noise (asking about the Tyndall effect ranked "Arm-wrestling" and
# "Meiosis" top). Distinctive words carry the signal instead: a caption
# containing a rare query term like "tyndall" is almost certainly the
# right figure, so lexical overlap is weighted above the vector score,
# with the vector score kept to catch wording the question doesn't share
# ("how plants make food" -> "Photosynthesis").
LEXICAL_WEIGHT = float(os.environ.get("TUTOR_IMAGE_LEXICAL_WEIGHT", "0.7"))


def score_images(
    db, subject_id: int, query_vector: list[float], query: str = ""
) -> list[tuple[float, object]]:
    """Scores every captioned figure in a subject against the question,
    best first. No filtering - `_relevant_images` decides what to keep,
    and the figures diagnostic endpoint shows the raw ranking."""
    import json

    from . import models

    rows = (
        db.query(models.MaterialImage)
        .filter(
            models.MaterialImage.subject_id == subject_id,
            models.MaterialImage.caption_embedding.isnot(None),
        )
        .all()
    )
    if not rows:
        return []

    idf = embeddings.build_idf([row.caption for row in rows])

    scored = []
    for row in rows:
        semantic = embeddings.cosine_similarity(query_vector, json.loads(row.caption_embedding))
        lexical = embeddings.lexical_overlap(query, row.caption, idf) if query else 0.0
        combined = LEXICAL_WEIGHT * lexical + (1 - LEXICAL_WEIGHT) * semantic
        scored.append((combined, row))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored


def _relevant_images(db, subject_id: int, query_vector: list[float], query: str = "") -> list[dict]:
    """Picks the figures whose captions genuinely match the question.

    An earlier version used page proximity - show every figure sharing a
    page with a matching passage - which surfaced whatever else happened
    to be on that page (e.g. a blood-centrifugation diagram for a
    question about the Tyndall effect). Textbook captions state what a
    figure depicts, so scoring against the caption picks the figure that
    actually answers the question.

    Selection is relative rather than a fixed cutoff: the best figure
    must beat the subject's median caption score by a margin. On a
    question a figure really illustrates, one caption stands out sharply
    from the rest; on a question with no matching figure, every caption
    scores about the same and nothing is shown.
    """
    import statistics

    scored = score_images(db, subject_id, query_vector, query)
    if not scored:
        return []

    best = scored[0][0]
    median = statistics.median([score for score, _ in scored])

    if best < MIN_IMAGE_SCORE or best - median < IMAGE_STANDOUT_MARGIN:
        return []

    cutoff = max(best * IMAGE_KEEP_RATIO, median + IMAGE_STANDOUT_MARGIN)

    return [
        {
            "id": row.id,
            "url": f"/api/images/{row.id}",
            "filename": row.material.filename,
            "page": row.page,
            "width": row.width,
            "height": row.height,
            "caption": row.caption,
            "score": score,
        }
        for score, row in scored[:MAX_IMAGES]
        if score >= cutoff
    ]


def answer_question(
    db, subject, question: str, history: list[dict] | None = None, top_k: int = 5
) -> dict:
    query_vector = embeddings.embed_query(question)
    matches = embeddings.search_chunks(
        db, subject.id, question, top_k=top_k, query_vector=query_vector
    )

    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT_TEMPLATE.format(
                subject_name=subject.name, context=_build_context(matches)
            ),
        }
    ]
    for turn in history or []:
        if turn.get("role") in ("user", "assistant") and turn.get("content"):
            messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": question})

    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT")
    if not deployment:
        raise TutorNotConfigured("AZURE_OPENAI_DEPLOYMENT is not set in .env")

    response = _get_client().chat.completions.create(
        model=deployment,
        messages=messages,
        temperature=0.3,
    )

    return {
        "answer": response.choices[0].message.content,
        "sources": [
            {
                "filename": m["filename"],
                "chunk_index": m["chunk_index"],
                "score": m["score"],
                "text": m["text"],
                "page": m.get("page"),
            }
            for m in matches
        ],
        "images": _relevant_images(db, subject.id, query_vector, question),
    }
