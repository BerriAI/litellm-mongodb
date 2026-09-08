from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, field_validator, model_validator


class SearchRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    query: str = Field(min_length=1, max_length=32_000)
    query_vector: tuple[FiniteFloat, ...] = Field(min_length=1)
    mongodb_database: str = Field(min_length=1)
    mongodb_collection: str = Field(min_length=1)
    mongodb_text_field: str = Field(default="text", min_length=1)
    mongodb_embedding_field: str = Field(default="embedding", min_length=1)
    max_num_results: int = Field(default=10, ge=1, le=50)
    mongodb_num_candidates: int | None = Field(default=None, ge=1, le=10_000)
    timeout_ms: Annotated[int, Field(gt=0)] = 30_000

    @field_validator("query_vector", mode="before")
    @classmethod
    def vector_sequence(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("query")
    @classmethod
    def nonblank_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be empty")
        return value

    @model_validator(mode="after")
    def valid_candidates(self) -> Self:
        if self.mongodb_num_candidates is not None and self.mongodb_num_candidates < self.max_num_results:
            raise ValueError("mongodb_num_candidates must be at least max_num_results")
        return self

    @property
    def candidates(self) -> int:
        return (
            self.mongodb_num_candidates
            if self.mongodb_num_candidates is not None
            else max(100, 10 * self.max_num_results)
        )


class Content(BaseModel):
    model_config = ConfigDict(frozen=True)
    type: Literal["text"] = "text"
    text: str


class SearchResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    score: FiniteFloat | None
    content: tuple[Content, ...]
    file_id: str | None
    filename: str | None


class SearchResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    object: Literal["vector_store.search_results.page"] = "vector_store.search_results.page"
    search_query: str
    data: tuple[SearchResult, ...]
