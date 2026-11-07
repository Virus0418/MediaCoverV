from __future__ import annotations

from pathlib import Path

from fastapi import Body, FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from mediacovergenerator.jobs import JobManager
from mediacovergenerator.models import (
    AppConfig,
    DeleteRequest,
    GenerateRequest,
    HealthResponse,
    HistoryRecord,
    JobSummary,
    LibraryInfo,
    PreviewRequest,
    PreviewResponse,
    TextLayerResolveRequest,
    TextLayerStatus,
)
from mediacovergenerator.text_layers import (
    TextLayerMissingError,
    apply_preview_overrides,
    builtin_thumb_file,
    delete_text_layer,
    list_builtin_layers,
    list_text_layer_ids,
    resolve_text_layer,
    store_text_layer,
    text_layer_file,
)
from mediacovergenerator.scheduler import AppScheduler
from mediacovergenerator.storage import ConfigRepository, HistoryRepository, WebhookRepository
from mediacovergenerator.webhooks import EmbyWebhookManager


PROJECT_ROOT = Path(__file__).resolve().parent.parent
config_repository = ConfigRepository(PROJECT_ROOT)
history_repository = HistoryRepository(PROJECT_ROOT)
webhook_repository = WebhookRepository(PROJECT_ROOT)
job_manager = JobManager(PROJECT_ROOT, config_repository, history_repository)
scheduler = AppScheduler(PROJECT_ROOT, config_repository, job_manager)
webhook_manager = EmbyWebhookManager(PROJECT_ROOT, config_repository, job_manager)

app = FastAPI(title="MediaCoverGenerator", version="1.0.0")
app.mount(
    "/assets/images",
    StaticFiles(directory=PROJECT_ROOT / "mediacovergenerator" / "assets" / "images"),
    name="images",
)


@app.on_event("startup")
def on_startup() -> None:
    scheduler.start()


