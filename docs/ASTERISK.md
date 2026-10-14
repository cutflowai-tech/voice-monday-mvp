# Connecting the agent to Asterisk (ARI)

> **Nothing in this repository changes a live PBX.** These are instructions and
> generated snippets that a PBX administrator reviews and applies by hand. Every step
> is additive and limited to **one unused test extension**, and every step has a revert.

## How it fits together

```
Caller ──► Asterisk ──(dialplan: test extension only)──► Stasis(voice-monday-agent)
                                                              │  ARI WebSocket events
                                                              ▼
                                                    voice-agent ari (this project)
                                       answer · record utterance · fetch WAV over ARI HTTP
                                       faster-whisper → parser → durable job → ElevenLabs
                                       writes .sln16 into the shared sounds folder
                                                              │
Asterisk plays  sound:voice-monday/<file>  ◄──┘
```

* The agent connects **outbound** to Asterisk's ARI (HTTP + WebSocket, port 8088 by default).
* Caller audio: Asterisk records each utterance as WAV; the agent downloads it with
  `GET /ari/recordings/stored/{name}/file` and deletes it afterwards.
* Assistant audio: the agent writes signed-linear 16 kHz files (`.sln16`) into a folder
  that Asterisk can read, and plays them with `sound:<absolute path>`.

## Requirements

| Item | Check command (on the PBX) |
|---|---|
| Asterisk 16+ (ARI stored-recording download needs 14+) | `asterisk -rx "core show version"` |
| ARI + Stasis modules | `asterisk -rx "module show like res_ari"` and `... like res_stasis` |
| WebSocket support | `asterisk -rx "module show like res_http_websocket"` |
| WAV + signed-linear formats | `asterisk -rx "core show file formats"` (look for `wav`, `sln16`) |
| Resampling 16 kHz → 8 kHz | `asterisk -rx "module show like codec_resample"` |

## 1. Choose an unused test extension

Pick a number that does not exist anywhere, e.g. `777`. Find the context your test
phone uses (`asterisk -rx "pjsip show endpoint <your-phone>"` → `context`), then:

```bash
asterisk -rx "dialplan show 777@from-internal"
```

It must report that the extension does not exist. Put the number in `.env`:

```bash
ASTERISK_TEST_EXTENSION=777
```

## 2. Generate the snippets from your `.env`

```bash
mkdir -p build && docker compose run --rm --no-deps --user "$(id -u):$(id -g)" -v "$PWD/build:/app/build" api render-asterisk --out build/asterisk
```

This writes three files to `build/asterisk/` (git-ignored). Static examples with
placeholder values live in [`asterisk/`](../asterisk).

| File | Goes into | Purpose |
|---|---|---|
| `extensions_voice_monday.conf` | `/etc/asterisk/` | New context `[voice-monday-test]` with only the test extension |
| `ari_voice_monday.conf` | appended to `ari.conf` | A dedicated ARI user for the agent (contains the password, mode 0600) |
| `http_voice_monday.conf` | merged into `http.conf` `[general]` | Enables the HTTP server ARI needs |

## 3. Apply on the PBX (additive changes only)

Back up first: `sudo cp -a /etc/asterisk /etc/asterisk.bak-$(date +%F)`.

### 3a. HTTP server (`/etc/asterisk/http.conf`)

Check whether it is already on: `asterisk -rx "http show status"`. If it shows
`Server Enabled and Bound to ...:8088` you can skip this step (FreePBX usually has it on).
Otherwise, in the **existing** `[general]` section set:

```ini
enabled = yes
bindaddr = 127.0.0.1   ; agent on the same host (recommended); else the PBX LAN IP
bindport = 8088
```

Never expose 8088 to the internet. If the agent runs on another machine, restrict 8088
to that machine's IP with the PBX firewall.

### 3b. ARI user (`/etc/asterisk/ari.conf`)

Make sure `[general]` contains `enabled = yes`, then append the user section from
`ari_voice_monday.conf`:

```ini
[voice-monday]
type = user
read_only = no
password = <ASTERISK_ARI_PASSWORD from your .env>
password_format = plain
```

FreePBX: put the user section in `/etc/asterisk/ari_additional_custom.conf` instead and do
not touch the FreePBX-managed ARI user.

### 3c. Dialplan

```bash
sudo cp build/asterisk/extensions_voice_monday.conf /etc/asterisk/
```

* **Plain Asterisk** — at the end of `/etc/asterisk/extensions.conf` add
  `#include extensions_voice_monday.conf`, and inside the context your test phone uses add
  `include => voice-monday-test`.
* **FreePBX** — add `#include extensions_voice_monday.conf` to
  `/etc/asterisk/extensions_custom.conf`, and in the same file add
  `include => voice-monday-test` under `[from-internal-custom]`.

The included context contains a single literal extension, so no existing extension or
pattern changes behaviour. (If the phone's context has a broad pattern such as `_7XXX`
that already matches 777, choose another number.)

### 3d. Sounds folder for generated speech

The agent container runs as UID/GID **10001**. Asterisk only needs to *read* the files
(they are written with mode 0644):

