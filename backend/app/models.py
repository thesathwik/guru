from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
)
from sqlalchemy.orm import relationship

from .database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    # Identity Platform's subject id. Keyed on this rather than email
    # because an email can be changed or reassigned; the uid cannot.
    auth_uid = Column(String, unique=True, nullable=False, index=True)
    email = Column(String, nullable=True)
    display_name = Column(String, nullable=True)
    # Who may add to the shared library. Seeded from ADMIN_EMAILS.
    is_admin = Column(Boolean, nullable=False, default=False)
    # Set by an administrator. A teacher can run classes of their own; it
    # grants nothing over the shared library or over anyone else's class.
    is_teacher = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_seen_at = Column(DateTime, nullable=True)

    profile = relationship(
        "LearnerProfile",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )


class LearnerProfile(Base):
    """What the student tells us about themselves, fed to the tutor.

    Everything here is stated by the student rather than inferred, so it
    is correctable and carries no risk of the tutor acting on a wrong
    guess about them. Inferred signals - weak topics from test results,
    recurring themes from questions - belong alongside this later, kept
    separate precisely so the two can be told apart.
    """

    __tablename__ = "learner_profiles"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True)

    grade = Column(String, nullable=True)  # "Class 9"
    board = Column(String, nullable=True)  # "CBSE", "ICSE", "State board"
    language = Column(String, nullable=True)  # preferred language for answers
    goals = Column(Text, nullable=True)  # "board exams in March"

    # How to teach this particular student. The reason a tutor beats a
    # search box: the same fact explained the way this person actually
    # follows it.
    learning_style = Column(Text, nullable=True)
    analogies = Column(Text, nullable=True)  # subjects whose examples land
    strengths = Column(Text, nullable=True)
    weaknesses = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)  # anything else worth remembering

    updated_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="profile")


class Classroom(Base):
    """A teacher's class. Its subjects are visible to its members and to
    nobody else - a third tier between the shared library and a student's
    own uploads."""

    __tablename__ = "classrooms"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    teacher_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    teacher = relationship("User", foreign_keys=[teacher_id])
    members = relationship(
        "ClassMember", back_populates="classroom", cascade="all, delete-orphan"
    )
    subjects = relationship("Subject", back_populates="classroom")


class ClassMember(Base):
    """A student on a class roster.

    Keyed on email rather than user id, because a teacher adds people
    before they have necessarily signed up. `user_id` stays NULL until
    somebody signs in with that address, at which point the invitation is
    claimed - otherwise a teacher would have to wait for every student to
    register, and re-add anyone who registered later.
    """

    __tablename__ = "class_members"

    id = Column(Integer, primary_key=True)
    classroom_id = Column(Integer, ForeignKey("classrooms.id"), nullable=False)
    email = Column(String, nullable=False)  # lowercased
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    added_at = Column(DateTime, default=datetime.utcnow)
    joined_at = Column(DateTime, nullable=True)

    classroom = relationship("Classroom", back_populates="members")
    user = relationship("User", foreign_keys=[user_id])


class Subject(Base):
    __tablename__ = "subjects"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    slug = Column(String, unique=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    # NULL means the shared library: visible to everyone. Set means a
    # personal upload, visible only to that user. Existing rows are all
    # NULL, so the library everyone already sees stays shared.
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    # Set means the subject belongs to a class: visible to its roster and
    # its teacher rather than to everyone. Scoping classes at the subject
    # level is what keeps retrieval unchanged - inside a subject the
    # existing owner_id rule already separates class material (NULL) from
    # a student's own notes.
    classroom_id = Column(Integer, ForeignKey("classrooms.id"), nullable=True)

    classroom = relationship("Classroom", back_populates="subjects")
    materials = relationship(
        "Material", back_populates="subject", cascade="all, delete-orphan"
    )
    tests = relationship("Test", back_populates="subject", cascade="all, delete-orphan")


class Material(Base):
    __tablename__ = "materials"

    id = Column(Integer, primary_key=True)
    subject_id = Column(Integer, ForeignKey("subjects.id"), nullable=False)
    # NULL means the shared library: visible to everyone. Set means a
    # personal upload, visible only to that user. Existing rows are all
    # NULL, so the library everyone already sees stays shared.
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    filename = Column(String, nullable=False)
    raw_path = Column(String, nullable=False)
    processed_path = Column(String, nullable=True)
    # uploaded, queued, processing, processed, error
    status = Column(String, default="uploaded")
    # When the current processing attempt started. A worker that dies
    # leaves the row in "processing" forever otherwise; this is what lets
    # a later run tell "in progress" from "abandoned".
    processing_started_at = Column(DateTime, nullable=True)
    # Attempts so far, so a material that kills every worker it touches
    # (out of memory, a malformed file) fails for good instead of being
    # retried until the end of time.
    attempts = Column(Integer, nullable=False, default=0)
    error_message = Column(Text, nullable=True)
    chunk_count = Column(Integer, nullable=True)
    char_count = Column(Integer, nullable=True)
    page_count = Column(Integer, nullable=True)
    # Pages that carry no extractable text and are mostly one image - i.e.
    # scanned or photographed. Their content is invisible to retrieval
    # until OCR runs, so this is surfaced rather than left implicit.
    scanned_page_count = Column(Integer, nullable=True)
    # How many of those scanned pages text recognition actually recovered.
    # Below scanned_page_count when a page was blank or unreadable.
    ocr_page_count = Column(Integer, nullable=True)
    uploaded_at = Column(DateTime, default=datetime.utcnow)
    processed_at = Column(DateTime, nullable=True)

    subject = relationship("Subject", back_populates="materials")
    chunks = relationship("Chunk", back_populates="material", cascade="all, delete-orphan")
    images = relationship(
        "MaterialImage", back_populates="material", cascade="all, delete-orphan"
    )


class Chunk(Base):
    __tablename__ = "chunks"

    id = Column(Integer, primary_key=True)
    # Denormalized from material.subject_id so retrieval can filter by
    # subject directly, without joining through materials - this is what
    # keeps every subject's tutor scoped to only its own material in a
    # single shared embeddings table.
    subject_id = Column(Integer, ForeignKey("subjects.id"), nullable=False)
    material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)
    # Denormalised from material.owner_id for the same reason subject_id is:
    # retrieval filters on it directly, on every query, without a join.
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    chunk_index = Column(Integer, nullable=False)
    text = Column(Text, nullable=False)
    embedding = Column(Text, nullable=False)  # JSON-encoded list[float]
    # Source page (1-based) this chunk's text starts on, used to pull in
    # figures/diagrams from the same page when the chunk is retrieved.
    page = Column(Integer, nullable=True)
    # How the text was obtained: "native" (extracted from the file) or
    # "ocr" (recognised from an image of the page). Retrieval weights BM25
    # above dense matching, which relies on exact terms - and those are
    # exactly what OCR gets wrong, so the two need telling apart. Recorded
    # from the start because backfilling it would mean reprocessing every
    # material again.
    source = Column(String, nullable=False, default="native")

    material = relationship("Material", back_populates="chunks")


