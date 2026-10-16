# Sarah: the ElevenLabs Agent on extensions 777 and 778

```
Phone → Asterisk ext 778: Answer + AudioSocket ─TCP 127.0.0.1:9019─▶ el-bridge
                                         │ outbound TLS WebSocket (signed URL per call)
                                         ▼
                ElevenLabs Agent (listening · AI · turn-taking · voice)
                                         │ says the acknowledgment, then calls submit_task
                                         ▼ HTTPS via Cloudflare Tunnel (bearer secret)
       api: /agent/tools/submit_task → 2 s grace window (held while you still talk)
            → job → worker → Monday → {"status": "created"} → «Done، خلاص حطيت التاسك»
```

Why a bridge instead of SIP: the office reaches the internet through a load balancer over
several lines with rotating public IPs, so SIP/RTP from ElevenLabs never found its way back
(one-way audio). The bridge uses only local TCP and an outbound WebSocket, so NAT doesn't
matter. The SIP mode is kept in the PBX script for a network with one stable public IP.

The bridge (`voice-agent el-bridge`, compose profile `agent`/`agent-quick`):

- plays ringback while the agent connects (about 0.5 s with a prefetched signed URL), so
  she "picks up" with «ألو» instead of the caller hearing dead air;
- streams 20 ms frames continuously (silence between words), because Asterisk's
  AudioSocket hangs up after 2 s without data;
- passes the caller's extension (carried in the AudioSocket UUID) as `caller_id`;
- marks caller speech in Redis, so the grace window waits until 1.5 s after the caller
  stops talking (at most 6 s more) before saving.

The conversation the user hears:

```
Agent: ألو
You:   عاوزك تحطي تاسك لأحمد يجيب الكهربائي بكرة
Agent: تمام، هقول لأحمد يجيب الكهربائي بكرة
       (2 s: a correction replaces the task, an extra request adds one, «الغيها» cancels it)
Agent: Done، خلاص حطيت التاسك. حاجة تانية؟
```

## Sarah

The agent is **Sarah (سارة)**, the team's task-line assistant. Her identity lives in
`src/voice_agent/sarah.py`:

- **Persona prompt:** warm, quick, lightly playful Cairene Arabic in female forms. If asked,
  she says she's the team's AI assistant.
- **Greetings:** per call, the bridge picks a greeting by the caller's gender and name
  from `config/callers.json` (e.g. «أهلاً يا أحمد، معاك سارة»).
- **Wording:** a different suggested first acknowledgment and confirmation for each call,
  so calls don't all sound the same.
- **Fillers:** randomized («لحظة…»، «ثواني بس…») while a task saves. A soft typing sound
  plays during the save.
- **Voice:** "Yasmine – Human-Like Banking Agent" (`L10lEremDiJfPicq5CPh`), an Egyptian
  library voice built for phone agents, on `eleven_v4_turbo`. Chosen 2026-10-15 after
  Fatima on `eleven_v3_conversational` sounded Saudi or formal on calls; the expressive v3
  model doesn't keep a professional voice's accent. On a non-v3 model the prompt asks for
  feelings in words and no `[tags]`; set `ELEVENLABS_AGENT_TTS_MODEL=eleven_v3_conversational`
  to get laughs and sighs back (with the accent trade-off).
- **Dialect:** Cairene Egyptian only. The prompt lists Egyptian words to use and Gulf or
  formal words to avoid (زين، وش، أبغى، الحين، أبشر، حسنًا، سوف…).
- **Grammar:** female forms for herself, and the caller's gender (`caller_gender`, from
  `config/callers.json` + `people.json`) when speaking to them: male or unknown → «عامل
  إيه؟»، female → «عاملة إيه؟».
- **Personality:** punctuation for natural rhythm, Egyptian reactions («يا سلام!»،
  «معلش»), a little chat and at most one light joke per call, never slowing a task.
- **Recognition:** the team's names are sent to speech recognition as `asr.keywords`.
- **Task titles:** Monday titles are instructions to the assignee, cleaned on the server by
  `agent_tools/task_text.py` even if the model slips: «يشتري كاميرات بكرة» → «اشتري كاميرات
  بكرة»; «يتصل بيا» → «اتصل بـ<caller>»; «يتصل بيكي» → «اتصل بسارة». When repeating a task,
  Sarah flips the point of view («يتصل بيكي» → «هخلّيه يتصل بيا»). The caller's original
  words stay in the job transcript.
- **Keyboard sound:** while a task is saving, the bridge plays its own typing loop
  (`assets/typing_16k.pcm`, made with ElevenLabs sound effects). It's louder than the
  platform's tool sound and drops under her voice when she speaks. Set the level with
  `EL_BRIDGE_TYPING_VOLUME` (0 = off, 1 = normal, up to 3).
