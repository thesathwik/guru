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


class Subject(Base):
    __tablename__ = "subjects"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    slug = Column(String, unique=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    materials = relationship(
        "Material", back_populates="subject", cascade="all, delete-orphan"
    )
    tests = relationship("Test", back_populates="subject", cascade="all, delete-orphan")


class Material(Base):
    __tablename__ = "materials"

    id = Column(Integer, primary_key=True)
    subject_id = Column(Integer, ForeignKey("subjects.id"), nullable=False)
    filename = Column(String, nullable=False)
    raw_path = Column(String, nullable=False)
    processed_path = Column(String, nullable=True)
    status = Column(String, default="uploaded")  # uploaded, processing, processed, error
    error_message = Column(Text, nullable=True)
    chunk_count = Column(Integer, nullable=True)
    char_count = Column(Integer, nullable=True)
    page_count = Column(Integer, nullable=True)
    # Pages that carry no extractable text and are mostly one image - i.e.
    # scanned or photographed. Their content is invisible to retrieval
    # until OCR runs, so this is surfaced rather than left implicit.
    scanned_page_count = Column(Integer, nullable=True)
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
