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
from typing import Any

from loguru import logger
from pydantic import model_validator

from pipecat.frames.frames import (
    AudioRawFrame,
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    OutputTransportMessageFrame,
    OutputTransportMessageUrgentFrame,
)
from pipecat.processors.frame_processor import FrameProcessorSetup
from pipecat.serializers.base_serializer import FrameSerializer
from pipecat.utils.deprecation import warn_deprecated


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

        audio_in_sample_rate:
            Sample rate of the audio Wavix sends on the websocket.
            Defaults to 24000.
        audio_out_sample_rate:
            Sample rate of the audio sent to Wavix on the websocket.
            Defaults to 24000.
        wavix_sample_rate:
            Deprecated. Use audio_in_sample_rate and audio_out_sample_rate.
        sample_rate:
            Deprecated. Overrides the rate inbound audio is converted to.
            Use PipelineParams.audio_in_sample_rate instead, which is the
            rate the serializer converts inbound audio to by default.
        audio_track:
            Optional track filter for inbound media.
        """

        audio_in_sample_rate: int = 24000
        audio_out_sample_rate: int = 24000
        wavix_sample_rate: int | None = None
        sample_rate: int | None = None
        audio_track: str | None = "inbound"

        @model_validator(mode="before")
        @classmethod
        def _carry_deprecated_rates(cls, data: Any) -> Any:
            if not isinstance(data, dict):
                return data
            if data.get("wavix_sample_rate") is not None:
                warn_deprecated(
                    "`WavixFrameSerializer.InputParams.wavix_sample_rate` is deprecated. "
                    "Use `audio_in_sample_rate` and `audio_out_sample_rate` instead.",
                    stacklevel=4,
                )
                data.setdefault("audio_in_sample_rate", data["wavix_sample_rate"])
                data.setdefault("audio_out_sample_rate", data["wavix_sample_rate"])
            if data.get("sample_rate") is not None:
                warn_deprecated(
                    "`WavixFrameSerializer.InputParams.sample_rate` is deprecated. "
                    "Use `PipelineParams.audio_in_sample_rate` instead.",
                    stacklevel=4,
                )
            return data

    def __init__(
        self,
        stream_id: str,
        params: WavixFrameSerializer.InputParams | None = None,
    ) -> None:
        params = params or WavixFrameSerializer.InputParams()

        # The base serializer owns the resamplers: inbound audio is converted
        # from the Wavix wire rate to the pipeline input rate, and outbound
        # audio from the pipeline output rate to the Wavix wire rate.
        super().__init__(params)
        self._params: WavixFrameSerializer.InputParams = params

        self._stream_id = stream_id
        self._audio_track = params.audio_track

    async def setup(self, setup: FrameProcessorSetup) -> None:
        """
        Initialize the serializer when the transport is set up.

        Inbound audio is converted to the pipeline's input sample rate,
        unless the deprecated sample_rate param overrides it.
        """
        await super().setup(setup)

        if self._params.sample_rate:
            self._sample_rate = self._params.sample_rate

        logger.debug(
            "Serializer setup: internal_sample_rate={} wavix_in_sample_rate={} "
            "wavix_out_sample_rate={}",
            self._sample_rate,
            self.audio_in_sample_rate,
            self.audio_out_sample_rate,
        )

    async def serialize(self, frame: Frame) -> str | bytes | None:
        """
        Convert Pipecat frames -> Wavix websocket messages.

        Outbound audio is normalized to Wavix's required format:
        PCM16, 24 kHz, mono, little-endian, 20 ms frames.
        """
        if isinstance(frame, InterruptionFrame):
            # Drop the outbound audio still held by the resampler so it is not
            # played after the interruption.
            await self._reset_audio_out()
            return json.dumps({"event": "clear", "stream_id": self._stream_id})

        if isinstance(frame, AudioRawFrame):
            if not frame.audio:
                return None

            if frame.num_channels != self.NUM_CHANNELS:
                logger.warning(
                    "Outbound channel mismatch: got {}, expected {}",
                    frame.num_channels,
                    self.NUM_CHANNELS,
                )
                return None

            audio = await self._resample_audio_out(frame)
            if not audio:
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

            if not self._sample_rate:
                logger.warning("[WavixFrameSerializer] Internal sample rate is not initialized")
                return None

            pcm_bytes = await self._resample_audio_in(pcm_bytes)
            if not pcm_bytes:
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
