# Fast AI assistant: ElevenLabs Agent on extension 778

```
Phone → Asterisk ext 778 ─SIP/TLS + X-CALLER-ID─▶ ElevenLabs Agent (listening · AI · turn-taking · voice)
                                                     │ says the acknowledgment, then calls submit_task
                                                     ▼ HTTPS via Cloudflare Tunnel (bearer secret)
                         api: /agent/tools/submit_task → 2 s grace window → job → worker → Monday
                                                     │ {"status": "created"}
                                                     ▼
                                     Agent: «Done، خلاص حطيت التاسك»
```

The conversation the user hears:

```
Agent: ألو
You:   عاوزك تحطي تاسك لأحمد يجيب الكهربائي بكرة
Agent: تمام، هقول لأحمد يجيب الكهربائي بكرة
       (2 s: a correction replaces the task, an extra request adds one, «الغيها» cancels it)
Agent: Done، خلاص حطيت التاسك. حاجة تانية؟
```

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
| `ELEVENLABS_AGENT_LLM` | `gemini-2.5-flash` | Faster model → faster first word |
| `ELEVENLABS_AGENT_TTS_MODEL` | `eleven_v4_turbo` | Lowest-latency voice model |
| `ELEVENLABS_TOOL_EXECUTION_MODE` | `post_tool_speech` | Window starts after her acknowledgment; `immediate` starts it while she speaks |

**What to expect:**
- **Typical speed:** 0.6–1.0 s from the end of your sentence to her first word.
- **About 0.4 s** needs the settings above and a good network path.
- Monday never delays the acknowledgment, because she speaks before the tool runs.

## Things to confirm on the first real calls

- **The grace window during a pending tool call.** Whether the platform accepts a new user turn, and a second tool call, while `submit_task` is still waiting. If it doesn't, try `ELEVENLABS_TOOL_EXECUTION_MODE=async`. Either way the server prevents wrong or duplicate items.
- **One-way audio** (you hear her, she can't hear you, or the reverse). Forward UDP 10000–10100 (`rtp.conf`) from the router to the PBX.
- **A changing public IP.** If the office's address changes, re-run the PBX script with `PBX_PUBLIC_IP` set.
