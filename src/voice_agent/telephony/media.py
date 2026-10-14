"""Turns synthesized speech into a media URI Asterisk can play.

Two delivery modes:

* ``file`` (recommended): audio is written as raw signed-linear (``.sln16``) into a
  directory Asterisk can read (same host, bind mount or network share). It is
  played with ``sound:<absolute path without extension>``, which ARI supports on
  every Asterisk version.
* ``http`` (experimental): audio is written as 8 kHz WAV and served by the API at
  ``/media/<name>.wav``; Asterisk fetches it via ``res_http_media_cache``. ARI
  support for ``sound:http://...`` varies between Asterisk versions; test first.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path

from voice_agent.speech.tts import SynthesizedAudio, TextToSpeechProvider

SAFE_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")


class MediaPublisher:
    def __init__(
        self,
        *,
        mode: str,
        sounds_dir: str | Path,
        uri_prefix: str = "",
        public_base_url: str = "",
    ):
        if mode not in ("file", "http"):
            raise ValueError("ASTERISK_PLAYBACK_MODE must be 'file' or 'http'")
        self.mode = mode
        self.sounds_dir = Path(sounds_dir)
        self.uri_prefix = uri_prefix.rstrip("/")
        self.public_base_url = public_base_url.rstrip("/")
        self.sounds_dir.mkdir(parents=True, exist_ok=True)

    @property
    def sample_rate(self) -> int:
        return 16000 if self.mode == "file" else 8000

    @property
    def extension(self) -> str:
        return ".sln16" if self.mode == "file" else ".wav"

    def path_for(self, name: str) -> Path:
        if not SAFE_NAME.match(name):
            raise ValueError(f"unsafe media name: {name!r}")
        return self.sounds_dir / f"{name}{self.extension}"

    def uri_for(self, name: str) -> str:
        if self.mode == "file":
            return f"sound:{self.uri_prefix}/{name}"
        return f"sound:{self.public_base_url}/media/{name}.wav"

    def exists(self, name: str) -> bool:
        return self.path_for(name).exists()

    def write(self, name: str, audio: SynthesizedAudio) -> str:
        path = self.path_for(name)
        data = audio.pcm if self.mode == "file" else audio.to_wav()
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        os.chmod(tmp, 0o644)  # Asterisk runs as a different user
        tmp.replace(path)  # atomic: Asterisk never sees a half-written file
        return self.uri_for(name)

    def prune(self, max_age_seconds: float = 7 * 24 * 3600) -> int:
        cutoff = time.time() - max_age_seconds
        removed = 0
        for path in self.sounds_dir.glob(f"*{self.extension}"):
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        return removed


class SpeechRenderer:
    """TTS + caching + publishing: text in, playable media URI out."""

    def __init__(self, tts: TextToSpeechProvider, publisher: MediaPublisher):
        self.tts = tts
        self.publisher = publisher

    def media_name(self, text: str) -> str:
        digest = hashlib.sha256(
            f"{self.tts.cache_namespace}|{self.publisher.sample_rate}|{text}".encode()
        ).hexdigest()
        return f"vm-{digest[:32]}"

    async def render(self, text: str) -> str:
        name = self.media_name(text)
        if self.publisher.exists(name):
            return self.publisher.uri_for(name)
        audio = await self.tts.synthesize(text, sample_rate=self.publisher.sample_rate)
        return self.publisher.write(name, audio)
