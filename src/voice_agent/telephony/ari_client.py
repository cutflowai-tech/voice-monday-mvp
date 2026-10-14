"""Minimal Asterisk REST Interface (ARI) client.

Endpoints used (docs.asterisk.org, ARI reference):

* ``POST   /ari/channels/{id}/answer``
* ``DELETE /ari/channels/{id}?reason=normal``
* ``POST   /ari/channels/{id}/play/{playbackId}?media=sound:...``
* ``POST   /ari/channels/{id}/record?name=&format=&maxDurationSeconds=&maxSilenceSeconds=``
* ``GET    /ari/recordings/stored/{name}/file``   (Asterisk >= 14)
* ``DELETE /ari/recordings/stored/{name}``
* ``POST   /ari/recordings/live/{name}/stop``
* ``GET    /ari/asterisk/info``
* WebSocket ``/ari/events?app={app}&api_key={user}:{password}``

Events consumed: StasisStart, StasisEnd, ChannelHangupRequest, ChannelDestroyed,
RecordingFinished, RecordingFailed, PlaybackFinished.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import httpx

from voice_agent.logging_setup import get_logger

log = get_logger(__name__)


class AriError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _bool(value: bool) -> str:
    return "true" if value else "false"


class AriClient:
    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        app: str,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.app = app
        self._username = username
        self._password = password
        self._http = httpx.AsyncClient(
            base_url=f"{self.base_url}/ari",
            auth=(username, password),
            timeout=httpx.Timeout(timeout_seconds, connect=5.0),
            transport=transport,
        )

    # ------------------------------------------------------------------ REST
    async def _request(
        self, method: str, path: str, *, params: dict | None = None, ok_404: bool = False
    ) -> httpx.Response:
        try:
            response = await self._http.request(method, path, params=params)
        except httpx.HTTPError as exc:
            raise AriError(f"ARI {method} {path} failed: {exc!r}") from exc
        if response.status_code == 404 and ok_404:
            return response
        if response.status_code >= 400:
            raise AriError(
                f"ARI {method} {path} -> {response.status_code}: {response.text[:200]}",
                status=response.status_code,
            )
        return response

    async def answer(self, channel_id: str) -> None:
        await self._request("POST", f"/channels/{quote(channel_id)}/answer")

    async def get_channel(self, channel_id: str) -> dict[str, Any]:
        return (await self._request("GET", f"/channels/{quote(channel_id)}")).json()

    async def hangup(self, channel_id: str, reason: str = "normal") -> None:
        await self._request(
            "DELETE", f"/channels/{quote(channel_id)}", params={"reason": reason}, ok_404=True
        )

    async def play(self, channel_id: str, media: str, playback_id: str) -> None:
        await self._request(
            "POST",
            f"/channels/{quote(channel_id)}/play/{quote(playback_id)}",
            params={"media": media},
        )

    async def stop_playback(self, playback_id: str) -> None:
        await self._request("DELETE", f"/playbacks/{quote(playback_id)}", ok_404=True)

    async def record(
        self,
        channel_id: str,
        name: str,
        *,
        fmt: str = "wav",
        max_duration_seconds: int = 20,
        max_silence_seconds: int = 2,
        if_exists: str = "overwrite",
        beep: bool = False,
        terminate_on: str = "#",
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            f"/channels/{quote(channel_id)}/record",
            params={
                "name": name,
                "format": fmt,
                "maxDurationSeconds": max_duration_seconds,
                "maxSilenceSeconds": max_silence_seconds,
                "ifExists": if_exists,
                "beep": _bool(beep),
                "terminateOn": terminate_on,
            },
        )
        return response.json() if response.content else {}

    async def stop_recording(self, name: str) -> None:
        await self._request("POST", f"/recordings/live/{quote(name)}/stop", ok_404=True)

    async def get_stored_recording(self, name: str) -> bytes:
        response = await self._request("GET", f"/recordings/stored/{quote(name)}/file")
        return response.content

    async def delete_stored_recording(self, name: str) -> None:
        await self._request("DELETE", f"/recordings/stored/{quote(name)}", ok_404=True)

    async def info(self) -> dict[str, Any]:
        return (await self._request("GET", "/asterisk/info")).json()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ events
    def events_url(self) -> str:
        parts = urlsplit(self.base_url)
        scheme = "wss" if parts.scheme == "https" else "ws"
        query = urlencode(
            {
                "app": self.app,
                "api_key": f"{self._username}:{self._password}",
                "subscribeAll": "false",
            }
        )
        return urlunsplit((scheme, parts.netloc, f"{parts.path}/ari/events", query, ""))

    async def events(
        self, connect: Callable[..., Any] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield ARI events forever, reconnecting with backoff on failures."""
        if connect is None:
            from websockets.asyncio.client import connect as ws_connect

            connect = ws_connect
        backoff = 1.0
        while True:
            try:
                async with connect(self.events_url(), ping_interval=20, ping_timeout=20) as ws:
                    log.info("ari.connected", app=self.app, url=self.base_url)
                    backoff = 1.0
                    async for raw in ws:
                        try:
                            yield json.loads(raw)
                        except json.JSONDecodeError:
                            log.warning("ari.bad_event", raw=str(raw)[:200])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("ari.disconnected", error=repr(exc), retry_in=backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


class AriEventBus:
    """Routes ARI events to the call sessions waiting for them."""

    def __init__(self) -> None:
        self._waiters: dict[tuple[str, str], asyncio.Future[dict[str, Any]]] = {}
        self._channel_listeners: dict[str, Callable[[dict[str, Any]], None]] = {}

    def expect(self, kind: str, ident: str) -> asyncio.Future[dict[str, Any]]:
        """Register interest *before* issuing the ARI command to avoid races."""
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiters[(kind, ident)] = future
        return future

    def discard(self, kind: str, ident: str) -> None:
        future = self._waiters.pop((kind, ident), None)
        if future and not future.done():
            future.cancel()

    def on_channel(self, channel_id: str, listener: Callable[[dict[str, Any]], None]) -> None:
        self._channel_listeners[channel_id] = listener

    def remove_channel(self, channel_id: str) -> None:
        self._channel_listeners.pop(channel_id, None)

    def dispatch(self, event: dict[str, Any]) -> None:
        etype = event.get("type", "")
        key: tuple[str, str] | None = None
        if etype in ("RecordingFinished", "RecordingFailed"):
            key = ("recording", (event.get("recording") or {}).get("name", ""))
        elif etype == "PlaybackFinished":
            key = ("playback", (event.get("playback") or {}).get("id", ""))
        if key:
            future = self._waiters.pop(key, None)
            if future and not future.done():
                future.set_result(event)
            return
        channel_id = (event.get("channel") or {}).get("id")
        if channel_id and channel_id in self._channel_listeners:
            self._channel_listeners[channel_id](event)
