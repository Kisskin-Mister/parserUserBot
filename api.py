from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response, status
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field

from database import Database
from parser_engine import AIClassifier, AI_SYSTEM_PROMPT, match_parser_config
from vacancy_pipeline.api import create_pipeline_router
from vacancy_pipeline.repository import PipelineRepository
from vacancy_pipeline.service import build_structured_judge_from_env

load_dotenv()

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
PROJECT_ROOT = Path(__file__).resolve().parent
TELEGRAM_ALLOWED_FILE_ROOTS = os.getenv(
    "TELEGRAM_ALLOWED_FILE_ROOTS",
    os.pathsep.join([str(PROJECT_ROOT / "resumes"), "/tmp"]),
)

app = FastAPI(title="parserUserBot API", version="2.0.0")
db = Database()
structured_judge = build_structured_judge_from_env()


def _pipeline_repository() -> PipelineRepository:
    if db.pool is None:
        raise HTTPException(status_code=503, detail="database is not initialized")
    return PipelineRepository(db.pool)


class ParserConfigIn(BaseModel):
    name: str = Field(min_length=1)
    enabled: bool = True
    mode: Literal["keyword", "ai"] = "keyword"
    source_chat_ids: list[int] = Field(default_factory=list)
    target_chat_id: int
    config: dict[str, Any] = Field(default_factory=dict)

    # Empty source_chat_ids means: listen to all non-private chats except excluded_chat_ids.


class ParserConfigPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    enabled: bool | None = None
    mode: Literal["keyword", "ai"] | None = None
    source_chat_ids: list[int] | None = None
    target_chat_id: int | None = None
    config: dict[str, Any] | None = None


class ValidateRequest(BaseModel):
    parser: ParserConfigIn
    message: str = ""


class TestMessageRequest(BaseModel):
    message: str


class TelegramMessageRequest(BaseModel):
    target: str = Field(min_length=1)
    text: str = Field(min_length=1)
    idempotency_key: str | None = Field(default=None, min_length=1)


class TelegramDocumentRequest(BaseModel):
    target: str = Field(min_length=1)
    file_path: str = Field(min_length=1)
    caption: str | None = None
    idempotency_key: str | None = Field(default=None, min_length=1)


def _validate_telegram_target(target: str) -> str:
    normalized = target.strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="target is required")
    if normalized.startswith("@") and len(normalized) > 1 and normalized[1:].replace("_", "").isalnum():
        return normalized
    numeric = normalized[1:] if normalized.startswith("-") else normalized
    if numeric.isdigit():
        return normalized
    raise HTTPException(status_code=400, detail="target must be @username or numeric chat id")


def _idempotency_key(kind: str, payload: dict[str, Any], provided: str | None) -> str:
    if provided and provided.strip():
        return provided.strip()
    raw = json.dumps({"kind": kind, **payload}, ensure_ascii=False, sort_keys=True)
    return f"auto:{kind}:{hashlib.sha256(raw.encode()).hexdigest()}"


def _allowed_file_roots() -> list[Path]:
    raw_parts: list[str] = []
    for chunk in TELEGRAM_ALLOWED_FILE_ROOTS.split(os.pathsep):
        raw_parts.extend(part.strip() for part in chunk.split(","))
    roots = []
    for part in raw_parts:
        if part:
            roots.append(Path(part).expanduser().resolve())
    return roots or [(PROJECT_ROOT / "resumes").resolve(), Path("/tmp").resolve()]


def _validate_allowed_file(file_path: str) -> Path:
    candidate = Path(file_path).expanduser().resolve()
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=400, detail="file_path does not exist or is not a file")
    for root in _allowed_file_roots():
        try:
            candidate.relative_to(root)
            return candidate
        except ValueError:
            continue
    raise HTTPException(status_code=400, detail="file_path is outside TELEGRAM_ALLOWED_FILE_ROOTS")


def _command_response(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "command_id": row["id"],
        "status": row.get("status"),
        "deduplicated": bool(row.get("deduplicated", False)),
    }


def _command_status_response(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "command_id": row["id"],
        "status": row.get("status"),
        "telegram_message_id": row.get("telegram_message_id"),
        "result": row.get("result"),
        "error": row.get("error"),
        "target": row.get("target"),
        "type": row.get("command_type"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "processed_at": row.get("processed_at"),
    }


async def require_admin(authorization: str | None = Header(default=None)) -> None:
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="ADMIN_TOKEN is not configured")
    expected = f"Bearer {ADMIN_TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid admin token")


def _ai_classifier() -> AIClassifier:
    return AIClassifier(base_url=OPENAI_BASE_URL, api_key=OPENAI_API_KEY, model=OPENAI_MODEL)


@app.on_event("startup")
async def startup() -> None:
    await db.init()


@app.on_event("shutdown")
async def shutdown() -> None:
    if db.pool:
        await db.pool.close()


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "service": "parserUserBot", "ai_configured": bool(OPENAI_API_KEY)}


@app.get("/parsers")
async def list_parsers(active_only: bool = False) -> Any:
    return jsonable_encoder(await db.list_parser_configs(active_only=active_only))


