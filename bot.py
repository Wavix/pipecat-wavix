"""
bot.py — Inbound call pipeline for pipecat-ngrok-bot.

This module contains the Pipecat pipeline that handles an inbound phone call.
It is invoked by server.py once a WebSocket connection is accepted and the
initial "start" event has been parsed.

Pipeline flow
─────────────
  WebSocket input
      │
      ▼
  Deepgram STT  (speech → text)
      │
      ▼
  User context aggregator
      │
      ▼
  OpenAI LLM   (text → text)
      │
      ▼
  Cartesia TTS  (text → speech)
      │
      ▼
  WebSocket output  (via WavixFrameSerializer: PCM16 → base64 JSON)
      │
      ▼
  Assistant context aggregator
"""

import json
import logging
import os
from typing import Optional

from dotenv import load_dotenv
from fastapi import WebSocket
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import EndFrame, LLMContextFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.services.cartesia.tts import CartesiaTTSService, CartesiaTTSSettings
from pipecat.services.deepgram.stt import DeepgramSTTService, DeepgramSTTSettings
from pipecat.services.openai.base_llm import OpenAILLMSettings
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from pipecat.transports.base_transport import BaseTransport
from pipecat.runner.types import RunnerArguments

from wavix_serializer import WavixFrameSerializer
load_dotenv()

# Suppress noisy third-party loggers that clutter the console during a call.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("deepgram").setLevel(logging.WARNING)

WAVIX_SAMPLE_RATE = 24000
BOT_SAMPLE_RATE = 16000
DEFAULT_DEEPGRAM_MODEL = "nova-2-phonecall"
DEFAULT_CARTESIA_VOICE_ID = "79a125e8-cd45-4c13-8a67-188112f4dd22"

async def _parse_stream_id(websocket: WebSocket) -> tuple[str, dict]:
    """
    Parse the initial Wavix "start" event to extract the stream ID and call data.
    Adapted from parse_telephony_websocket in pipecat/runner/utils.py
    
    The stream ID is required to initialize the WavixFrameSerializer, which is
    needed to decode incoming audio frames from the WebSocket.
    call_id is not strictly required for the bot pipeline but is useful for logging and debugging.

    Parameters
    ----------
    websocket:
        An *accepted* FastAPI WebSocket connection for this call.
    """
    # Read first two messages
    message_stream = websocket.iter_text()
    first_message = {}
    second_message = {}

    try:
        # First message - required
        first_message_raw = await message_stream.__anext__()
        logger.trace(f"First message: {first_message_raw}")
        first_message = json.loads(first_message_raw) if first_message_raw else {}
    except json.JSONDecodeError:
        pass
    except StopAsyncIteration:
        raise ValueError("WebSocket closed before receiving telephony handshake messages")

    try:
        # Second message - optional, some providers may only send one
        second_message_raw = await message_stream.__anext__()
        logger.trace(f"Second message: {second_message_raw}")
        second_message = json.loads(second_message_raw) if second_message_raw else {}
    except json.JSONDecodeError:
        pass
    except StopAsyncIteration:
        logger.warning("Only received one WebSocket message, expected two")

    try:
        # Try auto-detection on both messages
        is_wavix_first_message = _check_wavix_transport(first_message)
        is_wavix_second_message = _check_wavix_transport(second_message)

        if is_wavix_first_message:
            transport_type = "wavix"
            call_data_raw = first_message
            logger.debug(f"Detected transport: {transport_type} (from first message)")
        elif is_wavix_second_message:
            transport_type = "wavix"
            call_data_raw = second_message
            logger.debug(f"Detected transport: {transport_type} (from second message)")
        else:
            transport_type = "unknown"
            call_data_raw = second_message
            logger.warning("Could not auto-detect transport type")

        # Extract provider-specific data
        if transport_type == "wavix":
            stream_id = call_data_raw.get("stream_id")
            call_id = call_data_raw.get("call_id")

            if not stream_id or not call_id:
                logger.warning("Invalid Wavix start payload: missing stream_id or call_id")
                return "unknown", {}

            call_data = {
                "stream_id": stream_id,
                "call_id": call_id,
            }
        else:
            call_data = {}

        logger.debug(f"Parsed - Type: {transport_type}, Data: {call_data}")
        return transport_type, call_data

    except Exception as e:
        logger.error(f"Error parsing telephony WebSocket: {e}")
        raise

