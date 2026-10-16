"""Sarah's Monday assistant: anything about Monday that isn't a quick task.

Sarah (the ElevenLabs agent) stays fast and light; when the caller asks about Monday
(«راجعيلي بورد الأكاونت مانجرز»، «إيه اللي متأخر عند زياد؟»، «خلي تاسك الكاميرات Done»)
she says a short filler and calls the ``monday_assistant`` webhook with the request. Here
a stronger LLM with Monday tools (``connectors.monday_api.MondayTools``) works it out and
returns a short Egyptian Arabic answer that Sarah reads back.

* Access: only callers whose extension maps to a person in ``MONDAY_ASSISTANT_CALLERS``
  (the Monday API token can see and change everything).
* Memory: the exchange is kept per conversation for 30 minutes, so follow-ups («والتاني؟»،
  «أيوه اعملها») work.
* The LLM speaks OpenAI-compatible ``/chat/completions`` with tools (OpenAI or OpenRouter,
  from ``AGENT_API_KEY`` / ``AGENT_PROVIDER``).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from voice_agent.connectors.monday_api import MondayAPIError, MondayTools
from voice_agent.domain.callers import CallerDirectory
from voice_agent.domain.dates import today_in_cairo
from voice_agent.domain.people import PeopleDirectory
from voice_agent.logging_setup import get_logger

log = get_logger(__name__)

MEMORY_TTL_SECONDS = 1800
WARM_EVERY_SECONDS = 60  # board items; inside the tools' 2-minute items cache
MAP_EVERY_ROUNDS = 4  # the board/user map every 4 minutes
MEMORY_MAX_CHARS = 60_000  # oldest exchanges are dropped beyond this
WEEKDAYS_AR = ("الاتنين", "التلات", "الأربع", "الخميس", "الجمعة", "السبت", "الحد")

INSTRUCTIONS = """\
You are the Monday.com back office of Sarah (سارة), the phone assistant of Waset Co.
{caller} is on the phone with Sarah. Sarah passes you their request; your final answer is
read aloud by Sarah, so it is speech, not text.

Today is {today} ({weekday}), Cairo time.

# What you can do
Everything on the company's Monday account through your tools: read boards and items,
search, summarise, find what's late or stuck, who has what, and change things (status,
dates, owners, names, updates/comments, moving items, creating items or groups, archiving).

# How to work
- Pick the board from the map below. Board names are English; the caller says them in
  Egyptian Arabic («الأكاونت مانجرز» = Account Managers Tasks, «الريكويستس» = Request
  tasks). A person may have their own board (e.g. "Ahmed Tasks"); a person's tasks can
  also be on shared boards (Request tasks, Account Managers Tasks…): look at those too
  before saying they have nothing.
- A person's tasks («إيه اللي عند أحمد؟»، «إيه اللي متأخر عند مايكل؟»، «اللي محطوط لأحمد
  النهاردة») → ONE person_items call with their Monday name from the team list. It
  already searched every board, dropped finished items and worked out what's late or due
  today: use its counts as they are, never recount or redo the dates. «النهاردة» /
  «دلوقتي» in such a question means "currently": answer with the open count, then what's
  late and what's due today. For «متأخر» answer with the late ones.
- Call get_board before filtering or changing a board, to get exact column ids and labels.
- Before saying "nothing found", double-check: read the items without filters, or look on
  the other likely boards. A filter that returns 0 is usually a wrong filter.
- Before a change, be sure which item it is. If several items match, ask which one (name
  them briefly). Just do clear single changes and say what you did.
- Ask first («أعملها لكل الـ 7؟») before changing more than 3 items at once.
- Nothing is ever deleted: to remove an item, archive it and say «أرشفتها».
- Never invent anything. If you can't find it, say so and say which boards you looked at.
- get_items returns at most 200 items; "more": true means you saw only part. Never state
  totals, "most" or "all" from a partial page: say what you actually counted. The board
  map's item counts are exact totals.

# The team (as the caller names them → Monday)
{team}

