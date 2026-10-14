# Voice Monday Agent

An Egyptian Arabic phone assistant for an existing **Asterisk** PBX. A caller says:

> «حطي تاسك لمحمد إنه يشتري هارد للسيرفر»

The assistant does four things:

1. Understands the request: `{"action": "create_task", "assignee": "Mohamed", "task": "شراء هارد للسيرفر"}`.
2. Saves it as a **durable background job**. It is committed to disk before she speaks.
3. Replies right away with a short acknowledgment built from the request, in a female
   Egyptian voice (ElevenLabs). For example: «حاضر، هقول لمحمد يشتري هارد للسيرفر حالًا.»
4. **Keeps listening** for more instructions. A background worker creates the Monday.com
   item even if the caller has already hung up.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design, and
[docs/ASTERISK.md](docs/ASTERISK.md) for connecting a PBX safely.

## Quick start: simulate a call (no phone, no credentials)

Requires Docker. Monday is mocked by default.

```bash
docker compose build api
docker compose run --rm --no-deps api simulate "حطي تاسك لمحمد إنه يشتري هارد للسيرفر"
```

Several utterances in one call (including a clarification turn), then a hang-up:

```bash
docker compose run --rm --no-deps api simulate "حطي تاسك لمحمد إنه يشتري هارد للسيرفر" "حطي تاسك لأحمد" "يراجع الميزانية"
```

Interactive mode (type Arabic lines, press Enter on an empty line to hang up):

```bash
docker compose run --rm --no-deps api simulate -i
```

Show the retry path: the mock Monday fails twice, then succeeds.

```bash
docker compose run --rm --no-deps api simulate --fail-times 2 "حطي تاسك لمحمد إنه يشتري هارد للسيرفر"
```

The simulator prints the parsed intent, the acknowledgment, the job id, and each job's
final status and event history.

## Run the stack (API + worker + Redis)

```bash
cp .env.example .env          # MONDAY_MODE=mock by default
docker compose up -d --build
curl -s localhost:8000/health
curl -s localhost:8000/ready
curl -s -X POST localhost:8000/simulate \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-0001' \
  -d '{"text": "حطي تاسك لمحمد إنه يشتري هارد للسيرفر"}'
curl -s localhost:8000/jobs/<job_id>
curl -s localhost:8000/jobs/<job_id>/events
```

`POST /simulate` accepts:

| Field | Meaning |
|---|---|
| `text` | Arabic utterance (1–1000 chars) |
| `session_id` | Reuse the returned id to continue a conversation (clarifications) |
| `idempotency_key` or `Idempotency-Key` header | Resubmitting returns the same job (no duplicates) |
| `wait_seconds` | Optionally wait up to 30 s for the job's final status |

Interactive API docs: <http://localhost:8000/docs>.

### Use the real Monday.com API

1. Fill in `MONDAY_API_TOKEN` and `MONDAY_BOARD_ID` in `.env`, then list the board's
   groups, columns and users:

   ```bash
   docker compose run --rm --no-deps api monday-inspect
   ```

2. Set `MONDAY_GROUP_ID` and `MONDAY_ASSIGNEE_COLUMN`, then pick how the assignee is stored:
   * a *People* column: `MONDAY_ASSIGNEE_COLUMN_TYPE=people` and
     `MONDAY_MOHAMED_PERSON_ID=<Monday user id>`;
   * a *Dropdown* column (e.g. "Assigned To" with options per person):
     `MONDAY_ASSIGNEE_COLUMN_TYPE=dropdown` and `MONDAY_MOHAMED_PERSON_ID=<option id>`.
     `monday-inspect` lists each dropdown's `options`.

   Optionally set `MONDAY_IDEMPOTENCY_COLUMN` (a *Text* column) for extra duplicate
   protection.
3. Set `MONDAY_MODE=live`, then `docker compose up -d`.

More assignees need no code: add `MONDAY_AHMED_PERSON_ID=...` and make sure the name is
in the people directory ([src/voice_agent/people.json](src/voice_agent/people.json), or a
custom file set with `PEOPLE_DIRECTORY_FILE`).

### Phone calls via Asterisk

Follow [docs/ASTERISK.md](docs/ASTERISK.md). In short: route **one unused test extension**
to `Stasis(voice-monday-agent)`, add a dedicated ARI user, then:

```bash
docker compose -f docker-compose.yml -f docker-compose.pbx-host.yml --profile telephony up -d --build
```

## Tests, lint, formatting

The test image is Python 3.12; no local Python is needed.

```bash
docker compose --profile test build tests
docker compose --profile test run --rm tests ruff format --check src tests
docker compose --profile test run --rm tests ruff check src tests
docker compose --profile test run --rm tests pytest -q
```

