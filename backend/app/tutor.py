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

    from openai import AzureOpenAI

    _client = AzureOpenAI(
        azure_endpoint=endpoint,
        api_key=api_key,
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
    )
    return _client


def _build_context(matches: list[dict]) -> str:
    if not matches:
        return "(No material has been uploaded for this subject yet.)"
    return "\n\n---\n\n".join(f"[From {m['filename']}]\n{m['text']}" for m in matches)


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
            {"filename": m["filename"], "chunk_index": m["chunk_index"], "score": m["score"]}
            for m in matches
        ],
    }
