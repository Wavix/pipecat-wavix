"""
pipecat-wavix

Wavix WebSocket serializer for Pipecat.

This package provides utilities to convert between Pipecat audio frames
and the Wavix WebSocket media stream protocol.
"""

from .wavix_serializer import WavixFrameSerializer

__all__ = ["WavixFrameSerializer"]
__version__ = "1.0.0"