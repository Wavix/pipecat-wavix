"""
wavix_serializer.py

Single-file Wavix integration for Pipecat.

This file contains the Wavix websocket serializer used by Pipecat.

Inbound:
  websocket JSON -> base64 PCM -> InputAudioRawFrame

Outbound:
  OutputAudioRawFrame -> base64 PCM -> websocket JSON

Wavix media contract:
  - format: pcm16
  - sample rate: 24 kHz
  - bit depth: 16-bit
  - frame size: 20 ms
  - channels: mono
  - endianness: little-endian
"""

from __future__ import annotations

import base64
import json
from typing import Optional

from loguru import logger

from pipecat.audio.utils import create_stream_resampler
from pipecat.frames.frames import (
    AudioRawFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    OutputTransportMessageFrame,
    OutputTransportMessageUrgentFrame,
    StartFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer


class WavixFrameSerializer(FrameSerializer):
    """
    Serializer for Wavix websocket messages.

    Wire format:
    - JSON websocket messages
    - audio payload is base64-encoded PCM16
    - Wavix wire sample rate is 24 kHz
    - mono audio

    Inbound media:
        {
          "event": "media",
          "stream_id": "...",
          "media": {
            "payload": "<base64 pcm16>"
          }
        }

    Outbound media:
        same structure as above
    """

    WAVIX_SAMPLE_RATE: int = 24000
    NUM_CHANNELS: int = 1
    BYTES_PER_SAMPLE: int = 2
    FRAME_DURATION_MS: int = 20
    FRAME_BYTES: int = (
        WAVIX_SAMPLE_RATE * FRAME_DURATION_MS // 1000 * NUM_CHANNELS * BYTES_PER_SAMPLE
    )

    class InputParams(FrameSerializer.InputParams):
        """
        Serializer config.

        wavix_sample_rate:
            The websocket wire sample rate expected by Wavix.
        sample_rate:
            The internal Pipecat input sample rate.
            This is typically 16000 for STT + VAD.
        audio_track:
            Optional track filter for inbound media.
        """

        wavix_sample_rate: int = 24000
        sample_rate: Optional[int] = None
        audio_track: Optional[str] = "inbound"

    def __init__(
        self,
        stream_id: str,
        params: Optional["WavixFrameSerializer.InputParams"] = None,
    ) -> None:
        if params is None:
            params = WavixFrameSerializer.InputParams()

        super().__init__(params)

        self._stream_id = stream_id
        self._sample_rate = params.sample_rate
        self._wavix_sample_rate = params.wavix_sample_rate
        self._audio_track = params.audio_track

        # Audio is transmitted on the wire as little-endian PCM16 mono at 24 kHz.
        # We resample inbound to the pipeline rate and outbound back to the Wavix rate.
        self._input_resampler = create_stream_resampler()
        self._output_resampler = create_stream_resampler()

    async def setup(self, frame: StartFrame) -> None:
        """
        Initialize serializer sample rate after pipeline startup.

        If sample_rate is explicitly provided in params, use it.
        Otherwise use StartFrame.audio_in_sample_rate.
        """
        self._sample_rate = self._params.sample_rate or frame.audio_in_sample_rate

        logger.debug(
            "Serializer setup: internal_sample_rate={} wavix_sample_rate={}",
            self._sample_rate,
            self._wavix_sample_rate,
        )

    async def serialize(self, frame: Frame) -> str | bytes | None:
        """
        Convert Pipecat frames -> Wavix websocket messages.

        Outbound audio is normalized to Wavix's required format:
        PCM16, 24 kHz, mono, little-endian, 20 ms frames.
        """
        if isinstance(frame, StartFrame):
            await self.setup(frame)
            return None

        if isinstance(frame, EndFrame):
            return None

        if self._sample_rate is None:
            logger.warning("Sample rate not set up yet, cannot serialize audio frames.")
            return None

        if isinstance(frame, InterruptionFrame):
            return json.dumps({"event": "clear", "stream_id": self._stream_id})

        if isinstance(frame, AudioRawFrame):
            audio = frame.audio
            if not audio:
                return None

            if frame.sample_rate != self._wavix_sample_rate:
                audio = await self._output_resampler.resample(
                    audio,
                    frame.sample_rate,
                    self._wavix_sample_rate,
                )
                if not audio:
                    return None

            if frame.num_channels != self.NUM_CHANNELS:
                logger.warning(
                    "Outbound channel mismatch: got {}, expected {}",
                    frame.num_channels,
                    self.NUM_CHANNELS,
                )
                return None

            chunk = bytes(audio)
            if len(chunk) != self.FRAME_BYTES:
                logger.warning(
                    "Outbound frame size mismatch got={} expected={} frame_ms={}",
                    len(chunk),
                    self.FRAME_BYTES,
                    self.FRAME_DURATION_MS,
                )

            payload = base64.b64encode(chunk).decode("utf-8")
            if not payload:
                return None

            return json.dumps(
                {
                    "event": "media",
                    "stream_id": self._stream_id,
                    "media": {"payload": payload},
                }
            )

        if isinstance(frame, (OutputTransportMessageFrame, OutputTransportMessageUrgentFrame)):
            if self.should_ignore_frame(frame):
                return None
            return json.dumps(frame.message)

        return None

    async def deserialize(self, data: str | bytes) -> Frame | None:
        """
        Convert inbound Wavix websocket JSON -> Pipecat InputAudioRawFrame.

        Inbound Wavix audio arrives as 24 kHz PCM16 mono.
        If internal Pipecat input rate is 16 kHz, we resample here.
        """
        raw_message = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data

        try:
            message = json.loads(raw_message)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("[WavixFrameSerializer] Unparsable WebSocket message: {}", exc)
            return None

        event = message.get("event")

        if event == "media":
            media = message.get("media", {})
            track = media.get("track", "inbound")

            if self._audio_track and track != self._audio_track:
                return None

            payload_b64 = media.get("payload", "")
            if not payload_b64:
                return None

            try:
                pcm_bytes = base64.b64decode(payload_b64)
            except Exception as exc:
                logger.warning("[WavixFrameSerializer] base64 decode error: {}", exc)
                return None

            if not pcm_bytes:
                return None

            if len(pcm_bytes) != self.FRAME_BYTES:
                logger.warning(
                    "[WavixFrameSerializer] inbound frame size mismatch got={} expected={} frame_ms={}",
                    len(pcm_bytes),
                    self.FRAME_BYTES,
                    self.FRAME_DURATION_MS,
                )

            if self._sample_rate is None:
                logger.warning("[WavixFrameSerializer] Internal sample rate is not initialized")
                return None

            if self._wavix_sample_rate != self._sample_rate:
                pcm_bytes = await self._input_resampler.resample(
                    pcm_bytes,
                    self._wavix_sample_rate,
                    self._sample_rate,
                )

            if pcm_bytes is None or len(pcm_bytes) == 0:
                return None

            return InputAudioRawFrame(
                audio=pcm_bytes,
                num_channels=self.NUM_CHANNELS,
                sample_rate=self._sample_rate,
            )

        if event in {"connected", "start", "stop", "mark"}:
            return None

        logger.debug("[WavixFrameSerializer] Unknown event ignored: {!r}", event)
        return None
