"""Speech-to-text providers."""

from __future__ import annotations

import asyncio
import io
import wave
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from voice_agent.logging_setup import get_logger

log = get_logger(__name__)

# Phrases Whisper is known to hallucinate on silence/noise in Arabic (subtitle credits).
HALLUCINATIONS = (
    "ترجمة نانسي قنقر",
    "اشتركوا في القناة",
    "اشترك في القناة",
    "شكرا على المشاهدة",
    "شكراً على المشاهدة",
    "موسيقى",
)


@dataclass(frozen=True)
class Transcription:
    text: str
    language: str | None = None
    duration: float = 0.0
    avg_logprob: float | None = None


class SpeechToTextProvider(ABC):
    @abstractmethod
    async def transcribe(self, audio: bytes, *, language: str) -> Transcription:
        """Transcribe a complete audio file (WAV/any ffmpeg-decodable format)."""

    async def warmup(self) -> None:  # noqa: B027 - optional hook
        """Load models ahead of the first call so the first answer is fast."""


class FasterWhisperSTT(SpeechToTextProvider):
    """Local, offline Arabic transcription with faster-whisper (CTranslate2)."""

    def __init__(
        self,
        model_size: str = "small",
        *,
        device: str = "cpu",
        compute_type: str = "int8",
        beam_size: int = 5,
        initial_prompt: str | None = None,
        concurrency: int = 1,
    ):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.beam_size = beam_size
        self.initial_prompt = initial_prompt or None
        self._model: Any = None
        self._load_lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(concurrency)

    async def warmup(self) -> None:
        await self._ensure_model()

    async def _ensure_model(self) -> Any:
        async with self._load_lock:
            if self._model is None:
                log.info("stt.loading_model", model=self.model_size, device=self.device)
                self._model = await asyncio.to_thread(self._load)
                log.info("stt.model_ready", model=self.model_size)
        return self._model

    def _load(self) -> Any:
        from faster_whisper import WhisperModel  # heavy import, only when used

        return WhisperModel(self.model_size, device=self.device, compute_type=self.compute_type)

    async def transcribe(self, audio: bytes, *, language: str) -> Transcription:
        model = await self._ensure_model()
        async with self._sem:
            return await asyncio.to_thread(self._run, model, audio, language)

    def _run(self, model: Any, audio: bytes, language: str) -> Transcription:
        segments, info = model.transcribe(
            decode_audio(audio),
            language=language,
            beam_size=self.beam_size,
            vad_filter=True,
            condition_on_previous_text=False,
            initial_prompt=self.initial_prompt,
        )
        kept, logprobs = [], []
        for seg in segments:
            if seg.no_speech_prob > 0.6 and seg.avg_logprob < -1.0:
                continue
            kept.append(seg.text.strip())
            logprobs.append(seg.avg_logprob)
        text = " ".join(t for t in kept if t).strip()
        if any(h in text for h in HALLUCINATIONS) and len(text.split()) <= 6:
            text = ""
        return Transcription(
            text=text,
            language=getattr(info, "language", language),
            duration=float(getattr(info, "duration", 0.0) or 0.0),
            avg_logprob=sum(logprobs) / len(logprobs) if logprobs else None,
        )


WHISPER_SAMPLE_RATE = 16000


def decode_audio(audio: bytes) -> Any:
    """Decode WAV (what Asterisk records) to 16 kHz mono float32 for Whisper.

    Decoding PCM WAV ourselves avoids PyAV/ffmpeg on the hot path (faster, and
    immune to PyAV API changes). Non-WAV input falls back to faster-whisper's
    own decoder.
    """
    import numpy as np

    try:
        with wave.open(io.BytesIO(audio), "rb") as wav:
            channels, width, rate = wav.getnchannels(), wav.getsampwidth(), wav.getframerate()
            frames = wav.readframes(wav.getnframes())
    except (wave.Error, EOFError):
        return io.BytesIO(audio)
    if width != 2:
        return io.BytesIO(audio)
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    if rate != WHISPER_SAMPLE_RATE and samples.size:
        duration = samples.size / rate
        target = np.linspace(0, duration, int(duration * WHISPER_SAMPLE_RATE), endpoint=False)
        source = np.arange(samples.size) / rate
        samples = np.interp(target, source, samples).astype(np.float32)
    return samples
