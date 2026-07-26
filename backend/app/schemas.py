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
