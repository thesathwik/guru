"""Text embedding + subject-scoped similarity search.

Uses a single local multilingual model (no external API, no per-call
cost) for every subject. Isolation between subjects' tutors comes from
tagging every embedded chunk with subject_id and always filtering
retrieval to it - not from separate models or indexes.
"""
import json
import math
import os
import re
from functools import lru_cache

import numpy as np

MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# RRF settings. Lower k than the textbook 60 so a top-ranked hit carries
# more weight, and BM25 weighted above the dense ranking because this
# embedding model is a paraphrase model and unreliable on exact terms.
RRF_K = int(os.environ.get("RETRIEVAL_RRF_K", "20"))
DENSE_WEIGHT = float(os.environ.get("RETRIEVAL_DENSE_WEIGHT", "1.0"))
SPARSE_WEIGHT = float(os.environ.get("RETRIEVAL_SPARSE_WEIGHT", "2.0"))
# Minimum share of the best BM25 score a chunk needs before it counts as
# a lexical match at all.
SPARSE_MIN_RATIO = float(os.environ.get("RETRIEVAL_SPARSE_MIN_RATIO", "0.1"))



@lru_cache(maxsize=1)
def _get_model():
    from fastembed import TextEmbedding

    return TextEmbedding(model_name=MODEL_NAME)


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embeds a batch of chunk texts for storage."""
    if not texts:
        return []
    return [vec.tolist() for vec in _get_model().embed(texts)]


def embed_query(text: str) -> list[float]:
    """Embeds a single search query. Same model/space as embed_texts -
    this particular model is symmetric (no query/passage prefix needed,
    unlike e.g. the E5 model family)."""
    return next(iter(_get_model().embed([text]))).tolist()


# `\w` covers letters and digits but not Unicode combining marks, and in
# Devanagari the vowel signs (matras) are exactly that - so a plain \w+
# splits "भारत" into "भ" and "रत" at the ा. Query and document tokenise
# identically, so matching still appears to work while every word is
# silently shredded into fragments, wrecking IDF and BM25 term
# statistics. The Indic block and combining ranges are added explicitly,
# along with ZWJ/ZWNJ, which occur inside conjuncts.
_TOKEN_PATTERN = re.compile(
    "[\\w"
    "\u0300-\u036f"   # combining diacritical marks
    "\u0900-\u0d7f"   # Devanagari through Malayalam (includes all matras)
    "\ua8e0-\ua8ff"   # Devanagari Extended
    "\u200c\u200d"    # ZWNJ / ZWJ, which occur inside conjuncts
    "]+",
    re.UNICODE,
)


def tokenize(text: str) -> list[str]:
    return _TOKEN_PATTERN.findall(text.lower())


def build_bm25_idf(documents: list[str]) -> dict[str, float]:
    """BM25's smoothed IDF. It falls off far more sharply than the plain
    form for terms appearing in most documents, so boilerplate present
    on every page contributes almost nothing, while the +1 keeps it
    positive (unlike the unsmoothed variant, which goes negative)."""
    counts: dict[str, int] = {}
    for document in documents:
        for term in set(tokenize(document)):
            counts[term] = counts.get(term, 0) + 1

    total = max(len(documents), 1)
    return {
        term: math.log(1 + (total - count + 0.5) / (count + 0.5))
        for term, count in counts.items()
    }


def bm25_score(
    query_terms: list[str],
    doc_counts: dict[str, int],
    doc_length: int,
    idf: dict[str, float],
    average_length: float,
    k1: float = 1.5,
    b: float = 0.75,
) -> float:
    """Standard Okapi BM25. Term saturation (k1) stops a word repeated
    many times from dominating, and length normalisation (b) stops long
    chunks scoring highly just for containing more words - neither of
    which the previous hand-rolled overlap ratio accounted for."""
    score = 0.0
    for term in query_terms:
        frequency = doc_counts.get(term)
        if not frequency:
            continue
        weight = idf.get(term, 0.0)
        if weight <= 0:
            continue
        denominator = frequency + k1 * (1 - b + b * doc_length / max(average_length, 1e-9))
        score += weight * (frequency * (k1 + 1)) / denominator
    return score


def reciprocal_rank_fusion(
    rankings: list[list[int]],
    k: int = RRF_K,
    weights: list[float] | None = None,
) -> dict[int, float]:
    """Reciprocal Rank Fusion: combine rankings by position rather than
    by score. Dense cosine and BM25 produce scores on incompatible
    scales, so blending the raw numbers means choosing a weight that has
    to be re-guessed per model; RRF only needs the orderings.

    Textbook RRF uses k=60 and equal weights, which assumes the rankers
    are comparably good. Here they are not - the dense model is a
    paraphrase model and near noise on exact-term questions - so BM25 is
    weighted higher and k is lower, letting a strong lexical hit pull a
    passage up rather than being averaged away by a weak dense rank.
    """
    fused: dict[int, float] = {}
    for index, ranking in enumerate(rankings):
        weight = weights[index] if weights and index < len(weights) else 1.0
        for position, identifier in enumerate(ranking):
            fused[identifier] = fused.get(identifier, 0.0) + weight / (k + position + 1)
    return fused


# (chunk_count, idf, average_length, {chunk_id: (term_counts, length)})
_subject_stats_cache: dict[int, tuple[int, dict, float, dict]] = {}


def get_subject_stats(db, subject_id: int):
    """BM25 statistics for a subject's chunks, cached and rebuilt when
    the chunk count changes (material added or reprocessed). Tokenising
    every chunk per query would otherwise dominate query time."""
    from . import models

    chunk_count = db.query(models.Chunk).filter_by(subject_id=subject_id).count()
    cached = _subject_stats_cache.get(subject_id)
    if cached and cached[0] == chunk_count:
        return cached[1], cached[2], cached[3]

    rows = db.query(models.Chunk).filter_by(subject_id=subject_id).all()
    idf = build_bm25_idf([row.text for row in rows])

    documents = {}
    total_length = 0
    for row in rows:
        terms = tokenize(row.text)
        counts: dict[str, int] = {}
        for term in terms:
            counts[term] = counts.get(term, 0) + 1
        documents[row.id] = (counts, len(terms))
        total_length += len(terms)

    average_length = total_length / max(len(rows), 1)
    _subject_stats_cache[subject_id] = (chunk_count, idf, average_length, documents)
    return idf, average_length, documents


def get_subject_idf(db, subject_id: int) -> dict[str, float]:
    """Term weights over a subject's chunk text. Figure captions borrow
    these rather than deriving their own: the caption corpus is far too
    small to tell a rare word from a common one."""
    idf, _average_length, _documents = get_subject_stats(db, subject_id)
    return idf


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    exponent = math.exp(x)
    return exponent / (1.0 + exponent)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    a_arr = np.array(a)
    b_arr = np.array(b)
    denom = np.linalg.norm(a_arr) * np.linalg.norm(b_arr)
    if denom == 0:
        return 0.0
    return float(np.dot(a_arr, b_arr) / denom)


def search_chunks(
    db,
    subject_id: int,
    query: str,
    top_k: int = 5,
    query_vector: list[float] | None = None,
) -> list[dict]:
    """Two-stage retrieval over a single subject's chunks.

    Stage 1 runs dense vector search and BM25 independently and fuses
    their *rankings* with Reciprocal Rank Fusion. The two are
    complementary: dense matching handles paraphrase ("how plants make
    food" -> photosynthesis) but is weak on exact terms, while BM25
    nails rare words like "Tyndall" that dense retrieval washes out.
    Fusing ranks rather than scores avoids having to weight two
    incompatible scales.

    Stage 2 reranks the fused shortlist with a cross-encoder, which
    reads query and passage together instead of comparing vectors that
    were each encoded in ignorance of the other. This is what the
    embedding model - a paraphrase model, built for sentence similarity
    rather than question-to-passage retrieval - cannot do, and is why
    it returned pollination and potential-energy passages for unrelated
    questions.

    `query_vector` lets a caller that already embedded the query reuse
    it instead of paying for a second embedding pass.
    """
    from . import models
    from . import reranker

    rows = db.query(models.Chunk).filter_by(subject_id=subject_id).all()
    if not rows:
        return []

    query_vec = np.array(query_vector if query_vector is not None else embed_query(query))
    query_norm = np.linalg.norm(query_vec)

    dense_scores = {}
    for row in rows:
        chunk_vec = np.array(json.loads(row.embedding))
        denom = query_norm * np.linalg.norm(chunk_vec)
        dense_scores[row.id] = float(np.dot(query_vec, chunk_vec) / denom) if denom else 0.0

    sparse_scores = {}
    if query:
        idf, average_length, documents = get_subject_stats(db, subject_id)
        query_terms = tokenize(query)
        for row in rows:
            counts, length = documents.get(row.id, ({}, 0))
            sparse_scores[row.id] = bm25_score(
                query_terms, counts, length, idf, average_length
            )

    by_id = {row.id: row for row in rows}
    rankings = [sorted(dense_scores, key=dense_scores.get, reverse=True)]
    weights = [DENSE_WEIGHT]
    if sparse_scores:
        # Only let BM25 vote for chunks it meaningfully matched. Scoring
        # above zero is not enough: a passage sharing nothing but "is"
        # still scores slightly, and that faint vote is enough to lift it
        # over the real answer once fused with a dense rank. Requiring a
        # fraction of the best BM25 score keeps stopword-only matches out
        # of the fusion entirely.
        best_sparse = max(sparse_scores.values(), default=0.0)
        floor = best_sparse * SPARSE_MIN_RATIO
        matched = [i for i, score in sparse_scores.items() if score > 0 and score >= floor]
        matched.sort(key=sparse_scores.get, reverse=True)
        if matched:
            rankings.append(matched)
            weights.append(SPARSE_WEIGHT)

    fused = reciprocal_rank_fusion(rankings, weights=weights)
    order = sorted(fused, key=fused.get, reverse=True)

    shortlist = order[: max(reranker.CANDIDATES, top_k)]
    scores = reranker.rerank(query, [by_id[i].text for i in shortlist]) if query else None

    if scores is not None:
        ranked = sorted(zip(scores, shortlist), key=lambda pair: pair[0], reverse=True)
        # Cross-encoder outputs are logits; squash to 0-1 so the score
        # stays meaningful as a "match" figure in the UI.
        scored = [(_sigmoid(float(score)), by_id[i]) for score, i in ranked]
    else:
        # Reranker unavailable - fall back to the fused ranking. RRF
        # scores are tiny by construction (~1/60) and not similarities,
        # so report them relative to the best hit.
        best = max((fused[i] for i in shortlist), default=1.0) or 1.0
        scored = [(fused[i] / best, by_id[i]) for i in shortlist]

    return [
        {
            "material_id": row.material_id,
            "filename": row.material.filename,
            "chunk_index": row.chunk_index,
            "text": row.text,
            "page": row.page,
            "score": score,
        }
        for score, row in scored[:top_k]
    ]