- **Temperature:** 0.6 for variety; people and tool arguments are still fixed by enums.

## Boards (Request tasks, Account Managers, Senior Account Managers)

`config/boards.json` (enabled with `MONDAY_BOARDS_FILE=config/boards.json`) lists the boards
Sarah can use:

| Key | Board | Assign column | Extra columns set |
|---|---|---|---|
| `requests` (usual) | Request tasks (`MONDAY_BOARD_ID`) | dropdown (`MONDAY_<NAME>_PERSON_ID`) | Task From |
| `account_managers` | Account Managers Tasks `5098517650` | People (`monday_users`) | Status = Ready To Start, Priority = Medium, Timeline |
| `senior_account_managers` | Senior Account Managers Tasks `5106176269` | People | the same |

How a board is chosen:
- **Board named:** if the caller names one («على بورد الأكاونت مانجرز»), the task goes there.
- **Home board:** otherwise it goes to the person's home board (`home_board`; Request tasks
  by default).
- **"ask":** a person marked "ask" (Amir) gets the question «أحطها لأمير على أنهي بورد؟»
  first. The server enforces this: `submit_task` with `board=auto` for Amir returns
  `need_board` and saves nothing.

New tasks on the Account Managers boards go to the **Ready To Start** group. The Timeline
comes from the task's words (بكرة، بعد بكرة، النهارده، يوم الخميس…, in Cairo time), or
today. People columns need Monday *user* ids (`monday_users`); Mohamed is Mohamed Mohsen.

## Everything else on Monday (the Monday assistant)

Besides taking tasks, Sarah can answer questions and make changes anywhere on Monday:
- «راجعيلي بورد الأكاونت مانجرز»
- «إيه اللي متأخر عند زياد؟»
- «تاسك الكاميرات وصلت لفين؟»
- «خلي تاسك الريل Done»
- «حطي update على بروجكت كذا»
- «انقلها لجروب Done»
- «أرشفيها»

How a request flows:
1. Sarah says a short filler («ثانية أبصلك») and calls the `monday_assistant` webhook with
   the request.
2. The api (`agent_tools/monday_assistant.py`) gives it to a stronger LLM, `gpt-4.1` by
   default. That LLM can use Monday tools (`connectors/monday_api.py`):
   - `get_board`, `get_items`, `search_items`, `get_item`
   - `update_item`, `create_item`, `add_update`, `move_item`, `archive_item`
   - a `graphql` escape hatch for anything else
3. The LLM sees a map of every board and user (refreshed every 5 minutes), so it usually
   answers in one to three steps.
4. Sarah reads back its short Egyptian answer. The keyboard sound plays while it works.
5. Follow-ups («والتانية؟»، «أيوه اعملها») work: each call keeps its own memory for
   30 minutes.

Safety:
- **Who can use it:** only callers whose extension maps to a person in
  `MONDAY_ASSISTANT_CALLERS` (default `mohamed`, ext 101). Anyone else gets
  `not_allowed`, and Sarah offers to take a task instead.
- **Nothing is deleted:** removing means archiving, which can be restored from Monday.
- **Blocked mutations:** the escape hatch refuses deletions and user, team, permission,
  webhook and board/group/workspace archive mutations.
- **Bulk changes:** the assistant asks before changing more than 3 items at once.

Settings:

| Variable | Meaning |
|---|---|
| `AGENT_API_KEY`, `AGENT_PROVIDER` | The LLM (OpenAI or OpenRouter key). Without a key, the tool isn't offered to Sarah. |
| `MONDAY_ASSISTANT_MODEL` | Default `gpt-4.1` (or `openai/gpt-4.1` on OpenRouter). |
| `MONDAY_ASSISTANT_CALLERS` | Comma-separated person keys, default `mohamed`. |
| `MONDAY_ASSISTANT_TIMEOUT_SECONDS` | Default 50. Sarah's webhook waits this long plus 10 s. |

After changing these settings, restart the `api` service and run `el-agent sync`, so Sarah
gets the tool and the prompt section.

## Sarah calls people (prank mode)

On a normal call you can ask «اتصلي بأحمد واعملي فيه مقلب». What happens:

1. Sarah calls the `call_person` tool. The API (`agent_tools/outbound.py`) only accepts team
   members with an extension in `config/callers.json`, so outside numbers are impossible.
   It rate-limits (each person at most once per 5 minutes, each requester 3 per 10
   minutes) and queues the request in Redis.
