# Assistant extension 777

The observed host on 2026-10-14 is `wcds@192.168.0.118`, running plain Asterisk
22.5.2 and Docker Compose. Its calling context is `office-internal`; the prior
assistant route is 7999 in `voice-monday-test`. No `Answer()` or playback is in
the assistant dialplan. Extension 777 was unused when inspected.

The ARI service warms Whisper before connecting to the Stasis application. For
each call it checks the channel while it rings, then answers and starts recording
without a greeting or beep. Asterisk's channel recording captures incoming audio
only. The short answer-to-record transition remains to be measured with a handset.

This host binds ARI and Redis only to `127.0.0.1`. Copy
`deploy/asterisk-host.override.yml` to `docker-compose.override.yml` on the host;
it runs only the ARI container with host networking, keeping its existing access
to Asterisk and the local Redis port. All subsequent Compose commands must run
from the project directory so this override is loaded automatically.

`CALLER_DIRECTORY_FILE` points to a JSON object mapping caller numbers or internal
extensions to keys in `people.json`, for example `{"101": "ahmed"}` **only if the
owner has verified that assignment**. The default policy records an unknown
requester; `UNKNOWN_CALLER_POLICY=reject` rejects unmatched callers. Caller ID is
attribution rather than authentication. The assignee is always resolved separately.

`AGENT_API_KEY` enables a strict structured decision using the provider selected by
`AGENT_PROVIDER` (`openai` Responses or `openrouter` chat completions) and `AGENT_MODEL`.
Without it the deterministic parser is used.
The model can only select a conversation action, directory key, and task title.
Application code validates and persists the job, and the worker alone calls Monday.

For this host, keep the old 7999 route until a real call to 777 succeeds. Backups
of the prior code, `.env`, and dialplan are stored privately under
`/home/wcds/voice-monday-backups/`. The `.env` backup must never be committed.

Rollback: stop only the `ari` Compose service, restore the matching code and `.env`
backup, restore `extensions.conf` and `extensions_voice_monday.conf` from the same
dated directory, run `sudo asterisk -rx 'dialplan reload'`, then start the prior
`ari` service. Do not stop Redis or the worker; durable jobs continue processing.
