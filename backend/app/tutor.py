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


# Only pages behind a *confidently* matching chunk contribute figures, so
# a vague question doesn't pull in unrelated diagrams. Tunable without a
# code change if it turns out too strict/loose on real material.
MIN_IMAGE_SCORE = float(os.environ.get("TUTOR_MIN_IMAGE_SCORE", "0.35"))
MAX_IMAGES = int(os.environ.get("TUTOR_MAX_IMAGES", "3"))


def _relevant_images(db, matches: list[dict]) -> list[dict]:
    """Finds figures sitting on the same pages as the best-matching text.

    This is page proximity, not true image-level semantic matching: an
    image is considered relevant because the passage that answered the
    question came off the same page."""
    from . import models

    strong = [
        m for m in matches if m.get("page") is not None and m["score"] >= MIN_IMAGE_SCORE
    ]
    if not strong:
        return []

    images: list[dict] = []
    seen: set[int] = set()

    for match in strong:  # already ordered best-first
        rows = (
            db.query(models.MaterialImage)
            .filter_by(material_id=match["material_id"], page=match["page"])
            .all()
        )
        for row in rows:
            if row.id in seen:
                continue
            seen.add(row.id)
            images.append(
                {
                    "id": row.id,
                    "url": f"/api/images/{row.id}",
                    "filename": match["filename"],
                    "page": row.page,
                    "width": row.width,
                    "height": row.height,
                }
            )
            if len(images) >= MAX_IMAGES:
                return images

    return images


def answer_question(
    db, subject, question: str, history: list[dict] | None = None, top_k: int = 5
) -> dict:
    matches = embeddings.search_chunks(db, subject.id, question, top_k=top_k)

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
        "images": _relevant_images(db, matches),
    }
