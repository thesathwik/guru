"""The actual tutor: answers a student's question about one subject by
retrieving relevant chunks (scoped to that subject only, via
embeddings.search_chunks) and asking an LLM to answer grounded in them.
"""
import os
from datetime import datetime, timedelta

from . import embeddings

_client = None
_credentials = None

# Refresh this far ahead of the stated expiry, so a request that takes a
# while to generate does not start with a token about to die.
_TOKEN_REFRESH_MARGIN = timedelta(minutes=5)

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


def _vertex_endpoint(project: str) -> str:
    """Vertex AI exposes an OpenAI-compatible endpoint, so Gemini can be
    driven through the same client as any other provider."""
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    return (
        f"https://{location}-aiplatform.googleapis.com/v1/"
        f"projects/{project}/locations/{location}/endpoints/openapi"
    )


def _vertex_token() -> str:
    """A currently-valid access token for the service account Cloud Run
    already runs as - no API key to manage.

    The credentials object is cached and refreshed on demand rather than
    the *token* being captured once. Vertex access tokens last about an
    hour, so an instance that stays warm longer than that would otherwise
    keep presenting an expired token and fail every call with a 401 until
    it happened to be recycled.
    """
    global _credentials

    if _credentials is None:
        import google.auth

        _credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )

    expiry = getattr(_credentials, "expiry", None)
    expiring = False
    if expiry is not None:
        # google.auth stores this as a naive UTC datetime. Match whichever
        # it hands back rather than assuming, so a library upgrade that
        # starts returning an aware datetime does not raise TypeError here.
        now = datetime.now(expiry.tzinfo) if expiry.tzinfo else datetime.utcnow()
        expiring = expiry - now <= _TOKEN_REFRESH_MARGIN

    if not _credentials.valid or expiring:
        import google.auth.transport.requests

        _credentials.refresh(google.auth.transport.requests.Request())

    return _credentials.token


def _get_client():
    global _client

    from openai import OpenAI

    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if project:
        # Re-checked on every call, not just the first: the client is
        # long-lived but the token behind it is not. The SDK builds the
        # Authorization header per request from this attribute, so
        # assigning it is enough - there is no need to rebuild the client
        # (which would throw away its connection pool each hour).
        token = _vertex_token()
        if _client is None:
            _client = OpenAI(base_url=_vertex_endpoint(project), api_key=token)
        else:
            _client.api_key = token
        return _client

    if _client is not None:
        return _client

    # Azure AI Foundry's unified "v1" endpoint (https://<resource>.services
    # .ai.azure.com/openai/v1) is OpenAI-API-compatible, including for
    # non-OpenAI models in its catalog (e.g. Kimi K2) - so this uses the
    # plain OpenAI client with a custom base_url, not the AzureOpenAI
    # client (which targets the older *.openai.azure.com resource shape
    # and builds a different URL path).
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    api_key = os.environ.get("AZURE_OPENAI_API_KEY")
    if endpoint and api_key:
        _client = OpenAI(base_url=endpoint, api_key=api_key)
        return _client

    raise TutorNotConfigured(
        "No LLM configured - set GOOGLE_CLOUD_PROJECT (Vertex AI) or "
        "AZURE_OPENAI_ENDPOINT/AZURE_OPENAI_API_KEY (Azure AI Foundry)"
    )


def _model_name() -> str:
    """Vertex names models directly ('google/gemini-2.5-flash'); Azure
    addresses a deployment you named yourself."""
    if os.environ.get("GOOGLE_CLOUD_PROJECT"):
        return os.environ.get("VERTEX_MODEL", "google/gemini-2.5-flash")

    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT")
    if not deployment:
        raise TutorNotConfigured("AZURE_OPENAI_DEPLOYMENT is not set")
    return deployment


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
# Cross-encoder relevance probability a figure must reach to be shown.
# Unlike a cosine cutoff this is meaningful on its own: the model is
# trained to output "is this passage relevant to this query".
RERANK_MIN_SCORE = float(os.environ.get("TUTOR_RERANK_MIN_SCORE", "0.30"))
IMAGE_STANDOUT_MARGIN = float(os.environ.get("TUTOR_IMAGE_MARGIN", "0.08"))
IMAGE_KEEP_RATIO = float(os.environ.get("TUTOR_IMAGE_KEEP_RATIO", "0.85"))
MAX_IMAGES = int(os.environ.get("TUTOR_MAX_IMAGES", "3"))


