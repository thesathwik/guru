"""Test generation and grading.

Two LLM passes, both grounded in the student's own material:

- `generate_questions` builds a mixed-format test from a *broad sweep* of
  the chosen materials. This deliberately does not use
  embeddings.search_chunks: retrieval answers a question, and here there
  is no question yet - asking for the top-k passages for some invented
  query would concentrate every question on whatever the retriever liked
  best. Coverage is what a test needs, so context is sampled evenly
  across the selected materials instead.

- `grade_written_answers` marks the short/long responses. MCQs are marked
  by comparing indexes in `grade_attempt` and never reach the model, so a
  test of only MCQs costs nothing to mark and cannot be graded
  inconsistently.
"""
import json
import os

from . import models, tutor

# How much material to put in front of the model when generating. Gemini
# Flash has a large context window, so the practical limit is latency and
# cost rather than tokens; this is enough for a couple of chapters.
CONTEXT_CHAR_BUDGET = int(os.environ.get("TEST_CONTEXT_CHARS", "60000"))

MAX_QUESTIONS = int(os.environ.get("TEST_MAX_QUESTIONS", "30"))

# Written answers are worth more than a lucky guess on a four-option MCQ.
POINTS = {"mcq": 1, "short": 3, "long": 5}


class TestGenerationError(Exception):
    pass


