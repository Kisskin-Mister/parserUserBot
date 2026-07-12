from __future__ import annotations

import os
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field, field_validator

from database import Database
from parser_engine import AIClassifier, AI_SYSTEM_PROMPT, match_parser_config

load_dotenv()

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

app = FastAPI(title="parserUserBot API", version="2.0.0")
db = Database()


class ParserConfigIn(BaseModel):
    name: str = Field(min_length=1)
    enabled: bool = True
    mode: Literal["keyword", "ai"] = "keyword"
    source_chat_ids: list[int] = Field(default_factory=list)
    target_chat_id: int
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source_chat_ids")
    @classmethod
    def source_chats_required(cls, value: list[int]) -> list[int]:
        if not value:
            raise ValueError("source_chat_ids must contain at least one chat id")
        return value


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
