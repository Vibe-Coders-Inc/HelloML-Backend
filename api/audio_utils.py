"""
Audio conversion utilities for Twilio Media Streams ↔ OpenAI Realtime API.

Twilio uses μ-law encoded audio at 8kHz.
OpenAI Realtime API uses PCM16 audio at 24kHz (or g711_ulaw at 8kHz natively).

When the OpenAI session is configured with g711_ulaw, no resampling is needed —
raw μ-law bytes pass straight through. When PCM 24kHz is used, we resample with
scipy's polyphase filter (resample_poly) which is designed for streaming chunks,
unlike the old FFT-based resample() which caused ringing artifacts on small buffers.
"""

import audioop
import base64
from math import gcd
from scipy.signal import resample_poly
import numpy as np


# ---------------------------------------------------------------------------
# g711 μ-law pass-through helpers (no resampling, no PCM conversion)
# ---------------------------------------------------------------------------

def twilio_to_openai_passthrough(twilio_audio_base64: str) -> str:
    """Pass raw μ-law bytes directly (when OpenAI session uses g711_ulaw)."""
    return twilio_audio_base64  # already base64 μ-law — no conversion needed


def openai_to_twilio_passthrough(openai_audio_base64: str) -> str:
    """Pass raw μ-law bytes directly (when OpenAI session uses g711_ulaw)."""
    return openai_audio_base64  # already base64 μ-law — no conversion needed


# ---------------------------------------------------------------------------
# PCM / μ-law conversion helpers
# ---------------------------------------------------------------------------

def ulaw_decode(ulaw_data: bytes) -> bytes:
    """Decode μ-law audio to linear PCM (16-bit)."""
    return audioop.ulaw2lin(ulaw_data, 2)


def ulaw_encode(pcm_data: bytes) -> bytes:
    """Encode linear PCM (16-bit) to μ-law audio."""
    return audioop.lin2ulaw(pcm_data, 2)


# ---------------------------------------------------------------------------
# Resampling — polyphase filter (safe for small streaming chunks)
# ---------------------------------------------------------------------------

def resample_audio(pcm_data: bytes, from_rate: int, to_rate: int) -> bytes:
    """
    Resample PCM audio using a polyphase filter (resample_poly).

    Unlike FFT-based scipy.signal.resample, resample_poly uses a FIR
    polyphase filter that handles small chunks (~20 ms from Twilio)
    without ringing artifacts.
    """
    if from_rate == to_rate:
        return pcm_data

    audio_array = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float64)

    # Compute minimal up/down factors
    g = gcd(from_rate, to_rate)
    up = to_rate // g    # e.g. 3 for 8k→24k
    down = from_rate // g  # e.g. 1 for 8k→24k

    resampled = resample_poly(audio_array, up, down)

    # Clip to int16 range to avoid overflow
    resampled = np.clip(resampled, -32768, 32767).astype(np.int16)
    return resampled.tobytes()


# ---------------------------------------------------------------------------
# High-level Twilio ↔ OpenAI converters (PCM path)
# ---------------------------------------------------------------------------

def twilio_to_openai(twilio_audio_base64: str, target_rate: int = 24000) -> str:
    """
    Convert Twilio's μ-law 8kHz audio to OpenAI's PCM16 format.
    """
    ulaw_data = base64.b64decode(twilio_audio_base64)
    pcm_8khz = ulaw_decode(ulaw_data)
    pcm_target = resample_audio(pcm_8khz, from_rate=8000, to_rate=target_rate)
    return base64.b64encode(pcm_target).decode('utf-8')


def openai_to_twilio(openai_audio_base64: str, source_rate: int = 24000) -> str:
    """
    Convert OpenAI's PCM16 audio to Twilio's μ-law 8kHz format.
    """
    pcm_source = base64.b64decode(openai_audio_base64)
    pcm_8khz = resample_audio(pcm_source, from_rate=source_rate, to_rate=8000)
    ulaw_data = ulaw_encode(pcm_8khz)
    return base64.b64encode(ulaw_data).decode('utf-8')


def chunk_audio(audio_data: bytes, chunk_size_ms: int = 20, sample_rate: int = 8000, sample_width: int = 2) -> list[bytes]:
    """
    Split audio into chunks of specified duration.

    Args:
        audio_data: Audio bytes to chunk
        chunk_size_ms: Chunk duration in milliseconds
        sample_rate: Sample rate in Hz
        sample_width: Sample width in bytes (2 for 16-bit)

    Returns:
        List of audio chunks
    """
    # Calculate bytes per chunk
    bytes_per_sample = sample_width
    samples_per_chunk = int(sample_rate * chunk_size_ms / 1000)
    bytes_per_chunk = samples_per_chunk * bytes_per_sample

    # Split into chunks
    chunks = []
    for i in range(0, len(audio_data), bytes_per_chunk):
        chunk = audio_data[i:i + bytes_per_chunk]
        if len(chunk) == bytes_per_chunk:  # Only include full chunks
            chunks.append(chunk)

    return chunks
