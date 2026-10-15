# Architecture

One repository, three small processes that share one SQLite file and one Redis:

| Process | Command | Role |
|---|---|---|
| `ari` | `voice-agent ari` | Phone calls: Asterisk ARI → STT → engine → TTS |
| `api` | `voice-agent api` | `/health`, `/ready`, `/simulate`, `/jobs/{id}`, `/jobs/{id}/events` |
| `worker` | `voice-agent worker` | Executes durable jobs through connectors, with retries |
| ElevenLabs Agent (hosted) | `voice-agent el-agent sync` | Extension 778: listening, AI and voice in the cloud; calls `/agent/tools/*` on `api` ([details](ELEVENLABS_AGENT.md)) |

```
                         ┌──────────────────────── call session (per call, in memory) ─────────────────────────┐
Asterisk ─ARI─► AriCallChannel ─► VoiceLoop ─► SpeechToTextProvider ─► ConversationEngine ─► TextToSpeechProvider ─► play
                                                                          │   IntentParser
                                                                          │   CommandHandler.build_job
                                                                          │   Connector.validate      (cheap, local)
                                                                          ▼
                                                   JobService.submit: SQLite COMMIT ─► JobQueue.enqueue (Redis)
                                                                          │
                                                   AcknowledgmentGenerator (only after the commit)
                         └──────────────────────────────────────────────────────────────────────────────────────┘

                         ┌──────────────── background (independent of any call) ────────────────┐
Redis ─► Worker ─► JobStore.claim (guarded UPDATE) ─► Connector.execute ─► complete / retry / failed / dead_letter
                         └───────────────────────────────────────────────────────────────────────┘
```

`/agent/tools/*` keeps its 2-second grace-window drafts in the `api` process memory
(one uvicorn process). Committed tasks are durable like every other job; a draft that is
still inside its window when the API stops is committed during shutdown.

## Key guarantees

* **Accept = durable.** The engine only produces an acknowledgment after the job row is
  committed to SQLite. The caller can hang up the moment they hear it.
* **Call and job are independent.** Call state (`ConversationState`, the `utterances` audit
  table) never mixes with job state (`jobs`, `job_events`). A hang-up never touches jobs.
* **Never lose a job.** Redis carries only job ids. If enqueueing fails, or Redis loses data,
  the worker's reconciler re-enqueues due `queued` jobs and `processing` jobs whose lease
  expired. Duplicate queue messages are harmless, because `claim` is a guarded UPDATE.
* **No duplicate Monday items.**
  1. Each accepted command gets an idempotency key (`call/session + turn + words`, or a
     client-provided key on `/simulate`). It is a unique column, so resubmitting returns
     the original job.
  2. A `completed` job is never claimed again.
  3. The key is sent as Monday's `Idempotency-Key` header, so retries within 30 minutes
     replay the original response.
  4. Optional `MONDAY_IDEMPOTENCY_COLUMN`: the key is also written onto the item. When an
     earlier attempt's outcome is unknown (timeout, crash), the connector looks the item up
     before creating it again.
* **Retries.** Only `TransientConnectorError` is retried, with exponential backoff and
  jitter (honouring `Retry-After`). That covers timeouts, 5xx, 429, 423, 409, and Monday's
  rate-limit/complexity codes. With `JOB_MAX_RETRIES` (≥3) exhausted the job becomes
  `dead_letter`. Permanent errors (bad ids, auth, validation) go straight to `failed`.
  The transcript, intent and error history are always kept.

Job states: `queued → processing → completed | failed | dead_letter`. A retry is
`processing → queued` with a future `next_attempt_at`. Every transition is a row in
`job_events`.

## Interfaces (all replaceable)

| Interface | Module | Shipped implementation |
|---|---|---|
| `SpeechToTextProvider` | `speech/stt.py` | `FasterWhisperSTT` (local, offline) |
| `TextToSpeechProvider` | `speech/tts.py` | `ElevenLabsTTS` (default premade voice `cgSgspJ2msm6clMCkdW9`) |
| `IntentParser` | `nlu/base.py` | `EgyptianArabicParser` (deterministic rules) |
| `AcknowledgmentGenerator` | `ack/base.py` | `TemplateAcknowledgmentGenerator` |
| `JobQueue` | `jobs/queue.py` | `RedisJobQueue`, `InMemoryJobQueue` |
| `Connector` | `connectors/base.py` | — |
| `MondayConnector` | `connectors/monday.py` | `MondayGraphQLConnector`, `MockMondayConnector` |
| `CommandHandler` | `commands/base.py` | `CreateTaskHandler` |
| `CallChannel` | `telephony/channel.py` | `AriCallChannel` |

All wiring happens in `container.py`. Nothing else instantiates concrete classes.

## Adding a connector (e.g. WhatsApp)

1. Create `connectors/whatsapp.py`:

   ```python
   class WhatsAppConnector(Connector):
       name = "whatsapp"
       operations = frozenset({"send_message"})

       def validate(self, operation, payload):         # cheap, local checks
           ...

       async def execute(self, operation, payload, ctx):
           # Use ctx.idempotency_key for provider-side dedupe where supported.
           # Raise TransientConnectorError (retry) or PermanentConnectorError (stop).
           return ConnectorResult(external_id=message_id, data={...})
   ```

2. Register it in `container.build_connectors`, reading its settings from env vars
   (add them to `config.Settings`, `.env.example` and `validate_for`).
3. Add tests with a mocked HTTP transport (see `tests/test_monday.py`).

The call pipeline, engine, worker and API do not change.

## Adding a command (e.g. `send_message`)

1. **Intent schema**: add the `Action` (already present for `send_message`, `remind`,
   `schedule_meeting`) and any new slot in `IntentDetails`.
2. **Parser support**: add a matcher in `EgyptianArabicParser` (or let an LLM parser
   emit it). Tests go in `tests/test_parser.py`.
3. **Handler**: a `CommandHandler` that declares `required_fields` and builds a `JobSpec`:

   ```python
   class SendMessageHandler(CommandHandler):
       action = Action.SEND_MESSAGE
       required_fields = ("assignee", "task")

       def build_job(self, intent):
           return JobSpec(connector="whatsapp", operation="send_message",
                          payload={"to": intent.details.assignee_key, "text": intent.task})
   ```

4. **Register** it in `container.build_commands`.
5. **Acknowledgment**: templates for `send_message`/`remind`/`schedule_meeting` already
   exist; add one for any new action.

Until a handler is registered, the engine answers "not supported yet" without ending
the call. It also offers a fallback where one makes sense: an unsupported reminder is
offered as a task.

## Swapping in an LLM

* `IntentParser`: implement `parse()` with Claude/OpenAI structured output returning the
  same `ParseResult`/`Intent`. Keep the deterministic parser as a fast path or fallback.
* `AcknowledgmentGenerator`: generate one short sentence from the `Intent`. Keep the
  "pending, never claim success" rule in the prompt and check it with the same tests
  (`tests/test_acknowledgments.py`).

Both are set in `build_container(...)`, which already accepts `parser=` and `acks=`.

## Scaling notes

* SQLite (WAL) is right for a single host. For multiple hosts, move `JobStore` and
  `SessionLog` to Postgres. The interfaces stay the same.
* Run several workers for throughput. Claims are atomic, and each worker has its own
  Redis processing list (`WORKER_ID`, default the container hostname).
* faster-whisper on CPU handles a few concurrent calls with `small`. Use a GPU and
  `large-v3` for better Egyptian-dialect accuracy at higher volume.
