import re
from datetime import date, datetime
from typing import Any, Dict, List, Literal, Optional, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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


# =========================================================
# ПЛАНИРОВЩИК (календарь)
# =========================================================

HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
MAX_NOTE_TAGS = 20


def _check_color(value: Optional[str]) -> Optional[str]:
    if value is not None and not HEX_COLOR.match(value):
        raise ValueError("цвет должен быть в формате #RRGGBB")
    return value


def _strip_text(value: str, field: str, low: int, high: int) -> str:
    value = value.strip()
    if not low <= len(value) <= high:
        raise ValueError(f"{field}: длина должна быть от {low} до {high} символов")
    return value


class PlannerTagCreate(BaseModel):
    name: str
    color: str

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _strip_text(value, "название", 1, 30)

    @field_validator("color")
    @classmethod
    def _color(cls, value: str) -> str:
        return _check_color(value)


class PlannerTagPatch(BaseModel):
    name: Optional[str] = None
    color: Optional[str] = None
    position: Optional[int] = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _no_nulls(self):
        for field in self.model_fields_set:
            if getattr(self, field) is None:
                raise ValueError(f"{field}: значение не может быть null")
        return self

    @field_validator("name")
    @classmethod
    def _name(cls, value: Optional[str]) -> Optional[str]:
        return value if value is None else _strip_text(value, "название", 1, 30)

    @field_validator("color")
    @classmethod
    def _color(cls, value: Optional[str]) -> Optional[str]:
        return _check_color(value)


class PlannerTagSchema(BaseModel):
    id: UUID
    name: str
    color: str
    position: int