(`make check` runs format, lint and tests.) The default suite mocks every external API.

Optional live Monday integration test. It creates one `[TEST]` item and then archives it:

```bash
docker compose --profile test run --rm -e RUN_MONDAY_INTEGRATION=1 tests pytest -m integration tests/integration
```

Handy manual checks:

```bash
docker compose run --rm --no-deps api check-config --role ari    # lists every missing variable
docker compose run --rm --no-deps api tts-test "حاضر، هقول لمحمد يشتري هارد للسيرفر حالًا." --out /app/data/tts.wav
docker compose run --rm --no-deps ari stt-test /app/data/some-recording.wav
```

## Configuration

All settings are environment variables. See [.env.example](.env.example). `.env` is
git-ignored. Each process checks what it needs at startup and lists every missing
variable in one clear error.

| Variable | Default | Notes |
|---|---|---|
| `MONDAY_MODE` | `mock` | `live` uses the real API |
| `MONDAY_TASK_NAME_COLUMN` | `name` | `name` means the item name. A text column id also receives the task text. |
| `MONDAY_API_VERSION` | `2026-07` | Monday's current stable version (Oct 2026) |
| `JOB_MAX_RETRIES` | `3` | Retries after the first attempt (minimum 3), then `dead_letter` |
| `CALL_INACTIVITY_TIMEOUT_SECONDS` | `60` | Silence before she says goodbye and hangs up |
| `RECORD_MAX_SILENCE_SECONDS` | `2` | End-of-utterance silence |
| `WHISPER_MODEL` | `small` | `medium` / `large-v3` are more accurate but slower |
| `ELEVENLABS_MODEL_ID` | `eleven_multilingual_v2` | `eleven_flash_v2_5` for lower latency |

## Project layout

```
src/voice_agent/
  nlu/            IntentParser + deterministic Egyptian Arabic parser
  ack/            AcknowledgmentGenerator + intent-aware templates
  conversation/   ConversationEngine (shared by phone, API, simulator) + call audit log
  commands/       Command handlers: intent -> connector-agnostic JobSpec
  connectors/     Connector interface, Monday (GraphQL + mock)
  jobs/           SQLite JobStore, JobQueue (Redis / in-memory), Worker, JobService
  speech/         SpeechToTextProvider (faster-whisper), TextToSpeechProvider (ElevenLabs)
  telephony/      ARI client, CallChannel, VoiceLoop, AriApplication, media publishing
  api/            FastAPI app
  container.py    Composition root (the only place choosing implementations)
  cli.py          voice-agent api | worker | ari | simulate | ...
asterisk/         Example Asterisk snippets (rendered versions: `voice-agent render-asterisk`)
docs/             ARCHITECTURE.md, ASTERISK.md
tests/            pytest suite (+ optional live Monday integration test)
```

## Assumptions and known limitations

* **Supported command**: only `create_task` runs today. The parser also recognises reminders,
  meetings and messages, and acknowledgment templates exist for them. Until handlers are
  added, she says those are not supported yet. A reminder is offered as a task instead.
* **Relay phrasing** («قولي لمحمد يشتري…», «خلي محمد…») is treated as `create_task`.
* **Task title**: the verb is turned into a verbal noun (يشتري → شراء). Unknown verbs are
  kept as spoken. The whole phrase, including any time words, stays in the title.
* **Unknown or unconfigured people** are never guessed. She asks who the task is for.
  In mock mode every directory person gets a fake Monday id.
* **Clarification**: one short question per turn and at most two rounds. After that she
  asks for the full request again.
* **Turn-taking**: record until silence (or `#`), then reply. There is no barge-in while
  she is speaking.
* **Addressing the caller**: replies use the common Egyptian polite default (حضرتك /
  masculine second person). Edit `ack/templates.py` to change this.
* **Commands spoken right before a hang-up** are still transcribed and executed. She just
  cannot answer.
* **Idempotency on `/simulate`** across HTTP retries needs a client-provided key. Phone
  commands get a key automatically.
* **Monday's `Idempotency-Key`** window is 30 minutes. For recoveries after longer outages,
  configure `MONDAY_IDEMPOTENCY_COLUMN`.
* **Accuracy**: Whisper `small` handled telephone-quality (8 kHz) Arabic correctly in local
  tests. Real Egyptian callers, noisy lines and names benefit from `medium`/`large-v3`.
  `WHISPER_INITIAL_PROMPT` biases it toward this domain's vocabulary.
* **SQLite** is shared by the processes on one host (WAL mode). Use Postgres for multi-host.