def score_images(
    db, subject_id: int, query_vector: list[float], query: str = ""
) -> tuple[list[tuple[float, object]], bool]:
    """Ranks a subject's captioned figures against the question, best
    first, using the same two-stage pipeline as text retrieval: dense +
    BM25 fused by RRF for candidates, then a cross-encoder over the
    shortlist.

    Question-against-caption is precisely what a cross-encoder is good
    at and what a bi-encoder is not - both sides are short, so a
    single-vector comparison has almost nothing to work with, which is
    why "Effect of solutions of different concentrations on a cell" kept
    scoring highly for a question about the Tyndall effect.

    Returns (ranked, reranked) - `reranked` tells the caller which score
    scale it is looking at, since a cross-encoder probability and an RRF
    score need different cutoffs.
    """
    import json

    from . import models, reranker

    rows = (
        db.query(models.MaterialImage)
        .filter(
            models.MaterialImage.subject_id == subject_id,
            models.MaterialImage.caption_embedding.isnot(None),
        )
        .all()
    )
    if not rows:
        return [], False

    by_id = {row.id: row for row in rows}

    dense = {
        row.id: embeddings.cosine_similarity(query_vector, json.loads(row.caption_embedding))
        for row in rows
    }
    rankings = [sorted(dense, key=dense.get, reverse=True)]
    weights = [embeddings.DENSE_WEIGHT]

    if query:
        # Term weights come from the subject's chunk text - captions are
        # too small a corpus to tell a rare word from a common one.
        idf = embeddings.get_subject_idf(db, subject_id)
        caption_tokens = {row.id: embeddings.tokenize(row.caption) for row in rows}
        average_length = sum(len(t) for t in caption_tokens.values()) / max(len(rows), 1)
        query_terms = embeddings.tokenize(query)

        sparse = {}
        for row in rows:
            tokens = caption_tokens[row.id]
            counts: dict[str, int] = {}
            for term in tokens:
                counts[term] = counts.get(term, 0) + 1
            sparse[row.id] = embeddings.bm25_score(
                query_terms, counts, len(tokens), idf, average_length
            )

        best = max(sparse.values(), default=0.0)
        floor = best * embeddings.SPARSE_MIN_RATIO
        matched = [i for i, score in sparse.items() if score > 0 and score >= floor]
        matched.sort(key=sparse.get, reverse=True)
        if matched:
            rankings.append(matched)
            weights.append(embeddings.SPARSE_WEIGHT)

    fused = embeddings.reciprocal_rank_fusion(rankings, weights=weights)
    order = sorted(fused, key=fused.get, reverse=True)

    shortlist = order[: reranker.CANDIDATES]
    scores = reranker.rerank(query, [by_id[i].caption for i in shortlist]) if query else None

    if scores is not None:
        ranked = sorted(zip(scores, shortlist), key=lambda pair: pair[0], reverse=True)
        return [(embeddings._sigmoid(float(s)), by_id[i]) for s, i in ranked], True

    return [(fused[i], by_id[i]) for i in shortlist], False


def _relevant_images(db, subject_id: int, query_vector: list[float], query: str = "") -> list[dict]:
    """Picks the figures whose captions genuinely match the question.

    An earlier version used page proximity - show every figure sharing a
    page with a matching passage - which surfaced whatever else happened
    to be on that page (e.g. a blood-centrifugation diagram for a
    question about the Tyndall effect). Textbook captions state what a
    figure depicts, so scoring against the caption picks the figure that
    actually answers the question.

    When the cross-encoder ran, its output is a calibrated relevance
    probability, so a plain threshold works and is far more predictable
    than any heuristic over bi-encoder cosines - an irrelevant caption
    scores near zero rather than merely lower than its neighbours.

    Without it, scores are RRF positions with no inherent meaning, so
    selection falls back to a relative rule: the best figure must beat
    the subject's median by a margin. On a question a figure really
    illustrates one caption stands out sharply; when nothing matches,
    every caption scores about the same and nothing is shown.
    """
    import statistics

    scored, reranked = score_images(db, subject_id, query_vector, query)
    if not scored:
        return []

    if reranked:
        cutoff = RERANK_MIN_SCORE
        if scored[0][0] < cutoff:
            return []
    else:
        # RRF scores are positions, not similarities - an absolute floor
        # meant for cosines rejects everything here - so normalise to the
        # best hit and judge by separation instead.
        best = scored[0][0]
        if best <= 0:
            return []
        scored = [(score / best, row) for score, row in scored]

        # Without the cross-encoder, captions sharing a common word
        # ("Effect of solutions on a cell" against "Tyndall effect")
        # land near-tied, and nothing left can tell them apart. Showing
        # the wrong figure is worse than showing none, so this only
        # fires when one figure is clearly ahead, and then shows just it.
        runner_up = scored[1][0] if len(scored) > 1 else 0.0
        if runner_up > IMAGE_KEEP_RATIO:
            return []
        median = statistics.median([score for score, _ in scored])
        if 1.0 - median < IMAGE_STANDOUT_MARGIN:
            return []
        cutoff = IMAGE_KEEP_RATIO

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

    response = _get_client().chat.completions.create(
        model=_model_name(),
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
