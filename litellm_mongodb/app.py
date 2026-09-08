import asyncio
import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Annotated, Final, Protocol

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pymongo import AsyncMongoClient
from pymongo.errors import PyMongoError

from .config import Settings
from .models import SearchRequest, SearchResponse
from .search import MongoSearch, SearchFailure


class SearchBackend(Protocol):
    async def search(self, index: str, request: SearchRequest) -> SearchResponse | SearchFailure: ...
    async def ready(self) -> bool: ...


def error_response(error: SearchFailure) -> JSONResponse:
    return JSONResponse(
        status_code=error.status,
        content={"error": {"message": error.message, "type": "mongodb_error", "code": error.code}},
    )


async def search_until_disconnect(
    request: Request, backend: SearchBackend, index: str, body: SearchRequest
) -> SearchResponse | SearchFailure:
    async def disconnected() -> None:
        while True:
            if (await request.receive())["type"] == "http.disconnect":
                return

    search_task: Final = asyncio.create_task(backend.search(index, body))
    disconnect_task: Final = asyncio.create_task(disconnected())
    try:
        async with asyncio.timeout(body.timeout_ms / 1000):
            done, _ = await asyncio.wait((search_task, disconnect_task), return_when=asyncio.FIRST_COMPLETED)
            if search_task in done:
                return search_task.result()
            return SearchFailure(499, "client_disconnected", "The search client disconnected.")
    except TimeoutError:
        return SearchFailure(408, "mongodb_timeout", "The MongoDB vector search exceeded its deadline.")
    finally:
        for task in (search_task, disconnect_task):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


def create_app(settings: Settings | None = None, backend: SearchBackend | None = None) -> FastAPI:
    configuration: Final = settings if settings is not None else Settings.from_environment()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if backend is not None:
            app.state.search_backend = backend
            yield
            return
        try:
            client: Final = AsyncMongoClient[dict[str, object]](
                configuration.connection_string.get_secret_value(),
                appname="litellm-mongodb",
                connectTimeoutMS=configuration.connect_timeout_ms,
                serverSelectionTimeoutMS=configuration.connect_timeout_ms,
                timeoutMS=configuration.operation_timeout_ms,
                waitQueueTimeoutMS=configuration.operation_timeout_ms,
                maxPoolSize=configuration.max_pool_size,
                minPoolSize=0,
            )
        except (PyMongoError, ValueError, OSError):
            raise RuntimeError(
                "Cannot initialize MongoDB. Check the sidecar connection string and TLS files."
            ) from None
        app.state.search_backend = MongoSearch(client, configuration.operation_timeout_ms)
        try:
            yield
        finally:
            await client.close()

    app: Final = FastAPI(title="LiteLLM MongoDB Vector Search (BETA)", version="0.1.0b1", lifespan=lifespan)
    bearer: Final = HTTPBearer(auto_error=False)

    async def authenticate(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> None:
        from fastapi import HTTPException

        if credentials is None or not hmac.compare_digest(
            credentials.credentials.encode(), configuration.api_key.get_secret_value().encode()
        ):
            raise HTTPException(
                status_code=401, detail="Invalid MongoDB sidecar API key", headers={"WWW-Authenticate": "Bearer"}
            )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, error: RequestValidationError) -> JSONResponse:
        fields: Final = ", ".join(".".join(str(part) for part in item["loc"]) for item in error.errors())
        return error_response(SearchFailure(400, "invalid_request", f"Invalid MongoDB search request fields: {fields}"))

    @app.get("/health/liveness")
    async def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/readiness")
    async def readiness(request: Request) -> JSONResponse:
        active_backend: Final[SearchBackend] = request.app.state.search_backend
        try:
            async with asyncio.timeout(2):
                ready: Final = await active_backend.ready()
        except TimeoutError:
            return JSONResponse(status_code=503, content={"status": "unavailable"})
        return JSONResponse(status_code=200 if ready else 503, content={"status": "ok" if ready else "unavailable"})

    @app.post("/v1/vector_stores/{index}/search", dependencies=[Depends(authenticate)], response_model=SearchResponse)
    async def search(index: str, body: SearchRequest, request: Request) -> SearchResponse | JSONResponse:
        active_backend: Final[SearchBackend] = request.app.state.search_backend
        result: Final = await search_until_disconnect(request, active_backend, index, body)
        return error_response(result) if isinstance(result, SearchFailure) else result

    return app
