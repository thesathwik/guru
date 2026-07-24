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
