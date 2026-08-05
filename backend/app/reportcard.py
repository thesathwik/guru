"""Report cards: the arithmetic, and the paragraph.

The split matters. Every number here is computed in Python from marks and
attendance - totals, percentages, grades, averages. The model is given
those numbers already worked out and asked only to write the comment.

Letting a language model compute a mark would be the same mistake as
letting it compute a fee balance: fluent, plausible and occasionally
wrong, in a document a parent will treat as official. So it never does
arithmetic, and it is told to work only from the figures supplied.

Nothing is stored except the comment. Every number on a card is recomputed
when it is read, so correcting a mark cannot leave a stale total behind on
a card printed last week.
"""
from datetime import datetime

from . import models, tutor

# Roughly the CBSE bands. A school with its own scale will want this
# configurable; it is deliberately one list in one place until then.
GRADE_BANDS = [
    (90, "A1"), (80, "A2"), (70, "B1"), (60, "B2"),
    (50, "C1"), (40, "C2"), (33, "D"), (0, "E"),
]


def grade_for(percent: float | None) -> str | None:
    if percent is None:
        return None
    for floor, letter in GRADE_BANDS:
        if percent >= floor:
            return letter
    return "E"


def subject_results(db, enrolment, term) -> list[dict]:
    """Marks for this enrolment in this term, grouped by subject.

    A missing mark means the student did not sit the assessment, and is
    left out rather than counted as zero - averaging an absence with a
    score would quietly understate them.
    """
    rows = (
        db.query(models.Mark, models.Assessment)
        .join(models.Assessment, models.Mark.assessment_id == models.Assessment.id)
        .filter(
            models.Mark.enrolment_id == enrolment.id,
            models.Assessment.term_id == term.id,
        )
        .all()
    )

    by_subject: dict[str, dict] = {}
    for mark, assessment in rows:
        subject = assessment.subject_name or "General"
        entry = by_subject.setdefault(
            subject, {"subject": subject, "assessments": [], "scored": 0.0, "out_of": 0.0}
        )
        entry["assessments"].append(
            {
                "name": assessment.name,
                "score": mark.score,
                "max_marks": assessment.max_marks,
                "sat": mark.score is not None,
            }
        )
        if mark.score is not None:
            entry["scored"] += mark.score
            entry["out_of"] += assessment.max_marks or 0

    results = []
    for entry in by_subject.values():
        percent = (
            round(100 * entry["scored"] / entry["out_of"], 1)
            if entry["out_of"]
            else None
        )
        entry["percent"] = percent
        entry["grade"] = grade_for(percent)
        results.append(entry)
    return sorted(results, key=lambda r: r["subject"])


def attendance_for(db, enrolment, term) -> dict:
    """Attendance over the term. Same rule as the class summary: late
    counts as attended, excused is left out of the denominator."""
    query = (
        db.query(models.AttendanceRecord, models.AttendanceSession)
        .join(
            models.AttendanceSession,
            models.AttendanceRecord.session_id == models.AttendanceSession.id,
        )
        .filter(models.AttendanceRecord.enrolment_id == enrolment.id)
    )
    if term.starts_on:
        query = query.filter(models.AttendanceSession.on_date >= term.starts_on)
    if term.ends_on:
        query = query.filter(models.AttendanceSession.on_date <= term.ends_on)

    counts = {"present": 0, "absent": 0, "late": 0, "excused": 0}
    for record, _ in query.all():
        counts[record.status] = counts.get(record.status, 0) + 1

    attended = counts["present"] + counts["late"]
    countable = attended + counts["absent"]
    return {
        **counts,
        "sessions_counted": countable,
        "percent": round(100 * attended / countable) if countable else None,
    }