class MaterialImage(Base):
    __tablename__ = "material_images"

    id = Column(Integer, primary_key=True)
    subject_id = Column(Integer, ForeignKey("subjects.id"), nullable=False)
    material_id = Column(Integer, ForeignKey("materials.id"), nullable=False)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    page = Column(Integer, nullable=False)
    path = Column(String, nullable=False)
    content_type = Column(String, nullable=False)
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    # The figure's caption (or nearest describing text). Embedding this
    # is what lets retrieval pick the *right* figure on a page rather
    # than every figure that happens to share the page.
    caption = Column(Text, nullable=True)
    caption_embedding = Column(Text, nullable=True)  # JSON-encoded list[float]

    material = relationship("Material", back_populates="images")


# Which materials a test was generated from - the student picks these
# before generating, so a test can be scoped to one chapter rather than
# the whole subject. ON DELETE CASCADE keeps the rows from outliving a
# deleted material; nothing re-reads them after generation, so a test
# whose source material is later removed stays intact and takeable.
test_materials = Table(
    "test_materials",
    Base.metadata,
    Column("test_id", ForeignKey("tests.id", ondelete="CASCADE"), primary_key=True),
    Column(
        "material_id", ForeignKey("materials.id", ondelete="CASCADE"), primary_key=True
    ),
)


class Test(Base):
    __tablename__ = "tests"

    id = Column(Integer, primary_key=True)
    subject_id = Column(Integer, ForeignKey("subjects.id"), nullable=False)
    # NULL only for tests generated before sign-in existed.
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    title = Column(String, nullable=False)
    question_count = Column(Integer, nullable=False)
    # Minutes, or NULL for an untimed test. Enforced in the browser only -
    # it paces the student rather than securing the test.
    time_limit_minutes = Column(Integer, nullable=True)
    max_points = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    subject = relationship("Subject", back_populates="tests")
    materials = relationship("Material", secondary=test_materials)
    questions = relationship(
        "TestQuestion",
        back_populates="test",
        cascade="all, delete-orphan",
        order_by="TestQuestion.position",
    )
    attempts = relationship(
        "TestAttempt", back_populates="test", cascade="all, delete-orphan"
    )


class TestQuestion(Base):
    __tablename__ = "test_questions"

    id = Column(Integer, primary_key=True)
    test_id = Column(Integer, ForeignKey("tests.id"), nullable=False)
    position = Column(Integer, nullable=False)
    kind = Column(String, nullable=False)  # mcq, short, long
    prompt = Column(Text, nullable=False)
    # MCQ only: JSON-encoded list[str], and the index of the right one.
    options = Column(Text, nullable=True)
    correct_option = Column(Integer, nullable=True)
    # Written questions: the model answer the grader marks against. Also
    # shown to the student as the correct answer after submission.
    expected_answer = Column(Text, nullable=True)
    explanation = Column(Text, nullable=True)
    points = Column(Integer, nullable=False, default=1)
    # Where in the material this came from, so a student can go and read
    # the passage a question was drawn from.
    source_filename = Column(String, nullable=True)
    source_page = Column(Integer, nullable=True)

    test = relationship("Test", back_populates="questions")


class TestAttempt(Base):
    __tablename__ = "test_attempts"

    id = Column(Integer, primary_key=True)
    test_id = Column(Integer, ForeignKey("tests.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    started_at = Column(DateTime, default=datetime.utcnow)
    submitted_at = Column(DateTime, nullable=True)
    score_points = Column(Float, nullable=True)
    max_points = Column(Integer, nullable=True)

    test = relationship("Test", back_populates="attempts")
    answers = relationship(
        "TestAnswer", back_populates="attempt", cascade="all, delete-orphan"
    )


class TestAnswer(Base):
    __tablename__ = "test_answers"

    id = Column(Integer, primary_key=True)
    attempt_id = Column(Integer, ForeignKey("test_attempts.id"), nullable=False)
    question_id = Column(Integer, ForeignKey("test_questions.id"), nullable=False)
    # MCQ answers arrive as an option index, written ones as text.
    selected_option = Column(Integer, nullable=True)
    response = Column(Text, nullable=True)
    awarded_points = Column(Float, nullable=True)
    is_correct = Column(Boolean, nullable=True)
    feedback = Column(Text, nullable=True)

    attempt = relationship("TestAttempt", back_populates="answers")
    question = relationship("TestQuestion")
