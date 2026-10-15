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
- **Voice:** "Fatima – Expressive Egyptian Voice" (`I3u6waC588j43py1kDN2`, paid plan) on
  `eleven_v3_conversational` with expressive mode. She may use the audio tags
  [laughs] [chuckles] [sighs] [exhales] [excited], sparingly.
- **Dialect:** Cairene Egyptian only. The prompt lists Egyptian words to use and Gulf or
  formal words to avoid (زين، وش، أبغى، الحين، أبشر، حسنًا، سوف…).
- **Grammar:** female forms for herself, and the caller's gender (`caller_gender`, from
  `config/callers.json` + `people.json`) when speaking to them: male or unknown → «عامل
  إيه؟»، female → «عاملة إيه؟».
- **Personality:** punctuation for natural rhythm, Egyptian reactions («يا سلام!»،
  «معلش»), a little chat and at most one light joke per call, never slowing a task.
- **Recognition:** the team's names are sent to speech recognition as `asr.keywords`.
- **Temperature:** 0.6 for variety; people and tool arguments are still fixed by enums.

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