2. The bridge's dialer rings `PJSIP/<extension>` through ARI on 127.0.0.1, showing caller ID
   `AGENT_OUTBOUND_CALLER_ID` ("Sarah <777>").
3. When it's answered, the dialplan connects the call to the bridge, which starts Sarah in
   **prank mode**: «ألو يا أحمد، عايزاك في حاجة مهمة.». When the person asks what she wants,
   she calls the **client tool `prank_reveal`**, which the bridge handles itself:
   - it mutes her and plays a pre-rendered punchline in her voice: «خَدتَك عليه!» (or
     «خَدتِك» to a woman) followed by hard laughter;
   - it hangs up when the laughter ends.

   She never says who asked for the prank. If the person sounds busy or upset, she says
   it was a joke and apologises. A prank call lasts at most 90 s.

   The clips live in `data/prank/<voice>-<m|f>-<n>.pcm`. `el-agent sync` renders them once
   per voice with `eleven_v3`, because the fast model can't laugh. The vowel is marked on
   purpose: without it the voice read «خدتِك».

Turn it on with `AGENT_OUTBOUND_ENABLED=true`. Then re-run `scripts/pbx-elevenlabs-route.sh
apply`, which enables HTTP on loopback, adds the ARI user from `ASTERISK_ARI_USERNAME` /
`ASTERISK_ARI_PASSWORD` and the `[voice-monday-el-outbound]` context. Also restart
`el-bridge` and run `el-agent sync`. Set it to `false` and re-apply to turn it off again.

## Who does what

**ElevenLabs** handles:
- listening, with streaming Scribe;
- end-of-turn detection;
- interruptions;
- the AI that understands Egyptian Arabic and decides what to do;
- her voice.

**This server** stays the source of truth for tasks. `src/voice_agent/agent_tools/`:
- **Accepts only known people:** those that have a `MONDAY_<NAME>_PERSON_ID`.
- **Holds every task for `AGENT_GRACE_SECONDS`** (2) before saving it:
  - `mode=replace_last` within that time replaces it;
  - `cancel_last` drops it;
  - another `new` task is simply added.
- **Saves the job durably,** then waits up to `AGENT_DONE_WAIT_SECONDS` (5) for the worker.
- **Answers honestly:** `created` only when the Monday item exists; otherwise `queued` or `failed`.
- **Prevents duplicates:** a retried webhook or a repeated tool call joins the same draft (same conversation + person + words), so it never makes a second item.

The tool endpoints exist only when `AGENT_TOOL_SECRET` is set, and need `Authorization: Bearer <secret>`. Requests that arrive through Cloudflare (they carry `CF-Connecting-IP`) can reach **only** `/agent/tools/*`. `/simulate`, `/jobs` and `/health` stay private even if the tunnel's path rule is wrong.

## Setup

1. **ElevenLabs:** a paid plan (Creator or higher). It's needed for phone agents and for the "Fatima" library voice `I3u6waC588j43py1kDN2`. Put `ELEVENLABS_API_KEY` in `.env`.
2. **Secrets in the server `.env`.** Generate these on the server and never paste them anywhere else:

   ```bash
   echo "AGENT_TOOL_SECRET=$(openssl rand -hex 32)"
   echo "ELEVENLABS_SIP_USERNAME=pbx$(openssl rand -hex 4)"
   echo "ELEVENLABS_SIP_PASSWORD=$(openssl rand -hex 24)"
   ```

3. **Cloudflare Tunnel.** In Cloudflare Zero Trust:
   - Go to Networks → Tunnels → Create a tunnel (cloudflared), and copy the token into `CLOUDFLARE_TUNNEL_TOKEN`.
   - Add a public hostname, e.g. `voice-tools.<your-domain>`, with service `http://127.0.0.1:8000` and path `^/agent/tools/`.
   - Set `AGENT_PUBLIC_URL=https://voice-tools.<your-domain>`.

   Then start the tunnel:

   ```bash
   sudo docker compose --profile agent up -d cloudflared
   ```

4. **Create the agent, its tools and the SIP number.** Run it again whenever `people.json`, the Monday ids or the prompt change:

   ```bash
   sudo docker compose run --rm --no-deps api el-agent sync --dry-run   # inspect payloads
   sudo docker compose run --rm --no-deps api el-agent sync
   ```

   It creates or updates (never duplicates):
   - the workspace secret;
   - the `submit_task` and `cancel_last` webhook tools;
   - the agent;
   - the SIP trunk number `ELEVENLABS_SIP_NUMBER` (778).

   The ids are kept in `data/elevenlabs-state.json`; it holds no secret values.