def question_mix(count: int) -> dict[str, int]:
    """Splits a question count into formats.

    MCQ-heavy, because they mark deterministically and cover ground
    cheaply, but always with at least one written question once the test
    is long enough to afford one - a test of only MCQs cannot show
    whether a student can explain anything.
    """
    if count <= 2:
        return {"mcq": count, "short": 0, "long": 0}
    if count <= 4:
        return {"mcq": count - 1, "short": 1, "long": 0}
    if count <= 6:
        return {"mcq": count - 2, "short": 1, "long": 1}
    long_n = max(1, count // 8)
    short_n = max(1, round(count * 0.3))
    return {"mcq": count - short_n - long_n, "short": short_n, "long": long_n}


def gather_context(db, subject_id: int, material_ids: list[int]) -> str:
    """Samples chunks evenly across the chosen materials.

    Each material gets an equal share of the budget, and within a
    material the chunks are taken at a stride across the whole document
    rather than from the front. Otherwise a long chapter would spend its
    entire share on its opening pages and every question would come from
    the introduction.
    """
    rows = (
        db.query(models.Chunk)
        .filter(
            models.Chunk.subject_id == subject_id,
            models.Chunk.material_id.in_(material_ids),
        )
        .order_by(models.Chunk.material_id, models.Chunk.chunk_index)
        .all()
    )
    if not rows:
        return ""

    by_material: dict[int, list] = {}
    for row in rows:
        by_material.setdefault(row.material_id, []).append(row)

    filenames = {
        m.id: m.filename
        for m in db.query(models.Material)
        .filter(models.Material.id.in_(by_material.keys()))
        .all()
    }

    per_material = max(CONTEXT_CHAR_BUDGET // len(by_material), 1000)
    blocks = []
    for material_id, chunks in by_material.items():
        # Stride so the sample spans the document; +1 avoids a zero step.
        estimated = sum(len(c.text) for c in chunks)
        stride = max(1, round(estimated / per_material)) if estimated else 1
        used = 0
        for chunk in chunks[::stride]:
            if used + len(chunk.text) > per_material:
                break
            used += len(chunk.text)
            page = f", page {chunk.page}" if chunk.page else ""
            blocks.append(
                f"[{filenames.get(material_id, 'material')}{page}]\n{chunk.text}"
            )

    return "\n\n---\n\n".join(blocks)


GENERATE_PROMPT = """You are setting a test for a student studying {subject_name}.

Write exactly {total} questions based ONLY on the reference material below:
- {mcq} multiple-choice questions, each with exactly 4 options and one correct answer
- {short} short-answer questions, answerable in 2-3 sentences
- {long} long-answer questions, requiring a paragraph explaining or analysing something

Rules:
- Every question must be answerable from the reference material alone. Do not use
  outside knowledge and do not ask about anything the material does not cover.
- Spread the questions across the whole of the material, not just its beginning.
- Test understanding, not just recall of wording: ask why and how, not only what.
- Do not write questions about the material as an artefact (page numbers,
  figure numbers, "according to the text").
- For multiple-choice, the wrong options must be plausible and roughly the same
  length as the right one, so the answer is not guessable from its shape alone.
- Cite the source file (and page, if shown) each question came from.

Return ONLY a JSON object, no prose and no code fences:
{{
  "questions": [
    {{
      "kind": "mcq",
      "prompt": "...",
      "options": ["...", "...", "...", "..."],
      "correct_option": 0,
      "explanation": "why that option is right",
      "source_filename": "...",
      "source_page": 4
    }},
    {{
      "kind": "short",
      "prompt": "...",
      "expected_answer": "the answer a full-marks response would give",
      "explanation": "what a marker should look for",
      "source_filename": "...",
      "source_page": 7
    }}
  ]
}}
Use "long" for the long-answer questions, in the same shape as "short".

Reference material:
{context}
"""


def _message_text(response) -> str:
    """Pulls the reply text out, turning a truncated response into a
    message that says so.

    The whole test is a single JSON object, so hitting the output cap does
    not lose a trailing question - it produces JSON that cannot be parsed
    at all. Without this the failure surfaces as a bare "invalid JSON at
    character 19680", which says nothing about what to do differently.
    """
    choice = response.choices[0]
    if getattr(choice, "finish_reason", None) == "length":
        raise TestGenerationError(
            "The model ran out of room before finishing the test. "
            "Try again with fewer questions."
        )
    return choice.message.content


def _parse_json_object(raw: str) -> dict:
    """Models wrap JSON in code fences often enough to be worth handling
    rather than failing the whole test generation over."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    text = text.strip()
    # Fall back to the outermost braces if there is still stray prose.
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise TestGenerationError("The model did not return JSON")
        text = text[start : end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise TestGenerationError(f"The model returned invalid JSON: {exc}") from exc


def _clean_question(item: dict, position: int) -> models.TestQuestion:
    kind = (item.get("kind") or "").strip().lower()
    if kind not in POINTS:
        raise TestGenerationError(f"Unknown question kind {kind!r}")
    prompt = (item.get("prompt") or "").strip()
    if not prompt:
        raise TestGenerationError("A question came back with no prompt")

    options_json = None
    correct_option = None
    expected_answer = None

    if kind == "mcq":
        options = [str(o).strip() for o in (item.get("options") or []) if str(o).strip()]
        if len(options) < 2:
            raise TestGenerationError("A multiple-choice question had too few options")
        try:
            correct_option = int(item.get("correct_option"))
        except (TypeError, ValueError):
            raise TestGenerationError("A multiple-choice question had no correct option")
        if not 0 <= correct_option < len(options):
            raise TestGenerationError("correct_option is out of range")
        options_json = json.dumps(options)
    else:
        expected_answer = (item.get("expected_answer") or "").strip()
        if not expected_answer:
            raise TestGenerationError("A written question came back with no answer")

    page = item.get("source_page")
    try:
        page = int(page) if page is not None else None
    except (TypeError, ValueError):
        page = None

    return models.TestQuestion(
        position=position,
        kind=kind,
        prompt=prompt,
        options=options_json,
        correct_option=correct_option,
        expected_answer=expected_answer,
        explanation=(item.get("explanation") or "").strip() or None,
        points=POINTS[kind],
        source_filename=(str(item.get("source_filename")).strip() or None)
        if item.get("source_filename")
        else None,
        source_page=page,
    )


def generate_questions(
    db, subject, material_ids: list[int], count: int
) -> list[models.TestQuestion]:
    context = gather_context(db, subject.id, material_ids)
    if not context:
        raise TestGenerationError(
            "The selected materials have no processed content to build a test from"
        )

    mix = question_mix(count)
    prompt = GENERATE_PROMPT.format(
        subject_name=subject.name,
        total=count,
        mcq=mix["mcq"],
        short=mix["short"],
        long=mix["long"],
        context=context,
    )

    response = tutor._get_client().chat.completions.create(
        model=tutor._model_name(),
        messages=[{"role": "user", "content": prompt}],
        # Some variety between two tests over the same material, without
        # drifting off what the material actually says.
        temperature=0.7,
        # A whole test is one JSON object, so the reply is long: at the
        # provider's default cap a 20+ question test is cut off mid-object
        # and fails to parse. Budget per question rather than picking a
        # single number that is wasteful for a 5-question test and too
        # small for a 30-question one.
        max_tokens=min(2000 + count * 700, 32768),
    )

    payload = _parse_json_object(_message_text(response))
    items = payload.get("questions")
    if not isinstance(items, list) or not items:
        raise TestGenerationError("The model returned no questions")

    questions = [_clean_question(item, i) for i, item in enumerate(items[:count])]
    if not questions:
        raise TestGenerationError("No usable questions came back")
    return questions


GRADE_PROMPT = """You are marking a student's test answers for {subject_name}.

For each answer, award marks out of the stated maximum and explain the mark in
one or two sentences addressed to the student ("you..."). Judge whether the
student conveyed the substance of the expected answer - accept different wording,
correct extra detail, and partial answers deserving partial marks. Do not
penalise spelling or phrasing. An empty or off-topic answer scores 0.

Return ONLY a JSON object, no prose and no code fences:
{{"marks": [{{"id": <question id>, "points": <number>, "feedback": "..."}}]}}

Answers to mark:
{answers}
"""


def grade_written_answers(subject, items: list[dict]) -> dict[int, dict]:
    """Marks every written answer in one call, keyed by question id.

    One call rather than one per answer keeps marking a single round trip
    and lets the model apply a consistent standard across the test.
    """
    if not items:
        return {}

    blocks = []
    for item in items:
        blocks.append(
            f"--- Question id {item['id']} (max {item['points']} marks)\n"
            f"Question: {item['prompt']}\n"
            f"Expected answer: {item['expected_answer']}\n"
            f"Student's answer: {item['response'] or '(left blank)'}"
        )

    response = tutor._get_client().chat.completions.create(
        model=tutor._model_name(),
        messages=[
            {
                "role": "user",
                "content": GRADE_PROMPT.format(
                    subject_name=subject.name, answers="\n\n".join(blocks)
                ),
            }
        ],
        # Marking should be as reproducible as the model allows.
        temperature=0.0,
        max_tokens=min(1000 + len(items) * 400, 16384),
    )

    payload = _parse_json_object(_message_text(response))
    by_max = {item["id"]: item["points"] for item in items}
    marks: dict[int, dict] = {}
    for mark in payload.get("marks") or []:
        try:
            question_id = int(mark.get("id"))
            points = float(mark.get("points"))
        except (TypeError, ValueError):
            continue
        if question_id not in by_max:
            continue
        marks[question_id] = {
            # Never trust the model to stay inside the mark range.
            "points": max(0.0, min(points, float(by_max[question_id]))),
            "feedback": (mark.get("feedback") or "").strip() or None,
        }
    return marks


def grade_attempt(db, attempt) -> None:
    """Marks an attempt in place: exact comparison for MCQs, one batched
    LLM call for the written answers."""
    from datetime import datetime

    questions = {q.id: q for q in attempt.test.questions}
    written = []

    for answer in attempt.answers:
        question = questions.get(answer.question_id)
        if question is None:
            continue
        if question.kind == "mcq":
            answer.is_correct = (
                answer.selected_option is not None
                and answer.selected_option == question.correct_option
            )
            answer.awarded_points = float(question.points) if answer.is_correct else 0.0
            answer.feedback = None
        else:
            written.append(
                {
                    "id": question.id,
                    "prompt": question.prompt,
                    "expected_answer": question.expected_answer or "",
                    "points": question.points,
                    "response": answer.response,
                }
            )

    marks = grade_written_answers(attempt.test.subject, written) if written else {}

    for answer in attempt.answers:
        question = questions.get(answer.question_id)
        if question is None or question.kind == "mcq":
            continue
        mark = marks.get(question.id)
        if mark is None:
            # The grader skipped it. Leave it unmarked rather than
            # silently scoring zero, which would look like a wrong answer.
            answer.awarded_points = None
            answer.is_correct = None
            answer.feedback = "This answer could not be marked automatically."
            continue
        answer.awarded_points = mark["points"]
        answer.feedback = mark["feedback"]
        answer.is_correct = mark["points"] >= question.points * 0.75

    attempt.score_points = sum(a.awarded_points or 0.0 for a in attempt.answers)
    attempt.max_points = sum(q.points for q in attempt.test.questions)
    attempt.submitted_at = datetime.utcnow()