class PlannerNoteCreate(BaseModel):
    title: str
    start: date
    end: date
    description: str = Field(default="", max_length=500)
    color: Optional[str] = None
    tag_ids: List[UUID] = Field(default_factory=list, max_length=MAX_NOTE_TAGS)

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        return _strip_text(value, "заголовок", 1, 80)

    @field_validator("color")
    @classmethod
    def _color(cls, value: Optional[str]) -> Optional[str]:
        return _check_color(value)

    @field_validator("tag_ids")
    @classmethod
    def _unique_tags(cls, value: List[UUID]) -> List[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("tag_ids: теги не должны повторяться")
        return value

    @model_validator(mode="after")
    def _period(self):
        if self.end < self.start:
            raise ValueError("end: конец записи раньше начала")
        return self


class PlannerNotePatch(BaseModel):
    title: Optional[str] = None
    start: Optional[date] = None
    end: Optional[date] = None
    description: Optional[str] = Field(default=None, max_length=500)
    color: Optional[str] = None  # null сбрасывает цвет записи
    tag_ids: Optional[List[UUID]] = Field(default=None, max_length=MAX_NOTE_TAGS)

    @model_validator(mode="after")
    def _no_nulls(self):
        for field in self.model_fields_set - {"color"}:
            if getattr(self, field) is None:
                raise ValueError(f"{field}: значение не может быть null")
        return self

    @field_validator("title")
    @classmethod
    def _title(cls, value: Optional[str]) -> Optional[str]:
        return value if value is None else _strip_text(value, "заголовок", 1, 80)

    @field_validator("color")
    @classmethod
    def _color(cls, value: Optional[str]) -> Optional[str]:
        return _check_color(value)

    @field_validator("tag_ids")
    @classmethod
    def _unique_tags(cls, value: Optional[List[UUID]]) -> Optional[List[UUID]]:
        if value is not None and len(set(value)) != len(value):
            raise ValueError("tag_ids: теги не должны повторяться")
        return value


class PlannerNoteSchema(BaseModel):
    id: UUID
    title: str
    start: date
    end: date
    description: str
    color: Optional[str] = None
    tag_ids: List[UUID] = []
    created_at: datetime
    updated_at: datetime


# =========================================================
# КАЛЬКУЛЯТОР ОЦЕНОК
# =========================================================

COMPONENT_KEY = re.compile(r"^[A-Za-z0-9_]+$")
WEIGHT_TOLERANCE = 0.001


class GradeItems(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int = Field(ge=1, le=100)
    aggregate: Literal["mean", "floor", "floor_below_4"]
    names: Optional[List[str]] = None

    @model_validator(mode="after")
    def _names(self):
        if self.names is not None and len(self.names) != self.count:
            raise ValueError("names: подписей должно быть столько же, сколько оценок (count)")
        return self


class GradeComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    short_label: Optional[str] = None
    weight: float = Field(ge=0, le=1)
    is_exam: bool = False
    min_score: Optional[float] = Field(default=None, ge=0, le=10)
    items: Optional[GradeItems] = None

    @field_validator("key")
    @classmethod
    def _key(cls, value: str) -> str:
        if not COMPONENT_KEY.match(value):
            raise ValueError("key: только латиница, цифры и _")
        return value

    @field_validator("label")
    @classmethod
    def _label(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("label: название не может быть пустым")
        return value


def validate_components(components: List[GradeComponent]) -> List[GradeComponent]:
    """Правила шаблона, которые не выражаются в схеме одного компонента."""
    if not components:
        raise ValueError("components: нужен хотя бы один компонент")
    keys = [c.key for c in components]
    if len(set(keys)) != len(keys):
        raise ValueError("components: key должен быть уникален в шаблоне")
    if sum(c.is_exam for c in components) > 1:
        raise ValueError("components: экзамен (is_exam) не больше одного на шаблон")
    if abs(sum(c.weight for c in components) - 1) > WEIGHT_TOLERANCE:
        raise ValueError("components: сумма weight должна быть равна 1 (допуск 0.001)")
    return components


class GradeUserTemplateCreate(BaseModel):
    name: str
    components: List[GradeComponent]

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _strip_text(value, "название", 1, 60)

    @field_validator("components")
    @classmethod
    def _components(cls, value: List[GradeComponent]) -> List[GradeComponent]:
        return validate_components(value)


class GradeUserTemplatePatch(BaseModel):
    name: Optional[str] = None
    components: Optional[List[GradeComponent]] = None

    @model_validator(mode="after")
    def _no_nulls(self):
        for field in self.model_fields_set:
            if getattr(self, field) is None:
                raise ValueError(f"{field}: значение не может быть null")
        return self

    @field_validator("name")
    @classmethod
    def _name(cls, value: Optional[str]) -> Optional[str]:
        return value if value is None else _strip_text(value, "название", 1, 60)

    @field_validator("components")
    @classmethod
    def _components(cls, value: Optional[List[GradeComponent]]) -> Optional[List[GradeComponent]]:
        return value if value is None else validate_components(value)


# В ответах компоненты отдаются как лежат в БД: шаблоны предметов пишет парсер ПУД, и лишнее
# поле в нём не должно превращаться в 500.
class GradeUserTemplateSchema(BaseModel):
    id: UUID
    name: str
    components: List[Dict[str, Any]]
    created_at: datetime
    updated_at: datetime


class GradeSubjectTemplateSchema(BaseModel):
    id: UUID
    subject: str
    course: int
    program_id: Optional[UUID] = None
    academic_year: str
    source_url: Optional[str] = None
    rounding: Literal["half_up", "floor"]
    components: List[Dict[str, Any]]


GradeScoreValue = Optional[float]


class GradeScoresBody(BaseModel):
    # { "activity": 8, "labs": [9, 7.5, null], "exam": null }
    scores: Dict[str, Union[GradeScoreValue, List[GradeScoreValue]]] = Field(default_factory=dict)
    target: Optional[int] = Field(default=None, ge=1, le=10)

    @field_validator("scores")
    @classmethod
    def _range(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        for key, item in value.items():
            for score in item if isinstance(item, list) else [item]:
                if score is not None and not 0 <= score <= 10:
                    raise ValueError(f"scores.{key}: оценка должна быть от 0 до 10")
        return value


class GradeScoresSchema(BaseModel):
    scores: Dict[str, Any]
    target: Optional[int] = None