@app.on_event("shutdown")
def on_shutdown() -> None:
    scheduler.shutdown()


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(PROJECT_ROOT / "mediacovergenerator" / "web" / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    return FileResponse(
        PROJECT_ROOT / "mediacovergenerator" / "assets" / "images" / "favicon.svg",
        media_type="image/svg+xml",
    )


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    config = config_repository.load()
    from mediacovergenerator.emby import EmbyClient

    client = EmbyClient(config.emby)
    reachable = client.ping() if client.is_configured() else False
    return HealthResponse(
        status="ok" if reachable or not client.is_configured() else "degraded",
        configured=client.is_configured(),
        emby_reachable=reachable,
        active_jobs=job_manager.active_jobs(),
    )


@app.get("/config", response_model=AppConfig)
def get_config() -> AppConfig:
    return config_repository.load()


@app.put("/config", response_model=AppConfig)
def put_config(config: AppConfig) -> AppConfig:
    saved = config_repository.save(config)
    scheduler.reload()
    return saved


@app.get("/libraries", response_model=list[LibraryInfo])
def get_libraries() -> list[LibraryInfo]:
    config = config_repository.load()
    from mediacovergenerator.emby import EmbyClient

    client = EmbyClient(config.emby)
    if not client.is_configured():
        raise HTTPException(status_code=400, detail="Emby is not configured")
    try:
        return client.list_libraries()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/text-layers")
def get_text_layers() -> dict[str, list[str]]:
    config = config_repository.load()
    return {"library_ids": list_text_layer_ids(PROJECT_ROOT, config)}


@app.get("/text-layers/builtins")
def get_builtin_text_layers() -> dict[str, list[dict[str, str]]]:
    return {"layers": [{"name": name} for name in list_builtin_layers()]}


@app.get("/text-layers/builtins/{name}/thumb")
def get_builtin_text_layer_thumb(name: str) -> FileResponse:
    path = builtin_thumb_file(name)
    if path is None:
        raise HTTPException(status_code=404, detail="没有这张内置文字层")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@app.post("/text-layers/resolve", response_model=list[TextLayerStatus])
def resolve_text_layers(request: TextLayerResolveRequest) -> list[TextLayerStatus]:
    config = config_repository.load()
    statuses: list[TextLayerStatus] = []
    for item in request.items:
        try:
            found = resolve_text_layer(
                PROJECT_ROOT,
                config,
                item.library_id,
                item.library_name,
                explicit_builtin=item.builtin_layer,
            )
        except ValueError:
            found = None
        if found is None:
            statuses.append(TextLayerStatus(library_id=item.library_id, source="none", label="未匹配"))
        else:
            statuses.append(
                TextLayerStatus(
                    library_id=item.library_id,
                    source=found.source,
                    name=found.name,
                    label=found.label,
                )
            )
    return statuses


@app.get("/libraries/{library_id}/text-layer")
def get_text_layer(library_id: str) -> FileResponse:
    config = config_repository.load()
    try:
        path = text_layer_file(PROJECT_ROOT, config, library_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="还没有上传文字层")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-cache"})


@app.post("/libraries/{library_id}/text-layer")
async def upload_text_layer(library_id: str, file: UploadFile = File(...)) -> dict[str, object]:
    config = config_repository.load()
    raw = await file.read()
    try:
        path = store_text_layer(PROJECT_ROOT, config, library_id, raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"saved": True, "library_id": path.stem, "bytes": path.stat().st_size}


@app.delete("/libraries/{library_id}/text-layer")
def remove_text_layer(library_id: str) -> dict[str, bool]:
    config = config_repository.load()
    try:
        deleted = delete_text_layer(PROJECT_ROOT, config, library_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="还没有上传文字层")
    return {"deleted": True}


@app.post("/libraries/{library_id}/preview", response_model=PreviewResponse)
def preview_library(library_id: str, request: PreviewRequest | None = Body(default=None)) -> PreviewResponse:
    config = config_repository.load()
    try:
        config = apply_preview_overrides(config, library_id, request or PreviewRequest())
        from mediacovergenerator.service import LibraryUpdateService

        return LibraryUpdateService(PROJECT_ROOT, history_repository).preview_text_overlay(config, library_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TextLayerMissingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="找不到这个媒体库") from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/jobs/generate", response_model=JobSummary)
def generate(request: GenerateRequest | None = Body(default=None)) -> JobSummary:
    payload = request or GenerateRequest()
    return job_manager.start(payload.library_ids or None)


@app.post("/jobs/generate/{library_id}", response_model=JobSummary)
def generate_single(library_id: str) -> JobSummary:
    return job_manager.start([library_id], title="生成单个媒体库封面")


@app.get("/jobs", response_model=list[JobSummary])
def list_jobs() -> list[JobSummary]:
    return job_manager.list_jobs()


@app.post("/jobs/{job_id}/cancel", response_model=JobSummary)
def cancel_job(job_id: str) -> JobSummary:
    try:
        return job_manager.cancel(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc


@app.delete("/jobs/{job_id}")
def delete_job(job_id: str) -> dict[str, bool]:
    try:
        job_manager.delete(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"deleted": True}


@app.post("/jobs/delete")
def delete_jobs(request: DeleteRequest) -> dict[str, object]:
    deleted, blocked, missing = job_manager.delete_many(request.ids)
    return {
        "deleted": deleted,
        "blocked_ids": blocked,
        "missing_ids": missing,
    }


@app.get("/history", response_model=list[HistoryRecord])
def get_history(limit: int = 10) -> list[HistoryRecord]:
    return history_repository.list_recent(limit=limit)


@app.get("/history/{record_id}/image")
def get_history_image(record_id: str) -> FileResponse:
    record = history_repository.get(record_id)
    if not record or not record.saved_path:
        raise HTTPException(status_code=404, detail="History image not found")
    path = Path(record.saved_path)
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="History image file not found")
    return FileResponse(path)


@app.delete("/history/{record_id}")
def delete_history(record_id: str) -> dict[str, bool]:
    deleted = history_repository.delete(record_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="History record not found")
    return {"deleted": True}


@app.post("/history/delete")
def delete_history_many(request: DeleteRequest) -> dict[str, int]:
    return {"deleted": history_repository.delete_many(request.ids)}


@app.delete("/history")
def clear_history() -> dict[str, int]:
    return {"deleted": history_repository.clear()}


@app.get("/webhooks/last")
def get_last_webhook() -> dict[str, object]:
    return webhook_repository.load() or {}


@app.post("/webhooks/emby")
async def emby_webhook(
    request: Request,
    token: str | None = Query(default=None),
    x_webhook_token: str | None = Header(default=None),
) -> dict[str, object]:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

    effective_token = token or x_webhook_token
    webhook_repository.save(payload, token_provided=bool(effective_token))
    try:
        return webhook_manager.handle(payload, effective_token)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except LookupError as exc:
        return JSONResponse(
            status_code=202,
            content={
                "accepted": False,
                "scheduled": False,
                "reason": str(exc),
            },
        )
