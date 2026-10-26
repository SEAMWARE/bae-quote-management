# BAE Quote Management

Python/FastAPI microservice for managing tailored quotes and tender processes with TMForum APIs.

TMForum remains the system of record. This service stores no local quotes and needs no database. It uses the existing Quote v4, Party v4, Product Catalog v4 and Document v4 services. Existing TMForum quote IDs and documents remain usable.

## Run

Requires Python 3.11 or newer.

Use `pip install -r requirements.txt` for runtime dependencies, or `pip install -r dev-requirements.txt` for runtime and test dependencies. Both match the dependency ranges in `pyproject.toml`.

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r dev-requirements.txt
cp .env.example .env
# Edit .env for your environment.
uvicorn main:app --host 0.0.0.0 --port 8080 --env-file .env
```

Docker Compose also loads `.env` automatically:

```sh
docker compose up --build
```

Inside a container, `localhost` refers to that container; configure reachable TMForum hostnames. See `.env.example` for all settings. Optional service URLs default to `TMFORUM_API_BASE_URL`. Set `NOTIFICATION_API_BASE_URL` to the existing charging backend to enable notifications. An optional `TMFORUM_API_TOKEN` authenticates outbound TMForum requests; incoming credentials are not forwarded.

Swagger: `/swagger-ui.html`; OpenAPI: `/api-docs`; health: `/actuator/health`, `/actuator/health/liveness`, `/actuator/health/readiness` (process health, not an upstream dependency check). Health endpoints use the application port 8080.

## Architecture

`api.py` exposes the public HTTP routes. `service.py` implements quote workflows, including coordinating document uploads with quote attachment references. `scheduler.py` invokes those workflows for deadline transitions.

External APIs have separate clients in `quote_management/clients/`:

* `quotes.py`: Quote API CRUD and pagination.
* `documents.py`: Document API creation, base64 encoding, and deletion.
* `parties.py`: organization retrieval and lookup.
* `catalog.py`: product offering retrieval.
* `notifications.py`: charging backend notification delivery.
* `transport.py`: shared TMForum authentication, HTTP requests, and error handling.

`wiring.py` constructs and injects these clients into `QuoteService`. The quote service coordinates business operations through the clients; it contains no external URLs or HTTP requests. The clients share the application's managed HTTP connection pool, while each owns its API endpoint and configured base URL.

## API

All routes use the `/quoteManagement` prefix and camelCase request fields and query parameters.

| Method | Route | Input |
|---|---|---|
| GET | `/listAllQuotes` | — |
| GET | `/quoteByUser/{userId}` | `role` |
| GET | `/tendering/quotes/{userId}` | `role`, optional `externalId` |
| GET | `/tendering/coordinatorQuotes/{userId}` | — |
| GET | `/quoteById/{id}` | — |
| POST | `/createQuote` | JSON: `customerIdRef`, `providerIdRef`, optional `customerMessage`, `productOfferingId` |
| POST | `/tendering/createQuote` | JSON: `customerIdRef`, `providerIdRef`, `externalId`, optional `customerMessage` |
| POST | `/tendering/createCoordinatorQuote` | JSON: `customerIdRef`, `customerMessage` |
| PATCH | `/updateQuoteStatus/{id}` | query: `statusValue` |
| PATCH | `/addNoteToQuote/{id}` | query: `userId`, `messageContent` |
| PATCH | `/updateQuoteDate/{id}` | query: `date` as DD-MM-YYYY, `dateType` |
| PATCH | `/addAttachmentToQuote/{id}` | multipart: `file`, optional `description` |
| DELETE | `/removeAttachmentFromQuote/{id}` | — |
| DELETE | `/quote/{id}` | — |

Creation returns 201, deletion 204, other successful operations 200. Categories are `tailored`, `tender`, `coordinator`. Tailored quotes use external ID `0000`. Tender quotes group by external ID; coordinator quote IDs link tender processes. Quote state changes update `quoteItem[].state`, preserving item data. Status values are passed through to TMForum. Date types are `requested`, `expected`, `effective`, `expectedFulfillment`; dates become midnight UTC.

Parties live on the quote: `Seller`, `SellerOperator`, `Buyer`, `BuyerOperator`. Buyer names come from Organization `externalReference[].name`; the buyer operator is resolved using `organizationIdentification.identificationId` matching `DID_IDENTIFIER`. Product offering SellerOperator takes precedence over the configured DID. Listing accepts `Buyer` and `Seller` case-insensitively, plus `Customer` as an alias for `Buyer`. Invalid roles return an empty array. Filtering is checked locally as well as sent upstream. Lists use offset pagination.

PDF uploads accept `application/pdf`, up to 10 MiB. Content is base64-encoded in a DocumentSpecification; quote attachments carry the resulting document ID in `content`. Replacement waits for persistence before deleting the old document. Failed persistence leaves documents in place for investigation; there is no unsafe rollback when the upstream write outcome is unknown. Removal clears references before deleting the document. Quote deletion does not delete documents.

The hourly scheduler cancels expired in-progress tailored quotes, advances coordinators from inProgress to approved and then accepted as their dates pass, and cancels pending tender items after the coordinator's fulfillment deadline. It appends SYSTEM notes and sends notifications where both parties exist. Run only one scheduler-enabled replica; set `SCHEDULER_ENABLED=false` on other replicas. Concurrent array edits require upstream concurrency control to avoid lost updates.

## Deployment considerations

Contract tests use a simulated TMForum server. Verify live integration, gateway authentication, and frontend workflows in staging before deployment.

* Upstream errors are returned as HTTP errors. Error bodies include `timestamp`, `status`, `error`, `message`, and `path`; validation errors use status 400.
* TMForum response fields are preserved, including extensions. Missing quote errors carry an error object.
* New-quote and conversation notifications use the configured charging backend. Direct status and upload notifications are disabled; conversation notes can trigger those notification types.
* Scheduler status and SYSTEM note changes are combined into one PATCH. The scheduler rechecks item state before writing; atomic coordination across replicas requires upstream conditional updates.
* Incoming authentication must be enforced by the gateway.

Configure the TMForum endpoint URLs, marketplace DID, and health probes for the deployment environment.

## Tests

```sh
# Full suite
python -m pytest
# Isolated unit tests
python -m pytest tests/unit
# API contract tests
python -m pytest tests/test_contract.py
```

Unit tests in `tests/unit/` use mocked dependencies and a fixed clock for deadline decisions. They cover quote creation and filtering, party resolution failures, note routing, preservation of nested data, attachment validation and replacement ordering, persistence retries, API authentication and error translation, document encoding, pagination, notification failures, and scheduled transitions including stale state and per-quote failures. Async unit tests use the standard library's `IsolatedAsyncioTestCase`; no additional test dependencies are needed.

Contract tests exercise the public API against a simulated TMForum backend, including creation payloads, pagination with ignored upstream filters, validation and conflicts, dates, attachment lifecycle, and scheduled transitions. Neither suite contacts live services.

## Continuous integration

`.github/workflows/tests.yml` runs all unit and contract tests on pull requests targeting `main` and on pushes to `main`, including merges. It uses Python 3.12 to match the Docker image, installs `dev-requirements.txt`, and runs `python -m pytest -q`. No service credentials or live TMForum backend are required.