5. **PBX route** (additive, revertible, run on the PBX):

   ```bash
   sudo scripts/pbx-elevenlabs-route.sh apply     # … revert / verify
   # AGENT_PBX_EXTENSIONS="777 778" in .env routes several extensions to Sarah
   ```

   It adds:
   - a TLS transport to `sip.rtc.elevenlabs.io:5061`, with NAT settings for the office public IP;
   - the `elevenlabs` trunk: digest auth, G.722/ulaw, SRTP when offered;
   - extension 778, which sends the caller's extension as `X-CALLER-ID`.

   The existing 777 assistant is untouched.

6. **Call 778.**

## Tuning latency

Per-turn latency is visible in the ElevenLabs conversation history; our side logs `agent_tool.received / .committed / .result` with timings. The knobs:

| Setting | Default | Effect |
|---|---|---|
| `turn.turn_eagerness` | `eager` | Answers sooner after you stop talking |
| `turn.speculative_turn` | `true` | Starts thinking during the pause (more LLM cost) |
| `ELEVENLABS_AGENT_LLM` | `qwen3-30b-a3b` | Hosted by ElevenLabs. Measured on live calls 2026-10-15: ack 0.8–1.35 s after you stop, «Done» LLM 0.15 s. gemini-2.5-flash-lite: 1.0–1.5 s / 0.3–0.4 s; gemini-3.x-flash-lite add `[happy]` tags and are slower |
| `ELEVENLABS_AGENT_TTS_MODEL` | `eleven_v4_turbo` | Lowest-latency voice model |
| `ELEVENLABS_TOOL_EXECUTION_MODE` | `post_tool_speech` | Window starts after her acknowledgment; `immediate` starts it while she speaks |

**What to expect:**
- **Typical speed:** 0.6–1.0 s from the end of your sentence to her first word.
- **About 0.4 s** needs the settings above and a good network path.
- Monday never delays the acknowledgment, because she speaks before the tool runs.

## No-account tunnel (testing)

`scripts/agent-quick-tunnel.sh` starts a Cloudflare quick tunnel (random
`https://<words>.trycloudflare.com`, no account), checks that only `/agent/tools/*` is
reachable through it, and re-runs `el-agent sync` so the tools point at the new URL. Run it
again after any restart of the `cloudflared-quick` container.

## PBX notes from the first deployment (2026-10-15)

- **SIP identifiers are global at ElevenLabs.** `778` was already taken by another
  customer, so `ELEVENLABS_SIP_NUMBER` is a random `vmonday…` string. Callers still dial 778.
- **Escape `;` in Asterisk configs.** It starts a comment, so the contact needs
  `sip:sip.rtc.elevenlabs.io:5061\;transport=tls`.
- **A new PJSIP TLS transport needs an Asterisk restart.** Without it, the SIP DNS resolver
  doesn't know the transport and resolves nothing (`No resolution queries`,
  `PJLIB_UTIL_EDNSNOANSWERREC`). Use `core restart when convenient`.
- **One-way audio behind a multi-line load balancer.** The office reaches the internet
  through `192.168.12.1`, which spreads traffic over several lines with different public IPs.
  ElevenLabs hears the caller, but its audio never reaches the PBX. The fix is on the
  network: send all PBX traffic over one line, forward UDP 10000–10100 to 192.168.0.118 on
  every NAT layer of that line, and set `PBX_PUBLIC_IP` to that line's address. Ideally get a
  static IP from the ISP.

## When Sarah is unavailable

If the agent can't start, e.g. ElevenLabs answers `quota_exceeded` when the account is out
of credits, the bridge closes within a second. Extension 778 then plays the busy tone for
4 s instead of dropping silently. Check `el_bridge.call_failed` in the `el-bridge` logs.

Credits: agent minutes are expensive. On 2026-10-15 the free plan's 10,000 credits were
used up by about 11.5 minutes of calls (mostly test calls), and they reset monthly.
Extension 777 also uses ElevenLabs for its voice, so it goes silent too when credits run
out. Watch usage with `GET /v1/user/subscription`, and keep robot test calls short and few.

## Things to confirm on the first real calls

- **The grace window during a pending tool call.** Whether the platform accepts a new user turn, and a second tool call, while `submit_task` is still waiting. If it doesn't, try `ELEVENLABS_TOOL_EXECUTION_MODE=async`. Either way the server prevents wrong or duplicate items.
- **One-way audio** (you hear her, she can't hear you, or the reverse). Forward UDP 10000–10100 (`rtp.conf`) from the router to the PBX.
- **A changing public IP.** If the office's address changes, re-run the PBX script with `PBX_PUBLIC_IP` set.
