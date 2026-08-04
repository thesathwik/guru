from datetime import datetime

from pydantic import BaseModel


class SubjectCreate(BaseModel):
    name: str
    # Only honoured for an administrator; everyone else gets a subject of
    # their own regardless of what they ask for.
    shared: bool = False
    # Only honoured for the teacher of that class.
    classroom_id: int | None = None


class MaterialOut(BaseModel):
    id: int
    filename: str
    status: str
    error_message: str | None = None
    chunk_count: int | None = None
    char_count: int | None = None
    page_count: int | None = None
    scanned_page_count: int | None = None
    ocr_page_count: int | None = None
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
    shared: bool = False
    classroom_id: int | None = None
    classroom_name: str | None = None

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


class LearnerProfileIn(BaseModel):
    grade: str | None = None
    board: str | None = None
    language: str | None = None
    goals: str | None = None
    learning_style: str | None = None
    analogies: str | None = None
    strengths: str | None = None
    weaknesses: str | None = None
    notes: str | None = None


class LearnerProfileOut(LearnerProfileIn):
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


class MeOut(BaseModel):
    id: int
    email: str | None = None
    display_name: str | None = None
    is_admin: bool = False
    is_teacher: bool = False
    profile: LearnerProfileOut | None = None

    class Config:
        from_attributes = True


class ClassroomCreate(BaseModel):
    name: str


class ClassroomOut(BaseModel):
    id: int
    name: str
    created_at: datetime
    teaching: bool = False
    teacher_name: str | None = None
    member_count: int = 0
    subject_count: int = 0

    class Config:
        from_attributes = True


class ClassMemberCreate(BaseModel):
    email: str


class ClassMemberOut(BaseModel):
    id: int
    email: str
    display_name: str | None = None
    # False means invited but not yet signed up; the invitation is claimed
    # the first time that address signs in.
    joined: bool = False
    added_at: datetime | None = None


class AdminUserOut(BaseModel):
    id: int
    email: str | None = None
    display_name: str | None = None
    is_admin: bool = False
    is_teacher: bool = False
    created_at: datetime | None = None
    last_seen_at: datetime | None = None

    class Config:
        from_attributes = True


class AdminUserUpdate(BaseModel):
    is_teacher: bool
