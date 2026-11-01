"""
Token Harbor relay. Запускается как дочерний процесс main.py.
Слушает 127.0.0.1:RELAY_PORT (по умолчанию 8080), снаружи недоступен.
Стриминг — честный SSE через StreamingResponse.
"""
import os
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()

TOKENHARBOR_BASE = "https://tokenharbor.ai"
API_KEY = os.getenv("TOKENHARBOR_API_KEY", "").strip()

_EXCLUDED_HEADERS = {"content-encoding", "transfer-encoding", "connection", "content-length"}


@app.get("/health")
def health():
    return {"status": "ok", "key_configured": bool(API_KEY)}


@app.api_route(
    "/v1/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
)
async def proxy(path: str, request: Request):
    if not API_KEY:
        return JSONResponse(
            {"error": "TOKENHARBOR_API_KEY is not set in the environment"},
            status_code=500,
        )

    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "connection")
    }
    headers["Authorization"] = f"Bearer {API_KEY}"

    body = await request.body()

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=300.0, write=300.0, pool=300.0),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )

    try:
        req = client.build_request(
            method=request.method,
            url=f"{TOKENHARBOR_BASE}/v1/{path}",
            headers=headers,
            content=body,
        )
        upstream = await client.send(req, stream=True)
    except httpx.RequestError as exc:
        await client.aclose()
        return JSONResponse(
            {"error": "upstream unreachable", "detail": str(exc)},
            status_code=502,
        )

    resp_headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in _EXCLUDED_HEADERS
    }

    async def streamer():
        try:
            # aiter_bytes декомпрессит на нашей стороне; заголовок
            # content-encoding мы не пробрасываем, поэтому клиенту
            # уходит уже распакованный поток.
            async for chunk in upstream.aiter_bytes(chunk_size=4096):
                if chunk:
                    yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        streamer(),
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type=upstream.headers.get("content-type"),
    )