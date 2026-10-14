"""Command-line entry point: ``voice-agent <command>``.

Commands:
  api              Run the HTTP API (health, ready, simulate, jobs)
  worker           Run the background job worker
  ari              Run the Asterisk ARI voice agent
  simulate         Run the full business flow locally, no phone or Redis needed
  check-config     Validate environment variables for a role
  migrate          Create/upgrade the SQLite schema
  monday-inspect   Print board groups, columns and users (to find the IDs)
  render-asterisk  Render Asterisk config snippets from your .env
  tts-test         Synthesize a sentence with ElevenLabs into a WAV file
  stt-test         Transcribe a WAV file with faster-whisper
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any

from voice_agent.config import ConfigError, Role, Settings, load_settings
from voice_agent.logging_setup import configure_logging, get_logger

log = get_logger("voice_agent.cli")


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _install_stop_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - Windows
            signal.signal(sig, lambda *_: stop.set())


# --------------------------------------------------------------------------- api
def cmd_api(args: argparse.Namespace, settings: Settings) -> int:
    import uvicorn

    settings.validate_for(Role.API)
    uvicorn.run(
        "voice_agent.api.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        log_config=None,
        timeout_graceful_shutdown=10,
    )
    return 0


# --------------------------------------------------------------------------- worker
async def _run_worker(settings: Settings) -> None:
    from voice_agent.container import build_container, build_worker

    container = build_container(settings)
    worker = build_worker(container)
    stop = asyncio.Event()
    _install_stop_handlers(stop)
    task = asyncio.create_task(worker.run())
    await stop.wait()
    log.info("worker.stop_requested")
    worker.stop()  # finishes the in-flight job, then exits
    await task
    await container.aclose()


def cmd_worker(args: argparse.Namespace, settings: Settings) -> int:
    settings.validate_for(Role.WORKER)
    asyncio.run(_run_worker(settings))
    return 0


# --------------------------------------------------------------------------- ari
async def _run_ari(settings: Settings) -> None:
    from voice_agent.container import build_container
    from voice_agent.speech.stt import FasterWhisperSTT
    from voice_agent.speech.tts import ElevenLabsTTS
    from voice_agent.telephony.app import AriApplication
    from voice_agent.telephony.ari_client import AriClient
    from voice_agent.telephony.media import MediaPublisher, SpeechRenderer
    from voice_agent.telephony.voice_loop import VoiceLoop, VoiceLoopConfig

    container = build_container(settings)
    stt = FasterWhisperSTT(
        settings.whisper_model,
        device=settings.whisper_device,
        compute_type=settings.whisper_compute_type,
        beam_size=settings.whisper_beam_size,
        initial_prompt=settings.whisper_initial_prompt,
    )
    tts = ElevenLabsTTS(
        api_key=settings.elevenlabs_api_key,
        voice_id=settings.elevenlabs_voice_id,
        model_id=settings.elevenlabs_model_id,
        base_url=settings.elevenlabs_base_url,
        timeout_seconds=settings.elevenlabs_timeout_seconds,
    )
    publisher = MediaPublisher(
        mode=settings.asterisk_playback_mode,
        sounds_dir=settings.asterisk_sounds_dir,
        uri_prefix=settings.asterisk_sounds_uri_prefix,
        public_base_url=settings.public_media_base_url,
    )
    publisher.prune()
    speech = SpeechRenderer(tts, publisher)
    ari = AriClient(
        base_url=settings.asterisk_ari_url,
        username=settings.asterisk_ari_username,
        password=settings.asterisk_ari_password,
        app=settings.asterisk_stasis_app,
    )
    loop_config = VoiceLoopConfig(
        language=settings.default_language,
        inactivity_timeout_seconds=settings.call_inactivity_timeout_seconds,
        record_max_seconds=settings.record_max_seconds,
        record_max_silence_seconds=settings.record_max_silence_seconds,
        max_consecutive_failures=settings.call_max_consecutive_failures,
    )

    def loop_factory(channel):
        return VoiceLoop(
            channel=channel,
            stt=stt,
            engine=container.engine,
            acks=container.acks,
            config=loop_config,
        )

    await stt.warmup()
    # Pre-render greetings (cached on disk) so the first call starts instantly.
    greetings = {await container.acks.greeting() for _ in range(10)}
    for text in greetings:
        try:
            await speech.render(text)
        except Exception as exc:
            log.warning("ari.prerender_failed", error=repr(exc))

    app = AriApplication(ari=ari, speech=speech, loop_factory=loop_factory)
    stop = asyncio.Event()
    _install_stop_handlers(stop)
    try:
        await app.run(stop)
    finally:
        await ari.aclose()
        await tts.aclose()
        await container.aclose()


def cmd_ari(args: argparse.Namespace, settings: Settings) -> int:
    settings.validate_for(Role.ARI)
    asyncio.run(_run_ari(settings))
    return 0


# --------------------------------------------------------------------------- simulate
async def _simulate(args: argparse.Namespace, settings: Settings) -> int:
    from voice_agent.container import build_container, build_worker
    from voice_agent.conversation.engine import ConversationState
    from voice_agent.jobs.queue import InMemoryJobQueue

    container = build_container(settings, queue=InMemoryJobQueue())
    worker = build_worker(container)
    state = ConversationState(call_id="simulated-call")
    print(f"📞 simulated call started (session {state.session_id}, Monday: {settings.monday_mode})")

    async def turn(text: str) -> bool:
        reply = await container.engine.handle(state, text)
        _print_json(
            {
                "caller": text,
                "reply_kind": reply.kind,
                "intent": reply.intent.public_dict() if reply.intent else None,
                "job_id": reply.job.id if reply.job else None,
                "status": reply.job.status.value if reply.job else None,
                "acknowledgment": reply.text,
                "call_active": not reply.end_call,
            }
        )
        return not reply.end_call

    texts = list(args.text or [])
    if args.interactive or not texts:
        print("Type Arabic instructions (empty line or Ctrl-D to hang up).")
        while True:
            try:
                line = await asyncio.to_thread(input, "🗣  ")
            except EOFError:
                break
            if not line.strip() or not await turn(line):
                break
            await worker.run_until_idle(max_wait=60)
    else:
        for text in texts:
            if not await turn(text):
                break

    print(
        "📴 caller hung up — background jobs keep running"
        if state.accepted_job_ids
        else "📴 call ended"
    )
    await worker.run_until_idle(max_wait=120)
    results = []
    for job_id in state.accepted_job_ids:
        job = container.store.get(job_id)
        events = container.store.events(job_id)
        results.append(
            {
                "job_id": job_id,
                "final_status": job.status.value,
                "attempts": job.attempts,
                "external_ref": job.external_ref,
                "result": job.result,
                "last_error": job.last_error,
                "events": [f"{e.status.value}: {e.message}" for e in events],
            }
        )
    if results:
        print("✅ final job status:")
        _print_json(results)
    await container.aclose()
    return 0 if all(r["final_status"] == "completed" for r in results) else 1


def cmd_simulate(args: argparse.Namespace, settings: Settings) -> int:
    updates: dict[str, Any] = {
        "database_url": f"sqlite:///{args.db}",
        "monday_mode": "live" if args.live_monday else "mock",
        "job_backoff_base_seconds": 0.2
        if not args.live_monday
        else settings.job_backoff_base_seconds,
    }
    if args.fail_times is not None:
        updates["mock_monday_fail_times"] = args.fail_times
    settings = settings.model_copy(update=updates)
    settings.validate_for(Role.SIMULATE)
    return asyncio.run(_simulate(args, settings))


# --------------------------------------------------------------------------- utilities
def cmd_check_config(args: argparse.Namespace, settings: Settings) -> int:
    settings.validate_for(args.role)
    print(f"Configuration OK for role '{args.role}' (MONDAY_MODE={settings.monday_mode}).")
    return 0


def cmd_migrate(args: argparse.Namespace, settings: Settings) -> int:
    from voice_agent.db import Database

    version = Database(settings.sqlite_path).migrate()
    print(f"SQLite schema at version {version}: {settings.sqlite_path}")
    return 0


def cmd_monday_inspect(args: argparse.Namespace, settings: Settings) -> int:
    from voice_agent.connectors.monday import MondayConfig, MondayGraphQLConnector

    missing = [n for n in ("monday_api_token", "monday_board_id") if not getattr(settings, n)]
    if missing:
        raise ConfigError("monday-inspect", [f"{m.upper()} is required" for m in missing])

    async def run() -> dict:
        connector = MondayGraphQLConnector(
            MondayConfig(
                api_token=settings.monday_api_token,
                board_id=settings.monday_board_id,
                group_id=settings.monday_group_id,
                assignee_column=settings.monday_assignee_column,
                api_url=settings.monday_api_url,
                api_version=settings.monday_api_version,
            ),
            {},
        )
        try:
            return await connector.inspect()
        finally:
            await connector.aclose()

    data = asyncio.run(run())
    _print_json(data)
    print(
        "\nUse: group id -> MONDAY_GROUP_ID; assignee column id -> MONDAY_ASSIGNEE_COLUMN.\n"
        "  People column:   MONDAY_ASSIGNEE_COLUMN_TYPE=people,   "
        "MONDAY_MOHAMED_PERSON_ID=<user id>\n"
        "  Dropdown column: MONDAY_ASSIGNEE_COLUMN_TYPE=dropdown, "
        "MONDAY_MOHAMED_PERSON_ID=<option id>",
        file=sys.stderr,
    )
    return 0


def cmd_render_asterisk(args: argparse.Namespace, settings: Settings) -> int:
    from voice_agent.asterisk_templates import render_all

    if not settings.asterisk_test_extension:
        raise ConfigError("render-asterisk", ["ASTERISK_TEST_EXTENSION is required"])
    out = Path(args.out)
    for path in render_all(settings, out):
        print(f"wrote {path}")
    print("Review the files, then follow docs/ASTERISK.md. Nothing was changed on any PBX.")
    return 0


def cmd_tts_test(args: argparse.Namespace, settings: Settings) -> int:
    from voice_agent.speech.tts import ElevenLabsTTS

    async def run() -> bytes:
        tts = ElevenLabsTTS(
            api_key=settings.elevenlabs_api_key,
            voice_id=settings.elevenlabs_voice_id,
            model_id=settings.elevenlabs_model_id,
            base_url=settings.elevenlabs_base_url,
        )
        try:
            return (await tts.synthesize(args.text, sample_rate=16000)).to_wav()
        finally:
            await tts.aclose()

    if not settings.elevenlabs_api_key:
        raise ConfigError("tts-test", ["ELEVENLABS_API_KEY is required"])
    Path(args.out).write_bytes(asyncio.run(run()))
    print(f"wrote {args.out}")
    return 0


def cmd_stt_test(args: argparse.Namespace, settings: Settings) -> int:
    from voice_agent.speech.stt import FasterWhisperSTT

    audio = Path(args.file).read_bytes()

    async def run():
        stt = FasterWhisperSTT(
            settings.whisper_model,
            device=settings.whisper_device,
            compute_type=settings.whisper_compute_type,
            initial_prompt=settings.whisper_initial_prompt,
        )
        return await stt.transcribe(audio, language=settings.default_language)

    result = asyncio.run(run())
    _print_json(
        {"text": result.text, "duration": result.duration, "avg_logprob": result.avg_logprob}
    )
    return 0


# --------------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="voice-agent", description=__doc__.split("\n")[0])
    parser.add_argument("--env-file", default=".env", help="optional .env file to load")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("api", help="run the HTTP API")
    p.add_argument("--host", default=os.environ.get("API_HOST", "0.0.0.0"))  # noqa: S104
    p.add_argument("--port", type=int, default=int(os.environ.get("API_PORT", "8000")))
    p.set_defaults(func=cmd_api)

    sub.add_parser("worker", help="run the background worker").set_defaults(func=cmd_worker)
    sub.add_parser("ari", help="run the Asterisk ARI voice agent").set_defaults(func=cmd_ari)

    p = sub.add_parser("simulate", help="simulate a call locally (mock Monday by default)")
    p.add_argument("text", nargs="*", help="one or more caller utterances, in order")
    p.add_argument("--interactive", "-i", action="store_true")
    p.add_argument("--live-monday", action="store_true", help="use the real Monday API")
    p.add_argument("--fail-times", type=int, help="make the mock Monday fail N times first")
    p.add_argument("--db", default="data/simulator.db", help="SQLite file for the simulation")
    p.set_defaults(func=cmd_simulate)

    p = sub.add_parser("check-config", help="validate configuration")
    p.add_argument("--role", choices=[r.value for r in Role], default="api")
    p.set_defaults(func=cmd_check_config)

    sub.add_parser("migrate", help="apply SQLite migrations").set_defaults(func=cmd_migrate)
    sub.add_parser("monday-inspect", help="list board groups/columns/users").set_defaults(
        func=cmd_monday_inspect
    )

    p = sub.add_parser("render-asterisk", help="render Asterisk config snippets")
    p.add_argument("--out", default="build/asterisk")
    p.set_defaults(func=cmd_render_asterisk)

    p = sub.add_parser("tts-test", help="synthesize text to a WAV file with ElevenLabs")
    p.add_argument("text")
    p.add_argument("--out", default="tts-test.wav")
    p.set_defaults(func=cmd_tts_test)

    p = sub.add_parser("stt-test", help="transcribe a WAV file")
    p.add_argument("file")
    p.set_defaults(func=cmd_stt_test)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(args.env_file)
        configure_logging(
            settings.log_level, "text" if args.command == "simulate" else settings.log_format
        )
        if args.command == "simulate" and settings.log_level.upper() == "INFO":
            configure_logging("WARNING", "text")
        return args.func(args, settings)
    except ConfigError as exc:
        print(f"\n❌ {exc}\n", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