```bash
sudo install -d -m 0755 -o 10001 -g 10001 /usr/share/asterisk/sounds/voice-monday
```

and in `.env`:

```bash
ASTERISK_PLAYBACK_MODE=file
ASTERISK_SOUNDS_HOST_DIR=/usr/share/asterisk/sounds/voice-monday  # host astdatadir/sounds
ASTERISK_SOUNDS_URI_PREFIX=voice-monday  # relative to Asterisk sounds directory
```

If the agent runs on a different machine, the same folder must be shared (NFS/SMB) so that
both paths point at the same files, or use the experimental `http` mode below.

### 3e. Reload (no restart needed)

```bash
asterisk -rx "module reload http"          # only if you changed http.conf
asterisk -rx "module reload res_ari.so"
asterisk -rx "dialplan reload"
```

Verify:

```bash
asterisk -rx "ari show users"                         # voice-monday listed
asterisk -rx "dialplan show 777@from-internal"       # resolves to voice-monday-test
```

## 4. Start the agent

`.env` needs the ARI and ElevenLabs values. **Keep `MONDAY_MODE=mock` for the first calls.**

```bash
# Agent on the PBX host, ARI bound to 127.0.0.1 (recommended):
ASTERISK_ARI_URL=http://127.0.0.1:8088
docker compose -f docker-compose.yml -f docker-compose.pbx-host.yml --profile telephony up -d --build

# Agent on another machine (ARI reachable over the LAN):
ASTERISK_ARI_URL=http://<pbx-ip>:8088
docker compose --profile telephony up -d --build
```

The first start downloads the Whisper model into the `whisper-models` volume. Check:

```bash
docker compose logs -f ari          # expect "ari.ready" then "ari.connected"
asterisk -rx "ari show apps"        # voice-monday-agent listed
```

## 5. Test procedure (does not affect existing extensions)

1. With `MONDAY_MODE=mock`, dial `777` from the test phone; run `asterisk -rvvv` to watch.
2. Speak as soon as the call is answered: **«حطي تاسك لمحمد إنه يشتري هارد للسيرفر»**.
3. Expect a short acknowledgment that mentions Mohamed and the hard drive, and the call
   keeps listening. Give a second instruction, then hang up.
4. Check the jobs (`docker compose logs worker`, or `GET /jobs/{id}` using the `job_id`
   in the `ari` logs): they reach `completed` even though you hung up.
5. Say an unclear request («حطي تاسك») and confirm she asks one short question.
6. Say «مع السلامة» and confirm the call ends; call again and stay silent to confirm the
   inactivity timeout (`CALL_INACTIVITY_TIMEOUT_SECONDS`).
7. Only then switch `.env` to `MONDAY_MODE=live` (ideally a test board/group first),
   `docker compose up -d` again, and repeat step 2.

Other extensions are untouched throughout: only calls to the test extension enter
`Stasis(voice-monday-agent)`, and the agent ignores everything else.

## Audio format compatibility

| Direction | Format | Notes |
|---|---|---|
| Caller → agent | ARI `record` with `format=wav` → 8 kHz 16-bit mono PCM | Asterisk transcodes from any call codec (ulaw/alaw/G.722/Opus). The agent upsamples to 16 kHz for Whisper. |
| Agent → caller | ElevenLabs `pcm_16000` → `.sln16` file | Asterisk transcodes to the call codec; needs `format_sln` and `codec_resample`. |
| TTS failure | no playback | The call remains active for another utterance; no artificial beep is played. |

* **G.729 phones**: transcoding needs a G.729 codec module; use ulaw/alaw on the test phone.
* Silence detection uses ARI `maxSilenceSeconds` (`RECORD_MAX_SILENCE_SECONDS`, default 2);
  `#` also ends an utterance. An utterance is capped at `RECORD_MAX_SECONDS` (default 20).
* **`http` playback mode (experimental)**: `ASTERISK_PLAYBACK_MODE=http` writes 8 kHz WAV
  files served by the API at `/media/<name>.wav` and plays `sound:http://…`. This depends on
  `res_http_media_cache`, and ARI support for HTTP media differs between Asterisk versions.
  Test it with `Playback(http://…/file.wav)` in the dialplan before relying on it.

## Revert

1. Stop the agent: `docker compose --profile telephony down` (jobs already in SQLite
   survive. The worker can keep running without telephony).
2. Remove the `include => voice-monday-test` line and the `#include extensions_voice_monday.conf`
   line, then `sudo rm /etc/asterisk/extensions_voice_monday.conf`.
3. Remove the `[voice-monday]` section from `ari.conf` (or `ari_additional_custom.conf`).
4. Undo any `http.conf` change you made in 3a (or restore from the backup).
5. Reload: `asterisk -rx "dialplan reload"`, `asterisk -rx "module reload res_ari.so"`,
   and `asterisk -rx "module reload http"` if you changed http.conf.
6. Optionally `sudo rm -r /usr/share/asterisk/sounds/voice-monday`.
7. Confirm: `asterisk -rx "dialplan show 777@from-internal"` reports no such extension.
