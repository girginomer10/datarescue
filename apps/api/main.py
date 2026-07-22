from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from apps.api.config import Settings
from apps.api.models import (
    CaseEvent,
    CaseSnapshot,
    DemoDriftRequest,
    DemoResetResponse,
    PolicyConfig,
    SchemaChangeEvent,
    SchemaChangeResponse,
    SchemaField,
    VerifyDeploymentRequest,
)
from apps.api.store import CaseNotFoundError
from apps.api.workflow import DEFAULT_ASSET_URN, AssetNotAllowedError, WorkflowService
from packages.datahub.actions import DataHubSchemaMCLWatcher, MCLActionResult


def create_app(
    settings: Settings | None = None, workflow: WorkflowService | None = None
) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.runtime_dir.mkdir(parents=True, exist_ok=True)
        app.state.workflow = workflow or WorkflowService(settings)
        yield

    app = FastAPI(
        title="DataRescue API",
        version="0.1.0",
        description="Evidence-gated runtime recovery for DataHub schema drift",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Last-Event-ID"],
    )

    def service(request: Request) -> WorkflowService:
        return cast(WorkflowService, request.app.state.workflow)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "mode": settings.execution_mode}

    @app.post(
        "/api/v1/events/schema-change",
        response_model=SchemaChangeResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def schema_change(
        event: SchemaChangeEvent, request: Request
    ) -> SchemaChangeResponse:
        try:
            return await run_in_threadpool(service(request).ingest, event)
        except AssetNotAllowedError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error

    @app.post(
        "/api/v1/events/datahub-mcl",
        response_model=MCLActionResult,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def datahub_mcl(
        payload: dict[str, object], request: Request
    ) -> MCLActionResult:
        watcher = DataHubSchemaMCLWatcher(service(request).ingest)
        result = await run_in_threadpool(watcher.handle, payload)
        if result.status.value == "FAILED":
            raise HTTPException(status_code=422, detail=result.reason)
        return result

    @app.get("/api/v1/cases", response_model=list[CaseSnapshot])
    async def cases(request: Request) -> list[CaseSnapshot]:
        return await run_in_threadpool(service(request).store.list_cases)

    @app.get("/api/v1/cases/{case_id}", response_model=CaseSnapshot)
    async def case(case_id: str, request: Request) -> CaseSnapshot:
        try:
            return await run_in_threadpool(service(request).store.get_case, case_id)
        except CaseNotFoundError as error:
            raise HTTPException(status_code=404, detail="Case not found") from error

    @app.get("/api/v1/cases/{case_id}/events")
    async def case_events(
        case_id: str,
        request: Request,
        after: int = Query(default=0, ge=0),
        follow: bool = Query(default=False),
    ) -> StreamingResponse:
        try:
            await run_in_threadpool(service(request).store.get_case, case_id)
        except CaseNotFoundError as error:
            raise HTTPException(status_code=404, detail="Case not found") from error

        async def stream() -> AsyncIterator[str]:
            cursor = after
            idle_ticks = 0
            while True:
                events = await run_in_threadpool(
                    service(request).store.events_for_case, case_id, cursor
                )
                for event in events:
                    cursor = event.sequence
                    yield _sse(event)
                if not follow:
                    break
                if await request.is_disconnected():
                    break
                idle_ticks += 1
                if idle_ticks % 20 == 0:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(0.75)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post(
        "/api/v1/cases/{case_id}/verify-deployment", response_model=CaseSnapshot
    )
    async def verify_deployment(
        case_id: str, verification: VerifyDeploymentRequest, request: Request
    ) -> CaseSnapshot:
        try:
            return await run_in_threadpool(
                service(request).verify_deployment, case_id, verification
            )
        except CaseNotFoundError as error:
            raise HTTPException(status_code=404, detail="Case not found") from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/api/v1/policy", response_model=PolicyConfig)
    async def policy(request: Request) -> PolicyConfig:
        return service(request).policy.config

    @app.post("/api/v1/demo/drift", response_model=SchemaChangeResponse)
    async def demo_drift(
        request: Request, demo: DemoDriftRequest | None = None
    ) -> SchemaChangeResponse:
        demo = demo or DemoDriftRequest()
        after = (
            [
                SchemaField(name="payment_id", data_type="bigint", nullable=False),
                SchemaField(name="gross_amount", data_type="numeric", nullable=False),
                SchemaField(name="net_amount", data_type="numeric", nullable=False),
            ]
            if demo.scenario == "safe-repair"
            else [
                SchemaField(name="payment_id", data_type="bigint", nullable=False),
                SchemaField(name="gross_amount", data_type="numeric", nullable=False),
                SchemaField(name="settlement_amount", data_type="numeric", nullable=False),
            ]
        )
        event = SchemaChangeEvent(
            entity_urn=DEFAULT_ASSET_URN,
            before_fields=[
                SchemaField(name="payment_id", data_type="bigint", nullable=False),
                SchemaField(name="amount", data_type="numeric", nullable=False),
            ],
            after_fields=after,
            source=f"RECORDED_REPLAY:{demo.scenario}",
        )
        return await run_in_threadpool(service(request).ingest, event)

    @app.post("/api/v1/demo/reset", response_model=DemoResetResponse)
    async def demo_reset(request: Request) -> DemoResetResponse:
        sequence = await run_in_threadpool(service(request).store.reset)
        return DemoResetResponse(
            reset_sequence=sequence,
            message="Demo reset appended; historical evidence was not deleted",
        )

    web_dist = settings.web_dist_dir.resolve()
    web_index = web_dist / "index.html"
    web_assets = web_dist / "assets"
    if web_index.is_file():
        if web_assets.is_dir():
            app.mount("/assets", StaticFiles(directory=web_assets), name="web-assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def production_spa(full_path: str) -> FileResponse:
            if full_path == "health" or full_path.startswith("api/"):
                raise HTTPException(status_code=404, detail="Not found")
            requested = (web_dist / full_path).resolve()
            if web_dist in requested.parents and requested.is_file():
                return FileResponse(requested)
            return FileResponse(web_index)

    return app


def _sse(event: CaseEvent) -> str:
    data = json.dumps(event.model_dump(mode="json"), separators=(",", ":"))
    return f"id: {event.sequence}\nevent: {event.event_type.value}\ndata: {data}\n\n"


app = create_app()
