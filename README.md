# LiteLLM MongoDB Vector Search (BETA)

An optional HTTP sidecar for searching existing MongoDB Vector Search indexes through LiteLLM. It runs the official PyMongo driver separately from the LiteLLM SDK and proxy

LiteLLM generates query embeddings using your configured model, sends the vector to this service, and returns the same search results and chat sources. This service does not depend on LiteLLM or call an embedding or chat provider

## Requirements

Use MongoDB Atlas with Vector Search, or a self-managed deployment with MongoDB Search configured. A plain MongoDB server without the search process cannot execute Vector Search. Check MongoDB's [self-managed compatibility requirements](https://www.mongodb.com/docs/search/self-managed/current/deployment/compatibility-requirements/) for supported versions

Prepare your collection, documents, embeddings, and Vector Search index before connecting LiteLLM. The index's vector path and dimensions must match your stored vectors. Use the same embedding model for search queries

The MongoDB database user needs permission to run the search aggregation and list search indexes on the selected collections. Configure the Atlas IP access list or self-managed network rules for the sidecar host

## Deployment

Run one sidecar per MongoDB connection string. Several LiteLLM registrations can use different databases, collections, or indexes through that sidecar. Give its database user access only to the collections those registrations should search

Set `MONGODB_CONNECTION_STRING` and `MONGODB_SIDECAR_API_KEY` in a deployment secret store. The key authenticates LiteLLM to this service; the MongoDB URI stays in this service

```sh
docker run --rm --name mongodb-sidecar \
  -p 127.0.0.1:8080:8080 \
  -e MONGODB_CONNECTION_STRING \
  -e MONGODB_SIDECAR_API_KEY \
  ghcr.io/berriai/litellm-mongodb:v0.1.0-beta.1
```

The image supports Linux amd64 and arm64 and runs as user `10001:10001`. Pin the version or release digest in deployments. The image is optional; do not add it or PyMongo to ordinary LiteLLM installations

Each secret can instead be supplied through `MONGODB_CONNECTION_STRING_FILE` or `MONGODB_SIDECAR_API_KEY_FILE`. Mount the files read-only and make them readable by user 10001. Set either the value or its file variable, never both

MongoDB TLS settings remain in the connection string, including `tlsCAFile` and `tlsCertificateKeyFile`. Mount those files read-only at the paths used inside the container. Certificate verification stays enabled by default

Use a private container network for service-to-service communication. If the HTTP hop crosses an untrusted network, put the service behind an HTTPS reverse proxy. Do not expose its HTTP port publicly

For Docker Compose, add the service only when MongoDB is enabled:

```yaml
services:
  mongodb-sidecar:
    image: ghcr.io/berriai/litellm-mongodb:v0.1.0-beta.1
    environment:
      MONGODB_CONNECTION_STRING: ${MONGODB_CONNECTION_STRING:?required}
      MONGODB_SIDECAR_API_KEY: ${MONGODB_SIDECAR_API_KEY:?required}
    restart: unless-stopped
```

Connect your LiteLLM container to the same Compose network and use `http://mongodb-sidecar:8080`. A sidecar in the same Kubernetes Pod uses `http://127.0.0.1:8080`; use your chart's existing extra-container hooks and Kubernetes Secrets rather than adding a mandatory chart dependency

## Configure LiteLLM

Use the LiteLLM release containing the MongoDB sidecar adapter. The initial direct-driver configuration in `v1.101.0-rc.1` must be replaced with `api_base` and `api_key`; this integration remains BETA

```yaml
vector_store_registry:
  - vector_store_name: policies
    litellm_params:
      vector_store_id: policy_vector_index
      custom_llm_provider: mongodb
      api_base: http://mongodb-sidecar:8080
      api_key: os.environ/MONGODB_SIDECAR_API_KEY
      mongodb_database: knowledge
      mongodb_collection: policies
      mongodb_embedding_field: embedding
      mongodb_text_field: text
      litellm_embedding_model: text-embedding-3-small
```

The vector store ID is the exact MongoDB index name. `api_base` is the service origin or reverse-proxy path prefix; omit `/v1`. Configure the embedding-model alias in LiteLLM as usual

In the dashboard, open **Tools > Vector Stores > Manage Vector Stores > Add Vector Store**, choose **MongoDB (BETA)**, and enter the same settings. Saving a registration does not create a MongoDB index or ingest documents

Applications keep using LiteLLM's public search endpoint:

```sh
curl "$LITELLM_BASE_URL/v1/vector_stores/policy_vector_index/search" \
  -H "Authorization: Bearer $LITELLM_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"query":"What is the travel policy?","max_num_results":5}'
```

Direct SDK calls use `litellm.vector_stores.search` or `asearch`, with `custom_llm_provider="mongodb"` and the same settings. They need access to the sidecar and embedding provider but do not require a LiteLLM proxy or PyMongo

## API and behavior

The service provides `POST /v1/vector_stores/{index}/search` with bearer authentication. LiteLLM sends `query`, `query_vector`, `mongodb_database`, `mongodb_collection`, the field mappings, result/candidate limits, and `timeout_ms`. This is a search service, not a replacement for the full OpenAI vector-store API

Results use `object: "vector_store.search_results.page"`, `search_query`, and ordered `data` entries containing text, MongoDB IDs, and similarity scores. IDs are stringified. Text fields can use dotted paths. If every match lacks the text field, the request fails with an actionable error; partial sparse matches retain empty text

Queries must be nonblank and at most 32,000 characters. LiteLLM joins query lists with spaces. Result count defaults to 10 and accepts 1–50. Candidates default to `max(100, 10 * result_count)`; an override must be at least the result count and no more than 10,000

This BETA supports retrieval only. It does not support ingestion, collection/index creation, file uploads, filters, ranking options, query rewriting, or arbitrary aggregation pipelines

## Operations

`GET /health/liveness` checks that the HTTP process is running. `GET /health/readiness` performs a bounded MongoDB ping. Neither endpoint returns credentials. An outage makes readiness fail while the process remains available to recover

One asynchronous MongoDB client and its connection pool are reused per process. Pool size is bounded at 100 connections per server, connection/server-selection timeout at 10 seconds, and operation/pool-wait timeout at 30 seconds. A shorter LiteLLM read timeout reduces the search deadline. Disconnected HTTP requests cancel their in-flight work. Shutdown closes the client

Errors distinguish invalid configuration or database permissions (400), sidecar authentication (401), timeout (408), unavailable connections (503), and unexpected failures (500). Empty results are checked against index metadata so a missing or building index cannot look like a successful empty search. Driver errors are sanitized before they cross the HTTP boundary

## Development and releases

```sh
uv sync --frozen
uv run pytest -q
uv run ruff check .
uv run uvicorn litellm_mongodb.app:create_app --factory --host 127.0.0.1 --port 8080
```

CI tests Python 3.13 and 3.14 and builds both image architectures. A version tag runs tests, publishes the image to GHCR, attaches build provenance, and creates a GitHub prerelease. Maintainers must make a newly created GHCR package public before anonymous pulls work
