"""Application configuration.

All configuration comes from environment variables (optionally loaded from a local
``.env`` file that is git-ignored). Secrets never live in source files.

Each process role (api, worker, ari, simulate) validates only the variables it
needs, and reports *all* missing variables at once with a clear message.
"""

from __future__ import annotations

import os
import re
from enum import StrEnum
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PERSON_ID_ENV = re.compile(r"^MONDAY_([A-Z0-9_]+)_PERSON_ID$")
REQUESTER_ID_ENV = re.compile(r"^MONDAY_([A-Z0-9_]+)_REQUESTER_ID$")


class Role(StrEnum):
    API = "api"
    WORKER = "worker"
    ARI = "ari"
    SIMULATE = "simulate"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""

    def __init__(self, role: str, problems: list[str]):
        self.problems = problems
        lines = "\n".join(f"  - {p}" for p in problems)
        super().__init__(
            f"Invalid configuration for '{role}':\n{lines}\n"
            "Set these in your environment or in a local .env file "
            "(copy .env.example to .env). Never put secrets in source files."
        )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    # --- Asterisk ARI -------------------------------------------------------
    asterisk_ari_url: str = ""
    asterisk_ari_username: str = ""
    asterisk_ari_password: str = ""
    asterisk_stasis_app: str = "voice-monday-agent"
    asterisk_test_extension: str = "777"
    # Where the app writes generated audio, and how Asterisk refers to that folder.
    asterisk_playback_mode: Literal["file", "http"] = "file"
    asterisk_sounds_dir: str = "data/sounds"
    asterisk_sounds_uri_prefix: str = "voice-monday"
    public_media_base_url: str = ""
    record_max_seconds: int = Field(default=20, ge=3, le=120)
    record_max_silence_seconds: int = Field(default=2, ge=1, le=10)
    call_inactivity_timeout_seconds: int = Field(default=60, ge=10, le=3600)
    call_max_consecutive_failures: int = Field(default=3, ge=1, le=10)
    call_preparation_timeout_seconds: float = Field(default=8.0, gt=0, le=30)

    # --- ElevenLabs ---------------------------------------------------------
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = "cgSgspJ2msm6clMCkdW9"
    elevenlabs_model_id: str = "eleven_multilingual_v2"
    elevenlabs_base_url: str = "https://api.elevenlabs.io"
    elevenlabs_timeout_seconds: float = 15.0

    # --- Monday.com ---------------------------------------------------------
    monday_mode: Literal["mock", "live"] = "mock"
    monday_api_token: str = ""
    monday_api_url: str = "https://api.monday.com/v2"
    monday_api_version: str = "2026-07"
    monday_board_id: str = ""
    monday_group_id: str = ""
    monday_task_name_column: str = "name"
    monday_assignee_column: str = ""
    monday_assignee_column_type: Literal["people", "dropdown"] = "people"
    monday_idempotency_column: str = ""
    monday_requester_column: str = ""
    monday_timeout_seconds: float = 10.0
    mock_monday_fail_times: int = Field(default=0, ge=0)

    # --- Infrastructure -----------------------------------------------------
    redis_url: str = "redis://redis:6379/0"
    database_url: str = "sqlite:///data/agent.db"
    queue_name: str = "voice-agent"

    # --- Speech-to-text -----------------------------------------------------
    whisper_model: str = "small"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    whisper_beam_size: int = 5
    whisper_initial_prompt: str = (
        "حطي لمحسن تاسك ييجي بعد بكرة، وقولي لميكل ييجي بكرة. "
        "محسن هو محمد. مايكل. أحمد. آه صح. لا غلط، عدّلي أول تاسك."
    )
    default_language: str = "ar"

    # --- Jobs ---------------------------------------------------------------
    job_max_retries: int = Field(default=3, ge=3, le=20)
    job_backoff_base_seconds: float = Field(default=2.0, gt=0)
    job_backoff_max_seconds: float = Field(default=120.0, gt=0)
    job_execution_timeout_seconds: float = Field(default=30.0, gt=0)
    job_lease_seconds: float = Field(default=90.0, gt=0)
    job_reconcile_interval_seconds: float = Field(default=15.0, gt=0)
    worker_id: str = Field(default_factory=lambda: os.environ.get("HOSTNAME", "worker-1"))
    task_connector: str = "monday"

    # --- Misc ---------------------------------------------------------------
    people_directory_file: str = ""
    caller_directory_file: str = ""
    unknown_caller_policy: Literal["anonymous", "reject"] = "anonymous"
    agent_api_key: str = ""
    agent_provider: Literal["openai", "openrouter"] = "openai"
    agent_model: str = "gpt-4.1-mini"
    agent_timeout_seconds: float = Field(default=8.0, gt=0, le=30)
    review_before_submit: bool = True
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"

    @field_validator("asterisk_ari_url", "monday_api_url", "elevenlabs_base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    # ------------------------------------------------------------------ helpers
    @property
    def sqlite_path(self) -> Path:
        return sqlite_path_from_url(self.database_url)

    def monday_person_ids(self, environ: dict[str, str] | None = None) -> dict[str, int]:
        """Discover ``MONDAY_<NAME>_PERSON_ID`` variables -> {"mohamed": 123}.

        Adding a new assignee only needs a new env var plus an alias entry in the
        people directory; no code change.
        """
        env = os.environ if environ is None else environ
        result: dict[str, int] = {}
        for key, value in env.items():
            match = PERSON_ID_ENV.match(key.upper())
            if match and value.strip():
                result[match.group(1).lower()] = int(value.strip())
        return result

    def monday_requester_ids(self, environ: dict[str, str] | None = None) -> dict[str, int]:
        env = os.environ if environ is None else environ
        result: dict[str, int] = {}
        for key, value in env.items():
            match = REQUESTER_ID_ENV.match(key.upper())
            if match and value.strip():
                result[match.group(1).lower()] = int(value.strip())
        return result

    def validate_for(self, role: Role | str, environ: dict[str, str] | None = None) -> None:
        role = Role(role)
        env = os.environ if environ is None else environ
        problems: list[str] = []

        def need(*names: str) -> None:
            for name in names:
                if not str(getattr(self, name.lower(), "") or "").strip():
                    problems.append(f"{name} is required but empty or missing")

        if role in (Role.API, Role.WORKER, Role.ARI):
            need("REDIS_URL", "DATABASE_URL")
        if role is Role.ARI:
            need(
                "ASTERISK_ARI_URL",
                "ASTERISK_ARI_USERNAME",
                "ASTERISK_ARI_PASSWORD",
                "ASTERISK_STASIS_APP",
                "ELEVENLABS_API_KEY",
                "ELEVENLABS_VOICE_ID",
                "ASTERISK_SOUNDS_DIR",
            )
            if self.asterisk_playback_mode == "http":
                need("PUBLIC_MEDIA_BASE_URL")
            else:
                need("ASTERISK_SOUNDS_URI_PREFIX")
        uses_live_monday = self.monday_mode == "live" and role in (
            Role.API,
            Role.WORKER,
            Role.ARI,
            Role.SIMULATE,
        )
        if uses_live_monday:
            need(
                "MONDAY_API_TOKEN",
                "MONDAY_BOARD_ID",
                "MONDAY_GROUP_ID",
                "MONDAY_ASSIGNEE_COLUMN",
            )
            if not env.get("MONDAY_MOHAMED_PERSON_ID", "").strip():
                problems.append("MONDAY_MOHAMED_PERSON_ID is required when MONDAY_MODE=live")
            try:
                self.monday_person_ids(env)
            except ValueError:
                problems.append("Every MONDAY_<NAME>_PERSON_ID must be a numeric Monday user id")
            if self.monday_board_id and not self.monday_board_id.strip().isdigit():
                problems.append("MONDAY_BOARD_ID must be numeric")
        if not self.database_url.startswith("sqlite:///"):
            problems.append("DATABASE_URL must look like sqlite:///relative/path.db")
        if problems:
            raise ConfigError(role.value, problems)


def sqlite_path_from_url(url: str) -> Path:
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        raise ValueError(f"Unsupported DATABASE_URL: {url!r}")
    return Path(url[len(prefix) :])


def load_settings(env_file: str | Path | None = ".env") -> Settings:
    """Load ``.env`` (if present, without overriding real env vars) and build settings."""
    if env_file and Path(env_file).exists():
        load_dotenv(env_file, override=False)
    try:
        return Settings()
    except ValidationError as exc:
        problems = [
            f"{'.'.join(str(p) for p in err['loc']).upper()}: {err['msg']}" for err in exc.errors()
        ]
        raise ConfigError("startup", problems) from exc
