"""
server.py — FastAPI + ngrok server for inbound Wavix calls.

Flow:
1. Expose this app through ngrok.
2. Receive a Wavix inbound call webhook.
3. Answer the call over the Wavix REST API.
4. Start bidirectional media streaming to this app's WebSocket endpoint.
"""

import os
from typing import Any

from fastapi.concurrency import asynccontextmanager
import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from pipecat.runner.types import WebSocketRunnerArguments
from pyngrok import conf as ngrok_conf
from pyngrok import ngrok

from bot import bot

load_dotenv()

PORT = int(os.environ.get("PORT", 7860))
WAVIX_API_BASE = os.environ.get("WAVIX_API_BASE", "https://api.wavix.com")

# ── FastAPI app ────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("App startup complete")

    logger.info("Registered routes:")
    for route in app.routes:
        logger.info(
            "  path={} methods={}",
            getattr(route, "path", None),
            getattr(route, "methods", None),
        )

    yield  # <-- app runs here

    logger.info("App shutdown")


app = FastAPI(
    title="Wavix Pipecat Bot — Inbound",
    lifespan=lifespan,
)

app.state.public_url = None
app.state.ws_url = None
app.state.webhook_url = None

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
@app.middleware("http")
async def debug_request_path(request: Request, call_next):
    logger.info(
        "HTTP request received method={} url={} path={} root_path={}",
        request.method,
        str(request.url),
        request.scope.get("path"),
        request.scope.get("root_path"),
    )
    response = await call_next(request)
    logger.info(
        "HTTP response method={} path={} status={}",
        request.method,
        request.scope.get("path"),
        response.status_code,
    )
    return response

# ── HTTP endpoints ─────────────────────────────────────────────────────────────


@app.get("/health")
async def health_check():
    """Simple liveness probe used by load balancers / monitoring."""
    return {"status": "ok"}


@app.post("/wavix/inbound")
async def wavix_inbound_webhook(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Accept inbound Wavix call events and bootstrap streaming."""
    payload = await request.json()
    call_id = _extract_call_id(payload)
    event_type = _extract_event_type(payload)

    logger.info(
        "Inbound Wavix webhook received — event={!r} call_id={!r} payload={}",
        event_type,
        call_id,
        payload,
    )

    if not call_id:
        raise HTTPException(status_code=400, detail="Missing call_id in webhook")

    if event_type not in {"call_setup", "incoming_call", "ringing"}:
        return {"status": "ignored", "event": event_type, "call_id": call_id}

    ws_url = app.state.ws_url
    if not ws_url:
        raise HTTPException(
            status_code=500, detail="Server has no public WebSocket URL configured"
        )

    await answer_call(call_id)
    stream = await start_stream(call_id, ws_url)

    return {
        "status": "ok",
        "call_id": call_id,
        "stream_id": stream.get("stream_id"),
        "ws_url": ws_url,
    }


# ── WebSocket endpoint ─────────────────────────────────────────────────────────


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    Accept an inbound Wavix media-stream WebSocket.

    Once Wavix connects after the stream is started, hand the WebSocket to the
    bot entrypoint. The bot handles the initial telephony handshake itself.
    """
    
    await websocket.accept()
    logger.info("New inbound Wavix WebSocket connection")
    await bot(WebSocketRunnerArguments(websocket=websocket))


# ── ngrok helper ───────────────────────────────────────────────────────────────


def start_ngrok_tunnel(port: int) -> str:
    """
    Open an ngrok HTTP tunnel to *port* and return the public HTTPS URL.

    Set the ``NGROK_AUTH_TOKEN`` environment variable to use an authenticated
    ngrok session.
    """
    auth_token = os.environ.get("NGROK_AUTH_TOKEN")
    if auth_token:
        ngrok_conf.get_default().auth_token = auth_token
    else:
        logger.warning(
            "NGROK_AUTH_TOKEN is not set. "
            "The tunnel will run unauthenticated and may be limited."
        )

    tunnel = ngrok.connect(port, proto="http")
    public_url: str = tunnel.public_url

    # ngrok free tier may return http:// — normalise to https://.
    if public_url.startswith("http://"):
        public_url = "https://" + public_url[7:]

    return public_url


def _extract_call_id(payload: dict[str, Any]) -> str | None:
    return (
        payload.get("call_id")
        or payload.get("uuid")
        or payload.get("call", {}).get("id")
        or payload.get("data", {}).get("call_id")
        or payload.get("data", {}).get("uuid")
    )


def _extract_event_type(payload: dict[str, Any]) -> str | None:
    return (
        payload.get("event")
        or payload.get("event_type")
        or payload.get("type")
        or payload.get("data", {}).get("event")
    )


def _wavix_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {os.environ['WAVIX_API_KEY']}",
        "Content-Type": "application/json",
    }


async def _post_wavix(
    paths: list[str],
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        last_error: str | None = None

        for path in paths:
            response = await client.post(
                f"{WAVIX_API_BASE}{path}",
                headers=_wavix_headers(),
                json=json_body,
            )
            if response.status_code < 400:
                body = response.json() if response.content else {}
                logger.info(
                    "Wavix API success path={} status={} response={}",
                    path,
                    response.status_code,
                    body,
                )
                return body
            last_error = f"{path}: HTTP {response.status_code} {response.text}"
            logger.warning("Wavix API failure {}", last_error)

        raise HTTPException(status_code=502, detail=f"Wavix API request failed: {last_error}")


async def answer_call(call_id: str) -> dict[str, Any]:
    logger.info("Answering Wavix call {}", call_id)
    return await _post_wavix(
        paths=[f"/v1/calls/{call_id}/answer"],
        json_body={},
    )


async def start_stream(call_id: str, ws_url: str) -> dict[str, Any]:
    logger.info("Starting Wavix stream for call {} -> {}", call_id, ws_url)
    return await _post_wavix(
        paths=[f"/v1/calls/{call_id}/streams"],
        json_body={
            "stream_channel": "inbound",
            "stream_type": "twoway",
            "stream_url": ws_url,
        },
    )


# ── Entry point ────────────────────────────────────────────────────────────────



if __name__ == "__main__":
    public_url = start_ngrok_tunnel(PORT)
    ws_url = public_url.replace("https://", "wss://") + "/ws"
    webhook_url = public_url + "/wavix/inbound"
    app.state.public_url = public_url
    app.state.ws_url = ws_url
    app.state.webhook_url = webhook_url

    logger.info("=" * 64)
    logger.info("  Wavix Inbound Bot — ready")
    logger.info(f"  Local   : http://localhost:{PORT}")
    logger.info(f"  Public  : {public_url}")
    logger.info(f"  Webhook : {webhook_url}")
    logger.info(f"  WS URL  : {ws_url}")
    logger.info("")
    logger.info("  Configure Wavix inbound call webhook to:")
    logger.info(f"    {webhook_url}")
    logger.info("  The webhook answers the call, then starts media streaming to:")
    logger.info(f"    {ws_url}")
    logger.info("=" * 64)

    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
