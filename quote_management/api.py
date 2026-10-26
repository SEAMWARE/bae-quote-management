import asyncio
import logging
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from http import HTTPStatus
from typing import Annotated

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import Settings
from .scheduler import run_scheduler
from .wiring import create_quote_service


class CoordinatorRequest(BaseModel):
    customerMessage: str
    customerIdRef: str

    @field_validator("customerMessage", "customerIdRef")
    @classmethod
    def required(cls, value):
        if not value.strip():
            raise ValueError("Field cannot be blank")
        return value


class QuoteRequest(BaseModel):
    customerMessage: str | None = None
    customerIdRef: str
    providerIdRef: str
    productOfferingId: str | None = None

    @field_validator("customerIdRef", "providerIdRef")
    @classmethod
    def required(cls, value):
        if not value.strip():
            raise ValueError("ID reference is required")
        return value


class TenderRequest(QuoteRequest):
    externalId: str

    @field_validator("externalId")
    @classmethod
    def external_required(cls, value):
        if not value.strip():
            raise ValueError("External ID cannot be empty")
        return value


def create_app(settings=None, transport=None):
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        headers = {"Accept": "application/json"}
        async with httpx.AsyncClient(timeout=settings.timeout, headers=headers, transport=transport) as client:
            app.state.service = create_quote_service(settings, client)
            task = asyncio.create_task(run_scheduler(app.state.service)) if settings.scheduler_enabled else None
            try:
                yield
            finally:
                if task:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

    app = FastAPI(title="Quote Management", version="0.1.0", lifespan=lifespan,
                  openapi_url="/api-docs", docs_url="/swagger-ui.html")
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True,
                       allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"], allow_headers=["*"])

    @app.middleware("http")
    async def correlation(request, call_next):
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
        return response

    def error(request, status, message, **extra):
        return JSONResponse(status_code=status, content={"timestamp": datetime.now().isoformat(), "status": status,
                            "error": HTTPStatus(status).phrase, "message": message, "path": request.url.path, **extra})

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        return error(request, exc.status_code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return error(request, 400, "Validation failed", errors={str(e["loc"][-1]): e["msg"] for e in exc.errors()})

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        logging.getLogger(__name__).exception("Unhandled request error")
        return error(request, 500, "An unexpected error occurred")

    @app.get("/actuator/health")
    @app.get("/actuator/health/liveness")
    @app.get("/actuator/health/readiness")
    async def health():
        return {"status": "UP"}

    prefix = "/quoteManagement"

    @app.get(prefix + "/listAllQuotes")
    async def list_all(request: Request):
        return await request.app.state.service.list()

    @app.get(prefix + "/quoteByUser/{userId}")
    async def by_user(userId: str, role: str, request: Request):
        return await request.app.state.service.list(userId, role, "tailored")

    @app.get(prefix + "/tendering/quotes/{userId}")
    async def tenders(userId: str, role: str, request: Request, externalId: str | None = None):
        return await request.app.state.service.list(userId, role, "tender", externalId)

    @app.get(prefix + "/tendering/coordinatorQuotes/{userId}")
    async def coordinators(userId: str, request: Request):
        return await request.app.state.service.list(userId, category="coordinator")

    @app.get(prefix + "/quoteById/{id}")
    async def by_id(id: str, request: Request):
        return await request.app.state.service.get(id)

    @app.post(prefix + "/createQuote", status_code=201)
    async def create(data: QuoteRequest, request: Request):
        return await request.app.state.service.create(data, "tailored")

    @app.post(prefix + "/tendering/createQuote", status_code=201)
    async def create_tender(data: TenderRequest, request: Request):
        # The legacy tender endpoint does not accept a product offering.
        data.productOfferingId = None
        return await request.app.state.service.create(data, "tender")

    @app.post(prefix + "/tendering/createCoordinatorQuote", status_code=201)
    async def create_coordinator(data: CoordinatorRequest, request: Request):
        return await request.app.state.service.create(data, "coordinator")

    @app.patch(prefix + "/updateQuoteStatus/{id}")
    async def status(id: str, statusValue: str, request: Request):
        return await request.app.state.service.status(id, statusValue)

    @app.patch(prefix + "/addNoteToQuote/{id}")
    async def note(id: str, userId: str, messageContent: str, request: Request):
        return await request.app.state.service.note(id, userId, messageContent)

    @app.patch(prefix + "/updateQuoteDate/{id}")
    async def date(id: str, date: str, dateType: str, request: Request):
        fields = {"requested": "requestedQuoteCompletionDate", "expected": "expectedQuoteCompletionDate",
                  "effective": "effectiveQuoteCompletionDate", "expectedfulfillment": "expectedFulfillmentStartDate"}
        dateType = dateType.lower()
        if dateType not in fields:
            raise HTTPException(400, "Invalid dateType. Valid values: requested, expected, effective, expectedFulfillment")
        try:
            parsed = datetime.strptime(date, "%d-%m-%Y")
        except ValueError as exc:
            raise HTTPException(400, "Invalid date format. Expected DD-MM-YYYY") from exc
        await request.app.state.service.get(id)
        return await request.app.state.service.patch(id, {fields[dateType]: parsed.isoformat() + "Z"})

    @app.patch(prefix + "/addAttachmentToQuote/{id}")
    async def attachment(id: str, request: Request, file: Annotated[UploadFile, File()], description: Annotated[str, Form()] = ""):
        return await request.app.state.service.attachment(id, file, description)

    @app.delete(prefix + "/removeAttachmentFromQuote/{id}")
    async def remove_attachment(id: str, request: Request):
        return await request.app.state.service.remove_attachment(id)

    @app.delete(prefix + "/quote/{id}", status_code=204)
    async def delete(id: str, request: Request):
        await request.app.state.service.delete(id)
        return Response(status_code=204)

    return app