def _check_wavix_transport(message_data: dict) -> bool:
    """
     Attempt to detect transport type from WebSocket message structure 
     by checking for Wavix-specific fields in the payload.
    
     Detection is based on the fields in the "start" event payload.
     Returns a string identifier for the transport type, or "unknown" if detection fails.
     This is a best-effort heuristic and may not be 100% reliable, but can help with auto-configuration and logging.
     """

    # Wavix detection
    if (
        message_data.get("event") == "start"
        and "stream_id" in message_data
        and "call_id" in message_data
    ):
        logger.trace("Auto-detected: Wavix")
        return True

    # Failed to detect known transport type 
    return False

async def bot(runner_args: RunnerArguments):
    """Main bot entry point compatible with Pipecat Cloud."""

    _, call_data = await _parse_stream_id(runner_args.websocket)

    # ── Serializer ─────────────────────────────────────────────────────────────
    serializer = WavixFrameSerializer(
        stream_id=call_data["stream_id"],
        params=WavixFrameSerializer.InputParams(
            wavix_sample_rate=WAVIX_SAMPLE_RATE,
            sample_rate=BOT_SAMPLE_RATE,
            audio_track="inbound",
        ),
    )    

    # ── Transport ──────────────────────────────────────────────────────────────
    transport = FastAPIWebsocketTransport(
        websocket=runner_args.websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_in_passthrough=True,
            audio_in_sample_rate=BOT_SAMPLE_RATE,
            audio_out_enabled=True,
            audio_out_sample_rate=WAVIX_SAMPLE_RATE,
            add_wav_header=False,
            serializer=serializer,
            audio_out_10ms_chunks=2,
            fixed_audio_packet_size=960,
        ),
    )

    handle_sigint = runner_args.handle_sigint

    await run_bot(transport, handle_sigint)

async def run_bot(
    transport: BaseTransport,
    handle_sigint: bool):
    """
    Build and run the inbound-call Pipecat pipeline.
    """

    # ── AI services ────────────────────────────────────────────────────────────
    llm = OpenAILLMService(
        api_key=os.environ["OPENAI_API_KEY"],
        settings=OpenAILLMSettings(model=os.environ.get("OPENAI_MODEL", "gpt-4o")),
    )

    stt = DeepgramSTTService(
        api_key=os.environ["DEEPGRAM_API_KEY"],
        encoding="linear16",
        sample_rate=BOT_SAMPLE_RATE,
        channels=1,
        settings=DeepgramSTTSettings(
            model=os.environ.get("DEEPGRAM_MODEL", DEFAULT_DEEPGRAM_MODEL),
            language="en-US",
            interim_results=True,
            punctuate=True,
            smart_format=True,
        ),
    )

    tts = CartesiaTTSService(
        api_key=os.environ["CARTESIA_API_KEY"],
        settings=CartesiaTTSSettings(
            voice=os.environ.get(
                "CARTESIA_VOICE_ID", DEFAULT_CARTESIA_VOICE_ID
            )
        ),
        sample_rate=WAVIX_SAMPLE_RATE,
    )

    # ── Conversation context ────────────────────────────────────────────────────
    context = LLMContext()
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
        ),
    )

    # ── Pipeline ───────────────────────────────────────────────────────────────
    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            llm,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=BOT_SAMPLE_RATE,
            audio_out_sample_rate=WAVIX_SAMPLE_RATE,
            allow_interruptions=True,
            enable_metrics=True,
            report_only_initial_ttfb=True,
        ),
    )

    # ── Event handlers ─────────────────────────────────────────────────────────

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info(f"Inbound call connected")
        # Trigger an initial LLM response so the bot greets the caller.
        context.add_message({"role": "user", "content": "Please introduce yourself to the user."})
        await task.queue_frames([LLMContextFrame(context)])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info(f"Inbound call disconnected")
        await task.queue_frames([EndFrame()])

    # ── Run ────────────────────────────────────────────────────────────────────
    # `force_gc=True` to force garbage collection after
    # the runner finishes running a task which could be useful for long running
    # applications with multiple clients connecting.
    runner = PipelineRunner(handle_sigint=handle_sigint, force_gc=True)
    await runner.run(task)

if __name__ == "__main__":
    from pipecat.runner.run import main, TELEPHONY_TRANSPORTS
    TELEPHONY_TRANSPORTS.append("wavix")

    main()