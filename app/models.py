from sqlalchemy import Column, String, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
import uuid

from app.database import Base


class Topic(Base):
    __tablename__ = "topics"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    parent_id = Column(UUID(as_uuid=True), ForeignKey("topics.id"), nullable=True)
    name = Column(String, nullable=False)


class Document(Base):
    __tablename__ = "documents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    topic_id = Column(UUID(as_uuid=True), ForeignKey("topics.id"), nullable=True)
    title = Column(String, nullable=False)
    content_type = Column(String)


class DocumentRelation(Base):
    __tablename__ = "document_relations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    from_document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id"))
    to_document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id"))
    relation_type = Column(String)
    label = Column(String)
