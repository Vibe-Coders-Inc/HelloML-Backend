"""
Audio conversion utilities for Twilio Media Streams ↔ OpenAI Realtime API.

Twilio uses μ-law encoded audio at 8kHz.
OpenAI Realtime API uses PCM16 audio at 24kHz (or 16kHz depending on config).
"""

import audioop
import base64
from scipy import signal
import numpy as np


def ulaw_decode(ulaw_data: bytes) -> bytes:
    """
    Decode μ-law audio to linear PCM.

    Args:
        ulaw_data: μ-law encoded audio bytes

    Returns:
        Linear PCM audio bytes (16-bit)
    """
    return audioop.ulaw2lin(ulaw_data, 2)  # 2 = 16-bit samples


def ulaw_encode(pcm_data: bytes) -> bytes:
    """
    Encode linear PCM to μ-law audio.

    Args:
        pcm_data: Linear PCM audio bytes (16-bit)

    Returns:
        μ-law encoded audio bytes
    """
    return audioop.lin2ulaw(pcm_data, 2)  # 2 = 16-bit samples


def resample_audio(pcm_data: bytes, from_rate: int, to_rate: int) -> bytes:
    """
    Resample PCM audio from one sample rate to another.

    Args:
        pcm_data: Linear PCM audio bytes (16-bit, little-endian)
        from_rate: Source sample rate (Hz)
        to_rate: Target sample rate (Hz)

    Returns:
        Resampled PCM audio bytes
    """
    if from_rate == to_rate:
        return pcm_data

    # Convert bytes to numpy array of 16-bit integers
    audio_array = np.frombuffer(pcm_data, dtype=np.int16)

    # Calculate number of samples needed
    num_samples = int(len(audio_array) * to_rate / from_rate)

    # Resample using scipy
    resampled = signal.resample(audio_array, num_samples)

    # Convert back to 16-bit integers and then to bytes
    resampled_int16 = resampled.astype(np.int16)
    return resampled_int16.tobytes()


def twilio_to_openai(twilio_audio_base64: str, target_rate: int = 24000) -> str:
    """
    Convert Twilio's μ-law 8kHz audio to OpenAI's PCM16 format.

    Args:
        twilio_audio_base64: Base64-encoded μ-law audio from Twilio
        target_rate: Target sample rate for OpenAI (default: 24000 Hz)

    Returns:
        Base64-encoded PCM16 audio for OpenAI
    """
    # Decode base64
    ulaw_data = base64.b64decode(twilio_audio_base64)

    # Decode μ-law to linear PCM
    pcm_8khz = ulaw_decode(ulaw_data)

    # Resample from 8kHz to target rate (24kHz for OpenAI)
    pcm_target = resample_audio(pcm_8khz, from_rate=8000, to_rate=target_rate)

    # Encode to base64
    return base64.b64encode(pcm_target).decode('utf-8')


def openai_to_twilio(openai_audio_base64: str, source_rate: int = 24000) -> str:
    """
    Convert OpenAI's PCM16 audio to Twilio's μ-law 8kHz format.

    Args:
        openai_audio_base64: Base64-encoded PCM16 audio from OpenAI
        source_rate: Source sample rate from OpenAI (default: 24000 Hz)

    Returns:
        Base64-encoded μ-law audio for Twilio
    """
    # Decode base64
    pcm_source = base64.b64decode(openai_audio_base64)

    # Resample from source rate to 8kHz
    pcm_8khz = resample_audio(pcm_source, from_rate=source_rate, to_rate=8000)

    # Encode to μ-law
    ulaw_data = ulaw_encode(pcm_8khz)

    # Encode to base64
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
