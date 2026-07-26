from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
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
