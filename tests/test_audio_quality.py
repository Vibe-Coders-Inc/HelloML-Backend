"""
Audio quality verification tests — μ-law encoding/decoding, resampling, SNR.
"""

import numpy as np
import base64
import pytest

import audioop


class TestUlawRoundtrip:

    def test_ulaw_encode_decode_preserves_quality(self):
        """μ-law roundtrip should preserve audio with acceptable SNR."""
        from api.audio_utils import ulaw_encode, ulaw_decode

        # Generate 1-second 8kHz sine wave (440 Hz)
        sr = 8000
        t = np.linspace(0, 1.0, sr, endpoint=False)
        signal = (np.sin(2 * np.pi * 440 * t) * 16000).astype(np.int16)
        pcm = signal.tobytes()

        ulaw = ulaw_encode(pcm)
        recovered_pcm = ulaw_decode(ulaw)

        original = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
        recovered = np.frombuffer(recovered_pcm, dtype=np.int16).astype(np.float64)

        assert len(original) == len(recovered)

        # SNR should be > 30 dB for μ-law (typically ~38 dB)
        noise = original - recovered
        signal_power = np.mean(original ** 2)
        noise_power = np.mean(noise ** 2)
        snr_db = 10 * np.log10(signal_power / max(noise_power, 1e-10))
        assert snr_db > 30, f"SNR too low: {snr_db:.1f} dB"

    def test_ulaw_silence(self):
        """Silence should survive μ-law roundtrip."""
        from api.audio_utils import ulaw_encode, ulaw_decode

        silence = b'\x00\x00' * 800  # 100ms at 8kHz
        encoded = ulaw_encode(silence)
        decoded = ulaw_decode(encoded)
        samples = np.frombuffer(decoded, dtype=np.int16)
        # All samples should be near zero
        assert np.max(np.abs(samples)) < 10


class TestResampling:

    def test_resample_8k_to_24k(self):
        """Resample 8kHz → 24kHz should produce 3x samples."""
        from api.audio_utils import resample_audio

        sr = 8000
        duration = 0.1  # 100ms
        n_samples = int(sr * duration)
        t = np.linspace(0, duration, n_samples, endpoint=False)
        signal = (np.sin(2 * np.pi * 440 * t) * 16000).astype(np.int16)
        pcm_8k = signal.tobytes()

        pcm_24k = resample_audio(pcm_8k, 8000, 24000)
        n_out = len(pcm_24k) // 2  # int16 = 2 bytes
        assert n_out == n_samples * 3

    def test_resample_24k_to_8k(self):
        """Resample 24kHz → 8kHz should produce 1/3 samples."""
        from api.audio_utils import resample_audio

        sr = 24000
        duration = 0.1
        n_samples = int(sr * duration)
        t = np.linspace(0, duration, n_samples, endpoint=False)
        signal = (np.sin(2 * np.pi * 440 * t) * 16000).astype(np.int16)
        pcm_24k = signal.tobytes()

        pcm_8k = resample_audio(pcm_24k, 24000, 8000)
        n_out = len(pcm_8k) // 2
        assert n_out == n_samples // 3

    def test_resample_same_rate_noop(self):
        """Same rate should return identical bytes."""
        from api.audio_utils import resample_audio

        data = b'\x00\x01' * 100
        assert resample_audio(data, 8000, 8000) == data

    def test_resample_snr(self):
        """Resampled audio should maintain high SNR (no artifacts)."""
        from api.audio_utils import resample_audio

        sr = 8000
        duration = 0.5
        n = int(sr * duration)
        t = np.linspace(0, duration, n, endpoint=False)
        freq = 440
        signal = (np.sin(2 * np.pi * freq * t) * 16000).astype(np.int16)

        # Roundtrip: 8k → 24k → 8k
        up = resample_audio(signal.tobytes(), 8000, 24000)
        down = resample_audio(up, 24000, 8000)
        recovered = np.frombuffer(down, dtype=np.int16).astype(np.float64)
        original = signal.astype(np.float64)

        # Trim to same length (edge effects)
        min_len = min(len(original), len(recovered))
        original = original[:min_len]
        recovered = recovered[:min_len]

        noise = original - recovered
        snr = 10 * np.log10(np.mean(original ** 2) / max(np.mean(noise ** 2), 1e-10))
        assert snr > 20, f"Roundtrip SNR too low: {snr:.1f} dB"

    def test_no_int16_overflow(self):
        """Resampling loud signal should not overflow int16."""
        from api.audio_utils import resample_audio

        # Max amplitude signal
        signal = np.full(800, 32767, dtype=np.int16)
        result = resample_audio(signal.tobytes(), 8000, 24000)
        samples = np.frombuffer(result, dtype=np.int16)
        assert np.all(samples >= -32768)
        assert np.all(samples <= 32767)


class TestHighLevelConversion:

    def test_twilio_to_openai_pipeline(self):
        """Full Twilio→OpenAI conversion should produce valid base64 PCM."""
        from api.audio_utils import twilio_to_openai

        # Create fake μ-law audio (160 bytes = 20ms at 8kHz)
        sr = 8000
        t = np.linspace(0, 0.02, 160, endpoint=False)
        signal = (np.sin(2 * np.pi * 440 * t) * 16000).astype(np.int16)
        ulaw = audioop.lin2ulaw(signal.tobytes(), 2)
        b64_in = base64.b64encode(ulaw).decode()

        b64_out = twilio_to_openai(b64_in)
        pcm_out = base64.b64decode(b64_out)

        # Should be 3x samples (8k→24k), each 2 bytes
        assert len(pcm_out) == 160 * 3 * 2

    def test_openai_to_twilio_pipeline(self):
        """Full OpenAI→Twilio conversion should produce valid base64 μ-law."""
        from api.audio_utils import openai_to_twilio

        sr = 24000
        t = np.linspace(0, 0.02, 480, endpoint=False)
        signal = (np.sin(2 * np.pi * 440 * t) * 16000).astype(np.int16)
        b64_in = base64.b64encode(signal.tobytes()).decode()

        b64_out = openai_to_twilio(b64_in)
        ulaw_out = base64.b64decode(b64_out)

        # 480 samples at 24kHz → 160 samples at 8kHz → 160 μ-law bytes
        assert len(ulaw_out) == 160


class TestChunking:

    def test_chunk_audio_20ms(self):
        """chunk_audio should split into correct 20ms chunks."""
        from api.audio_utils import chunk_audio

        # 100ms of 8kHz 16-bit audio = 800 samples = 1600 bytes
        data = b'\x00\x01' * 800
        chunks = chunk_audio(data, chunk_size_ms=20, sample_rate=8000, sample_width=2)
        # 100ms / 20ms = 5 chunks
        assert len(chunks) == 5
        assert all(len(c) == 320 for c in chunks)  # 160 samples * 2 bytes