def weak_topics(db, enrolment, term, limit: int = 6) -> list[dict]:
    """What this student actually got wrong, from their test attempts.

    This is the part a conventional report card cannot produce. The
    questions carry the material and page they were drawn from, so the
    comment can point at what to revise rather than saying "needs to work
    harder".
    """
    student_user_id = enrolment.student.user_id
    if student_user_id is None:
        return []

    query = (
        db.query(models.TestAnswer, models.TestQuestion)
        .join(
            models.TestQuestion,
            models.TestAnswer.question_id == models.TestQuestion.id,
        )
        .join(
            models.TestAttempt,
            models.TestAnswer.attempt_id == models.TestAttempt.id,
        )
        .filter(
            models.TestAttempt.user_id == student_user_id,
            models.TestAttempt.submitted_at.isnot(None),
            models.TestAnswer.is_correct.is_(False),
        )
    )
    if term.starts_on:
        query = query.filter(models.TestAttempt.submitted_at >= term.starts_on)
    if term.ends_on:
        query = query.filter(models.TestAttempt.submitted_at <= term.ends_on)

    topics = []
    for answer, question in query.all()[: limit * 3]:
        topics.append(
            {
                "question": question.prompt,
                "source": question.source_filename,
                "page": question.source_page,
            }
        )
    return topics[:limit]


def build(db, enrolment, term) -> dict:
    """Everything on the card except the comment, all recomputed."""
    subjects = subject_results(db, enrolment, term)
    scored = sum(s["scored"] for s in subjects)
    out_of = sum(s["out_of"] for s in subjects)
    overall = round(100 * scored / out_of, 1) if out_of else None

    return {
        "student": {
            "id": enrolment.student.id,
            "full_name": enrolment.student.full_name,
            "admission_number": enrolment.student.admission_number,
            "roll_number": enrolment.roll_number,
        },
        "classroom": enrolment.classroom.name if enrolment.classroom else None,
        "term": term.name,
        "subjects": subjects,
        "overall_percent": overall,
        "overall_grade": grade_for(overall),
        "attendance": attendance_for(db, enrolment, term),
        "weak_topics": weak_topics(db, enrolment, term),
    }


COMMENT_PROMPT = """Write a short report card comment about {name} for {term}.

Every figure below is already calculated. Use them as given: do not add up,
average, re-check or infer any number, and do not state a figure that does
not appear here.

Marks:
{subjects}

Attendance: {attendance}

Questions they answered incorrectly this term:
{weak}

Write 2-4 sentences addressed to the student's parents, in a warm, plain
register a teacher would actually use. Say what went well, name
specifically what to work on if the marks or the wrong answers show
something, and end with a practical next step. No greeting, no sign-off,
no bullet points. If there is too little here to say anything meaningful,
say only that briefly rather than padding."""


def draft_comment(card: dict) -> str:
    """Asks the model for the paragraph, given the figures.

    Deliberately the last step and the only generative one: if this fails,
    the card is still complete and correct without it.
    """
    lines = []
    for s in card["subjects"]:
        line = f"- {s['subject']}: {s['scored']:g}/{s['out_of']:g}"
        if s["percent"] is not None:
            line += f" ({s['percent']}%, grade {s['grade']})"
        lines.append(line)
    subjects = "\n".join(lines) or "- no marks recorded this term"

    attendance = card["attendance"]
    attendance_text = (
        f"{attendance['percent']}% "
        f"({attendance['present']} present, {attendance['absent']} absent, "
        f"{attendance['late']} late, {attendance['excused']} excused)"
        if attendance["percent"] is not None
        else "not recorded"
    )

    weak = (
        "\n".join(
            f"- {t['question'][:160]}"
            + (f" [{t['source']}{f', page {t['page']}' if t['page'] else ''}]"
               if t["source"] else "")
            for t in card["weak_topics"]
        )
        or "- none recorded"
    )

    response = tutor._get_client().chat.completions.create(
        model=tutor._model_name(),
        messages=[
            {
                "role": "user",
                "content": COMMENT_PROMPT.format(
                    name=card["student"]["full_name"].split()[0],
                    term=card["term"],
                    subjects=subjects,
                    attendance=attendance_text,
                    weak=weak,
                ),
            }
        ],
        temperature=0.4,
        # Four sentences need very few tokens, but this model reasons before
        # answering and that reasoning is drawn from the same budget - at
        # 400 the comment came back cut off mid-sentence.
        max_tokens=2048,
    )

    choice = response.choices[0]
    if getattr(choice, "finish_reason", None) == "length":
        # Better no comment than half a sentence: the card is complete
        # without one, and the caller records the failure.
        raise ValueError("The comment was cut off before it finished")

    text = (choice.message.content or "").strip()
    if not text:
        raise ValueError("The model returned an empty comment")
    return text
