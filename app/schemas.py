from uuid import UUID
from pydantic import BaseModel
from typing import List, Optional


class RelatedDocumentSchema(BaseModel):
    id: UUID
    title: str
    relation_type: Optional[str] = None
    label: Optional[str] = None
    view_url: str
    download_url: str


class DocumentSchema(BaseModel):
    id: UUID
    title: str
    content_type: Optional[str] = None
    view_url: str
    download_url: str
    related_documents: List[RelatedDocumentSchema] = []


class TopicSchema(BaseModel):
    id: UUID
    name: str
    documents: List[DocumentSchema] = []
    subtopics: List["TopicSchema"] = []


class DocumentTopicSchema(BaseModel):
    id: UUID
    name: str


class DocumentDetailSchema(BaseModel):
    id: UUID
    title: str
    content_type: Optional[str] = None
    description: Optional[str] = None
    filename: Optional[str] = None
    bucket_name: Optional[str] = None
    object_key: Optional[str] = None
    size_bytes: Optional[int] = None
    preview_mode: str
    view_url: str
    preview_url: str
    download_url: str
    topic: Optional[DocumentTopicSchema] = None
    related_documents: List[RelatedDocumentSchema] = []


TopicSchema.model_rebuild()
