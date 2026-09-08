from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from pymongo import AsyncMongoClient, timeout
from pymongo.errors import ConfigurationError, ConnectionFailure, OperationFailure, PyMongoError

from .models import Content, SearchRequest, SearchResponse, SearchResult


@dataclass(frozen=True, slots=True)
class SearchFailure:
    status: int
    code: str
    message: str


def translate_error(error: Exception) -> SearchFailure:
    if isinstance(error, PyMongoError) and error.timeout:
        return SearchFailure(
            408,
            "mongodb_timeout",
            "MongoDB did not respond before the search deadline. Check connectivity, the Atlas "
            "IP access list, and index readiness.",
        )
    if isinstance(error, ConnectionFailure):
        return SearchFailure(
            503,
            "mongodb_unavailable",
            "The MongoDB connection was dropped or refused. Check connectivity and TLS configuration, then retry.",
        )
    if isinstance(error, OperationFailure):
        detail: Final = str(error).lower()
        if error.code in (13, 18) or any(
            marker in detail for marker in ("bad auth", "authentication failed", "not authorized")
        ):
            return SearchFailure(
                400,
                "mongodb_authentication",
                "MongoDB rejected the sidecar credentials or the database user lacks read access to the collection.",
            )
        if "dimension" in detail:
            return SearchFailure(
                400,
                "mongodb_dimensions",
                "The query embedding does not match the index dimensions. "
                "litellm_embedding_model must match the model that produced the stored vectors.",
            )
        if "is not indexed as vector" in detail:
            return SearchFailure(
                400,
                "mongodb_vector_field",
                "mongodb_embedding_field must match the vector path in the MongoDB Vector Search index.",
            )
        if "index" in detail and any(marker in detail for marker in ("not found", "does not exist", "unknown")):
            return SearchFailure(
                400,
                "mongodb_index_missing",
                "No queryable MongoDB Vector Search index was found. Check the exact index "
                "name, database, collection, and index readiness.",
            )
        return SearchFailure(
            400,
            "mongodb_query_rejected",
            "MongoDB rejected the vector search. Check the database, collection, index definition, and vector field.",
        )
    if isinstance(error, ConfigurationError):
        if any(marker in str(error).lower() for marker in ("resolution lifetime expired", "dns operation timed out")):
            return SearchFailure(
                408,
                "mongodb_dns_timeout",
                "The MongoDB hostname lookup timed out. Check DNS connectivity from the sidecar.",
            )
    if isinstance(error, (ConfigurationError, ValueError, OSError)):
        return SearchFailure(
            400,
            "mongodb_configuration",
            "Check MONGODB_CONNECTION_STRING in the sidecar, percent-encoded credentials, DNS, and mounted TLS files.",
        )
    return SearchFailure(500, "mongodb_internal_error", "The MongoDB search could not be completed.")


def field_value(document: Mapping[str, object], dotted_path: str) -> str | None:
    head, _, rest = dotted_path.partition(".")
    value: Final = document.get(head)
    if not rest:
        return None if value is None else str(value)
    return field_value(value, rest) if isinstance(value, Mapping) else None


def search_result(document: Mapping[str, object], text_field: str) -> SearchResult:
    identifier: Final = document.get("_id")
    document_id: Final = None if identifier is None else str(identifier)
    score: Final = document.get("score")
    return SearchResult(
        score=float(score) if isinstance(score, (int, float)) else None,
        content=(Content(text=field_value(document, text_field) or ""),),
        file_id=document_id,
        filename=document_id,
    )


class MongoSearch:
    def __init__(self, client: AsyncMongoClient[dict[str, object]], operation_timeout_ms: int = 30_000) -> None:
        self.client: Final = client
        self.operation_timeout_ms: Final = operation_timeout_ms

    async def ready(self) -> bool:
        try:
            with timeout(2):
                await self.client.admin.command("ping")
            return True
        except (PyMongoError, OSError, ValueError):
            return False

    async def search(self, index: str, request: SearchRequest) -> SearchResponse | SearchFailure:
        pipeline: Final = [
            {
                "$vectorSearch": {
                    "index": index,
                    "path": request.mongodb_embedding_field,
                    "queryVector": list(request.query_vector),
                    "numCandidates": request.candidates,
                    "limit": request.max_num_results,
                }
            },
            {"$project": {request.mongodb_text_field: 1, "score": {"$meta": "vectorSearchScore"}}},
        ]
        try:
            target: Final = self.client[request.mongodb_database][request.mongodb_collection]
            with timeout(min(request.timeout_ms, self.operation_timeout_ms) / 1000):
                async with await target.aggregate(pipeline) as cursor:
                    documents: Final = await cursor.to_list(length=request.max_num_results)
                if not documents:
                    async with await target.list_search_indexes(index) as catalogue_cursor:
                        catalogue: Final = await catalogue_cursor.to_list(length=1)
                    if not catalogue:
                        return SearchFailure(
                            400,
                            "mongodb_index_missing",
                            "No MongoDB Vector Search index was found on this collection. The "
                            "vector store ID must be the exact index name.",
                        )
                    if not catalogue[0].get("queryable"):
                        return SearchFailure(
                            400,
                            "mongodb_index_not_ready",
                            "The MongoDB Vector Search index is not queryable yet. Wait for the index build to finish.",
                        )
                if documents and all(
                    field_value(document, request.mongodb_text_field) is None for document in documents
                ):
                    return SearchFailure(
                        400,
                        "mongodb_text_field",
                        "None of the matched documents has the configured text field. Set "
                        "mongodb_text_field to the readable text field, including a dotted path when needed.",
                    )
                return SearchResponse(
                    search_query=request.query,
                    data=tuple(search_result(document, request.mongodb_text_field) for document in documents),
                )
        except (PyMongoError, OSError, ValueError) as error:
            return translate_error(error)