@app.post("/parsers", dependencies=[Depends(require_admin)], status_code=status.HTTP_201_CREATED)
async def create_parser(parser: ParserConfigIn) -> Any:
    return jsonable_encoder(await db.create_parser_config(parser.model_dump()))


@app.get("/parsers/{parser_id}")
async def get_parser(parser_id: int) -> Any:
    parser = await db.get_parser_config(parser_id)
    if not parser:
        raise HTTPException(status_code=404, detail="parser not found")
    return jsonable_encoder(parser)


@app.put("/parsers/{parser_id}", dependencies=[Depends(require_admin)])
async def put_parser(parser_id: int, parser: ParserConfigIn) -> Any:
    updated = await db.update_parser_config(parser_id, parser.model_dump())
    if not updated:
        raise HTTPException(status_code=404, detail="parser not found")
    return jsonable_encoder(updated)


@app.patch("/parsers/{parser_id}", dependencies=[Depends(require_admin)])
async def patch_parser(parser_id: int, patch: ParserConfigPatch) -> Any:
    updated = await db.update_parser_config(parser_id, patch.model_dump(exclude_unset=True))
    if not updated:
        raise HTTPException(status_code=404, detail="parser not found")
    return jsonable_encoder(updated)


@app.delete("/parsers/{parser_id}", dependencies=[Depends(require_admin)], status_code=status.HTTP_204_NO_CONTENT)
async def delete_parser(parser_id: int) -> None:
    deleted = await db.delete_parser_config(parser_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="parser not found")


@app.post("/parsers/{parser_id}/enable", dependencies=[Depends(require_admin)])
async def enable_parser(parser_id: int) -> Any:
    parser = await db.set_parser_enabled(parser_id, True)
    if not parser:
        raise HTTPException(status_code=404, detail="parser not found")
    return jsonable_encoder(parser)


@app.post("/parsers/{parser_id}/disable", dependencies=[Depends(require_admin)])
async def disable_parser(parser_id: int) -> Any:
    parser = await db.set_parser_enabled(parser_id, False)
    if not parser:
        raise HTTPException(status_code=404, detail="parser not found")
    return jsonable_encoder(parser)


@app.post("/parsers/validate", dependencies=[Depends(require_admin)])
async def validate_parser(request: ValidateRequest) -> Any:
    parser = request.parser.model_dump()
    result = await match_parser_config(request.message, parser, _ai_classifier())
    return {"valid": result.error is None, "matched": result.matched, "reason": result.reason, "error": result.error}


@app.post("/parsers/{parser_id}/test-message")
async def test_message(parser_id: int, request: TestMessageRequest) -> Any:
    parser = await db.get_parser_config(parser_id)
    if not parser:
        raise HTTPException(status_code=404, detail="parser not found")
    result = await match_parser_config(request.message, parser, _ai_classifier())
    return {"matched": result.matched, "reason": result.reason, "error": result.error}


@app.get("/events")
async def events(limit: int = Query(default=100, ge=1, le=500)) -> Any:
    return jsonable_encoder(await db.list_events(limit=limit))


@app.get("/ai/system-prompt")
async def ai_system_prompt() -> dict[str, str]:
    return {"system_prompt": AI_SYSTEM_PROMPT}



@app.post("/telegram/messages", dependencies=[Depends(require_admin)], status_code=status.HTTP_201_CREATED)
async def enqueue_telegram_message(request: TelegramMessageRequest, response: Response) -> Any:
    target = _validate_telegram_target(request.target)
    data = request.text
    dedupe_key = _idempotency_key("send_message", {"target": target, "text": data}, request.idempotency_key)
    row = await db.enqueue_command("send_message", target, data, dedupe_key)
    if row.get("deduplicated"):
        response.status_code = status.HTTP_200_OK
    return jsonable_encoder(_command_response(row))


@app.post("/telegram/documents", dependencies=[Depends(require_admin)], status_code=status.HTTP_201_CREATED)
async def enqueue_telegram_document(request: TelegramDocumentRequest, response: Response) -> Any:
    target = _validate_telegram_target(request.target)
    file_path = str(_validate_allowed_file(request.file_path))
    payload = {"file_path": file_path}
    if request.caption is not None:
        payload["caption"] = request.caption
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    dedupe_key = _idempotency_key("send_document", {"target": target, **payload}, request.idempotency_key)
    row = await db.enqueue_command("send_document", target, data, dedupe_key)
    if row.get("deduplicated"):
        response.status_code = status.HTTP_200_OK
    return jsonable_encoder(_command_response(row))


@app.get("/telegram/commands/{command_id}", dependencies=[Depends(require_admin)])
async def get_telegram_command(command_id: int) -> Any:
    row = await db.get_command(command_id)
    if not row:
        raise HTTPException(status_code=404, detail="command not found")
    return jsonable_encoder(_command_status_response(row))

app.include_router(
    create_pipeline_router(
        repository_factory=_pipeline_repository,
        judge_factory=lambda: structured_judge,
        admin_dependency=require_admin,
    )
)