# Your answer (spoken by Sarah)
- Cairene Egyptian Arabic, like a colleague on the phone: «عند أحمد 3 تاسكات: الكاميرات،
  والكهربائي، وتسليم الفيديو.» Never formal Arabic. No vowel marks.
- SHORT: one or two sentences, under 35 words. The caller interrupts long answers. No
  lists, bullets, markdown, emojis, ids, links or quotation marks.
- Board, item and people names as they are written on Monday.
- Many results: say how many and the two or three most important, then «أقولك الباقي؟».
- Dates naturally («بكرة»، «يوم الخميس»، «16 أكتوبر»).
- If you need something from the caller, end with one short question.

# Map of the account
{workspace}
"""


class MondayAssistant:
    def __init__(
        self,
        *,
        tools: MondayTools,
        api_key: str,
        model: str,
        provider: str = "openai",
        allowed: set[str],
        callers: CallerDirectory,
        directory: PeopleDirectory,
        monday_users: dict[str, int] | None = None,
        activity=None,
        timeout_seconds: float = 50.0,
        max_steps: int = 10,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock=time.monotonic,
    ):
        self.tools = tools
        self.model = model
        self.allowed = allowed
        self.callers = callers
        self.directory = directory
        self.monday_users = dict(monday_users or {})  # person key -> Monday user id
        self.activity = activity  # SpeechActivity: the bridge plays typing while we work
        self.timeout_seconds = timeout_seconds
        self.max_steps = max_steps
        self._key = api_key
        self._clock = clock
        self._memory: dict[str, tuple[float, list[dict]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._client = httpx.AsyncClient(
            base_url=(
                base_url
                or (
                    "https://openrouter.ai/api/v1"
                    if provider == "openrouter"
                    else "https://api.openai.com/v1"
                )
            ).rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds, connect=5.0),
            transport=transport,
        )

    def start(self) -> None:
        """Keep the board/user map fresh in the background, so no call waits for it."""
        self._warmer = asyncio.create_task(self._keep_warm())

    async def _keep_warm(self) -> None:
        rounds = 0
        while True:
            try:
                await self.tools.workspace_map(refresh=rounds % MAP_EVERY_ROUNDS == 0)
                await self.tools.refresh_items()
            except Exception as exc:  # retried on the next round
                log.warning("monday_assistant.warm_failed", error=type(exc).__name__)
            rounds += 1
            await asyncio.sleep(WARM_EVERY_SECONDS)

    async def aclose(self) -> None:
        warmer = getattr(self, "_warmer", None)
        if warmer is not None:
            warmer.cancel()
        await self._client.aclose()
        await self.tools.api.aclose()

    # ------------------------------------------------------------------ public
    async def ask(self, *, conversation_id: str, request: str, caller_id: str | None) -> dict:
        caller_key = self.callers.lookup(caller_id or "")
        if caller_key not in self.allowed:
            log.info("monday_assistant.not_allowed", caller=caller_key or "unknown")
            return {"status": "not_allowed"}
        started = self._clock()
        lock = self._locks.setdefault(conversation_id, asyncio.Lock())
        async with lock:
            await self._signal(conversation_id, True)
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    answer, steps = await self._answer(conversation_id, request, caller_key)
            except TimeoutError:
                log.warning("monday_assistant.timeout", conversation_id=conversation_id)
                return {"status": "failed", "reason": "timeout"}
            except (httpx.HTTPError, MondayAPIError, ValueError, KeyError) as exc:
                log.warning(
                    "monday_assistant.failed",
                    conversation_id=conversation_id,
                    error=type(exc).__name__,
                    detail=str(exc)[:200],
                )
                return {"status": "failed"}
            finally:
                await self._signal(conversation_id, False)
        log.info(
            "monday_assistant.answered",
            conversation_id=conversation_id,
            steps=steps,
            seconds=round(self._clock() - started, 2),
        )
        return {"status": "ok", "answer": answer}

    # ------------------------------------------------------------------ internals
    async def _answer(self, conversation_id: str, request: str, caller_key: str) -> tuple[str, int]:
        history = self._history(conversation_id)
        messages = [
            {"role": "system", "content": await self._instructions(caller_key)},
            *history,
            {"role": "user", "content": request},
        ]
        new = [messages[-1]]
        tools = [{"type": "function", "function": schema} for schema in MondayTools.SCHEMAS]
        for step in range(1, self.max_steps + 1):
            final_step = step == self.max_steps
            message = await self._complete(messages, tools, final=final_step)
            calls = message.get("tool_calls") or []
            if not calls or final_step:
                answer = (message.get("content") or "").strip()
                if not answer:
                    raise ValueError("empty answer")
                new.append({"role": "assistant", "content": answer})
                self._remember(conversation_id, history + new)
                return answer, step
            assistant = {
                "role": "assistant",
                "content": message.get("content"),
                "tool_calls": calls,
            }
            results = await asyncio.gather(*(self._run_tool(c) for c in calls))
            outputs = [
                {"role": "tool", "tool_call_id": c["id"], "content": out}
                for c, out in zip(calls, results, strict=True)
            ]
            messages += [assistant, *outputs]
            new += [assistant, *outputs]
        raise ValueError("no answer")  # pragma: no cover (the last step always answers)

    async def _run_tool(self, call: dict) -> str:
        function = call.get("function") or {}
        name = function.get("name", "")
        try:
            args = json.loads(function.get("arguments") or "{}")
        except ValueError:
            return json.dumps({"error": "arguments must be JSON"})
        log.info(
            "monday_assistant.tool", tool=name, args=json.dumps(args, ensure_ascii=False)[:300]
        )
        return await self.tools.call(name, args if isinstance(args, dict) else {})

    async def _complete(self, messages: list[dict], tools: list[dict], *, final: bool) -> dict:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "none" if final else "auto",
            "temperature": 0.2,
        }
        response = await self._client.post(
            "/chat/completions", headers={"Authorization": f"Bearer {self._key}"}, json=body
        )
        response.raise_for_status()
        choices = response.json().get("choices") or []
        if not choices:
            raise ValueError("LLM returned no choices")
        return choices[0].get("message") or {}

    async def _instructions(self, caller_key: str) -> str:
        caller = self.directory.get(caller_key)
        names = {str(u["id"]): u["name"] for u in await self.tools.users()}
        lines = []
        for p in self.directory:
            line = f"- {p.name_ar} = {p.name_en}"
            if p.aliases:
                line += f" (also {', '.join(p.aliases)})"
            uid = self.monday_users.get(p.key)
            if uid:
                line += f": on Monday «{names.get(str(uid), '?')}» (user id {uid})"
            lines.append(line)
        team = "\n".join(lines)
        today = today_in_cairo()
        return INSTRUCTIONS.format(
            caller=f"{caller.name_en} ({caller.name_ar})" if caller else "A team member",
            today=today.isoformat(),
            weekday=WEEKDAYS_AR[today.weekday()],
            team=team,
            workspace=await self.tools.workspace_map(),
        )

    def _history(self, conversation_id: str) -> list[dict]:
        now = self._clock()
        for key in [k for k, (ts, _) in self._memory.items() if now - ts > MEMORY_TTL_SECONDS]:
            self._memory.pop(key, None)
            self._locks.pop(key, None)
        return list(self._memory.get(conversation_id, (now, []))[1])

    def _remember(self, conversation_id: str, messages: list[dict]) -> None:
        # drop whole exchanges (from a user message to the next) until it fits
        while len(json.dumps(messages, ensure_ascii=False)) > MEMORY_MAX_CHARS:
            nxt = next((i for i, m in enumerate(messages[1:], 1) if m["role"] == "user"), None)
            if nxt is None:
                break
            messages = messages[nxt:]
        self._memory[conversation_id] = (self._clock(), messages)

    async def _signal(self, conversation_id: str, on: bool) -> None:
        if self.activity is not None and hasattr(self.activity, "set_saving"):
            try:
                await self.activity.set_saving(conversation_id, on)
            except Exception:
                log.warning("monday_assistant.signal_failed", conversation_id=conversation_id)
