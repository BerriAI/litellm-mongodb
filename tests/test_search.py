from collections.abc import Mapping, Sequence
from typing import Final, Self
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from pymongo.errors import (
    ConfigurationError,
    ConnectionFailure,
    ExecutionTimeout,
    OperationFailure,
    ServerSelectionTimeoutError,
)

from litellm_mongodb.app import create_app
from litellm_mongodb.config import Settings
from litellm_mongodb.search import MongoSearch

SETTINGS: Final = Settings(SecretStr("mongodb://not-used"), SecretStr("test-sidecar-key"))
HEADERS: Final = {"Authorization": "Bearer test-sidecar-key"}
PAYLOAD: Final = {
    "query": "travel policy",
    "query_vector": [0.1, 0.2, 0.3],
    "mongodb_database": "policies",
    "mongodb_collection": "documents",
    "mongodb_text_field": "metadata.body",
    "mongodb_embedding_field": "stored_vector",
    "max_num_results": 2,
    "mongodb_num_candidates": 40,
}
DOCUMENTS: Final = ({"_id": 123, "metadata": {"body": "Use BLUE-42"}, "score": 0.9}, {"_id": "sparse", "score": 0.8})


class Cursor:
    def __init__(self, documents: Sequence[Mapping[str, object]]) -> None:
        self.documents: Final = documents

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def to_list(self, length: int) -> list[Mapping[str, object]]:
        return list(self.documents[:length])


@pytest.mark.parametrize(
    "key,overrides,status",
    [
        ("test-sidecar-key", {}, 200),
        ("wrong", {}, 401),
        ("", {}, 401),
        ("test-sidecar-key", {"query": " "}, 400),
        ("test-sidecar-key", {"query_vector": []}, 400),
        ("test-sidecar-key", {"query_vector": ["NaN"]}, 400),
        ("test-sidecar-key", {"max_num_results": 0}, 400),
        ("test-sidecar-key", {"max_num_results": 51}, 400),
        ("test-sidecar-key", {"mongodb_num_candidates": 1}, 400),
        ("test-sidecar-key", {"mongodb_num_candidates": 10_001}, 400),
        ("test-sidecar-key", {"pipeline": [{"$out": "other"}]}, 400),
        ("test-sidecar-key", {"mongodb_connection_string": "mongodb://secret"}, 400),
        ("test-sidecar-key", {"filters": {}}, 400),
    ],
)
def test_authenticated_api_preserves_pipeline_and_results(
    key: str, overrides: Mapping[str, object], status: int
) -> None:
    client: Final = MagicMock()
    collection: Final = client["policies"]["documents"]
    collection.aggregate = AsyncMock(return_value=Cursor(DOCUMENTS))
    with TestClient(create_app(SETTINGS, MongoSearch(client))) as http:
        response: Final = http.post(
            "/v1/vector_stores/policy_index/search",
            headers={"Authorization": f"Bearer {key}"},
            json={**PAYLOAD, **overrides},
        )
    assert response.status_code == status
    assert "mongodb://secret" not in response.text
    if status != 200:
        collection.aggregate.assert_not_called()
        return
    collection.aggregate.assert_awaited_once_with(
        [
            {
                "$vectorSearch": {
                    "index": "policy_index",
                    "path": "stored_vector",
                    "queryVector": [0.1, 0.2, 0.3],
                    "numCandidates": 40,
                    "limit": 2,
                }
            },
            {"$project": {"metadata.body": 1, "score": {"$meta": "vectorSearchScore"}}},
        ]
    )
    collection.list_search_indexes.assert_not_called()
    assert response.json() == {
        "object": "vector_store.search_results.page",
        "search_query": "travel policy",
        "data": [
            {"score": 0.9, "file_id": "123", "filename": "123", "content": [{"type": "text", "text": "Use BLUE-42"}]},
            {"score": 0.8, "file_id": "sparse", "filename": "sparse", "content": [{"type": "text", "text": ""}]},
        ],
    }


@pytest.mark.parametrize(
    "failure,status,code",
    [
        (OperationFailure("bad auth mongodb://secret", code=8000), 400, "mongodb_authentication"),
        (OperationFailure("denied mongodb://secret", code=13), 400, "mongodb_authentication"),
        (OperationFailure("wrong dimensions mongodb://secret"), 400, "mongodb_dimensions"),
        (OperationFailure("path is not indexed as vector mongodb://secret"), 400, "mongodb_vector_field"),
        (ServerSelectionTimeoutError("mongodb://secret"), 408, "mongodb_timeout"),
        (ExecutionTimeout("mongodb://secret"), 408, "mongodb_timeout"),
        (ConnectionFailure("mongodb://secret"), 503, "mongodb_unavailable"),
        (ConfigurationError("mongodb://secret"), 400, "mongodb_configuration"),
    ],
)
def test_search_errors_are_sanitized_and_next_request_recovers(failure: Exception, status: int, code: str) -> None:
    client: Final = MagicMock()
    collection: Final = client["policies"]["documents"]
    collection.aggregate = AsyncMock(side_effect=(failure, Cursor(DOCUMENTS)))
    with TestClient(create_app(SETTINGS, MongoSearch(client))) as http:
        response: Final = http.post("/v1/vector_stores/policy_index/search", headers=HEADERS, json=PAYLOAD)
        recovered: Final = http.post("/v1/vector_stores/policy_index/search", headers=HEADERS, json=PAYLOAD)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert "mongodb://secret" not in response.text
    assert recovered.status_code == 200
    assert recovered.json()["data"][0]["content"][0]["text"] == "Use BLUE-42"
