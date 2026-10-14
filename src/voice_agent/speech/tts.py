"""Text-to-speech providers.

ElevenLabs API (checked Oct 2026):
``POST {base}/v1/text-to-speech/{voice_id}?output_format=pcm_16000`` with the
``xi-api-key`` header and JSON body ``{"text", "model_id"}``. ``pcm_*`` output is
raw signed 16-bit little-endian mono, which Asterisk plays natively as ``.sln16``
(16 kHz) or ``.sln`` (8 kHz) and transcodes to the call's codec.
"""

from __future__ import annotations

import io
import wave
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

from voice_agent.logging_setup import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class SynthesizedAudio:
    pcm: bytes
    """Signed 16-bit little-endian mono PCM."""
    sample_rate: int

    def to_wav(self) -> bytes:
        return pcm_to_wav(self.pcm, self.sample_rate)


class TTSError(RuntimeError):
    pass


class TextToSpeechProvider(ABC):
    @abstractmethod
    async def synthesize(self, text: str, *, sample_rate: int = 16000) -> SynthesizedAudio: ...

    @property
    def cache_namespace(self) -> str:
        """Identifies voice/model so cached audio is invalidated when they change."""
        return type(self).__name__

    async def aclose(self) -> None:  # noqa: B027 - optional hook
        pass


class ElevenLabsTTS(TextToSpeechProvider):
    SUPPORTED_RATES = (8000, 16000, 22050, 24000)

    def __init__(
        self,
        *,
        api_key: str,
        voice_id: str,
        model_id: str = "eleven_multilingual_v2",
        base_url: str = "https://api.elevenlabs.io",
        timeout_seconds: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not api_key:
            raise ValueError("ELEVENLABS_API_KEY is required")
        self.voice_id = voice_id
        self.model_id = model_id
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"xi-api-key": api_key},
            timeout=httpx.Timeout(timeout_seconds, connect=5.0),
            transport=transport,
        )

    @property
    def cache_namespace(self) -> str:
        return f"elevenlabs:{self.voice_id}:{self.model_id}"

    async def synthesize(self, text: str, *, sample_rate: int = 16000) -> SynthesizedAudio:
        if sample_rate not in self.SUPPORTED_RATES:
            raise ValueError(f"Unsupported sample rate {sample_rate}")
        text = text.strip()
        if not text:
            raise ValueError("Cannot synthesize empty text")
        last_error: Exception | None = None
        for attempt in (1, 2):  # one quick retry; the caller is waiting
            try:
                response = await self._client.post(
                    f"/v1/text-to-speech/{self.voice_id}",
                    params={"output_format": f"pcm_{sample_rate}"},
                    json={"text": text, "model_id": self.model_id},
                )
            except httpx.TransportError as exc:
                last_error = exc
                log.warning("tts.transport_error", attempt=attempt, error=repr(exc))
                continue
            if response.status_code == 200:
                return SynthesizedAudio(pcm=response.content, sample_rate=sample_rate)
            last_error = TTSError(f"ElevenLabs HTTP {response.status_code}: {response.text[:300]}")
            if response.status_code < 500 and response.status_code != 429:
                break
            log.warning("tts.http_error", attempt=attempt, status=response.status_code)
        raise TTSError(f"ElevenLabs synthesis failed: {last_error!r}") from last_error

    async def aclose(self) -> None:
        await self._client.aclose()


def pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buf.getvalue()
