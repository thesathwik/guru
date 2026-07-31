from datetime import datetime

from pydantic import BaseModel


class SubjectCreate(BaseModel):
    name: str


class MaterialOut(BaseModel):
    id: int
    filename: str
    status: str
    error_message: str | None = None
    chunk_count: int | None = None
    char_count: int | None = None
    page_count: int | None = None
    scanned_page_count: int | None = None
    uploaded_at: datetime
    processed_at: datetime | None = None

    class Config:
        from_attributes = True


class SubjectOut(BaseModel):
    id: int
    name: str
    slug: str
    created_at: datetime
    material_count: int = 0

    class Config:
        from_attributes = True


class SubjectDetailOut(SubjectOut):
    materials: list[MaterialOut] = []


class ChatMessage(BaseModel):
    role: str  # "user" or "assistant"
    content: str


class ChatRequest(BaseModel):
    message: str
    history: list[ChatMessage] = []


class ChatSource(BaseModel):
    filename: str
    chunk_index: int
    score: float
    text: str
    page: int | None = None


class ChatImage(BaseModel):
    id: int
    url: str
    filename: str
    page: int
    width: int | None = None
    height: int | None = None
    caption: str | None = None
    score: float | None = None


class ChatResponse(BaseModel):
    answer: str
    sources: list[ChatSource]
    images: list[ChatImage] = []


class TestCreate(BaseModel):
    material_ids: list[int]
    question_count: int = 10
    time_limit_minutes: int | None = None
    title: str | None = None


# What the student sees while taking the test. Deliberately carries no
# correct_option, expected_answer or explanation - the answers must not
# reach the browser before the attempt is submitted, or they are one
# devtools panel away.
class TestQuestionOut(BaseModel):
    id: int
    position: int
    kind: str
    prompt: str
    options: list[str] | None = None
    points: int


class TestSummaryOut(BaseModel):
    id: int
    subject_id: int
    title: str
    question_count: int
    time_limit_minutes: int | None = None
    max_points: int
    created_at: datetime
    material_filenames: list[str] = []
    attempt_count: int = 0
    best_score: float | None = None

    class Config:
        from_attributes = True


class TestDetailOut(TestSummaryOut):
    questions: list[TestQuestionOut] = []


class AttemptAnswerIn(BaseModel):
    question_id: int
    selected_option: int | None = None
    response: str | None = None


class AttemptSubmit(BaseModel):
    answers: list[AttemptAnswerIn] = []


# The post-submission view, which does include the answers.
class GradedAnswerOut(BaseModel):
    question_id: int
    position: int
    kind: str
    prompt: str
    options: list[str] | None = None
    points: int
    selected_option: int | None = None
    response: str | None = None
    awarded_points: float | None = None
    is_correct: bool | None = None
    feedback: str | None = None
    correct_option: int | None = None
    expected_answer: str | None = None
    explanation: str | None = None
    source_filename: str | None = None
    source_page: int | None = None


class AttemptOut(BaseModel):
    id: int
    test_id: int
    started_at: datetime
    submitted_at: datetime | None = None
    score_points: float | None = None
    max_points: int | None = None
    answers: list[GradedAnswerOut] = []
