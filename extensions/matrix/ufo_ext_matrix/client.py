"""The client-server API calls the surface makes, as the bot user.

The access token rides the `Authorization` header and nothing else — never a query string, an
exception message, or a `repr` — so no log line the surface writes can carry it. A failure names the
endpoint, the HTTP status, and the Matrix `errcode`; the response body is the homeserver's text and
is not repeated."""

from collections.abc import Mapping, Sequence
from types import TracebackType
from typing import Any, Self
from urllib.parse import quote

import httpx

from ufo_ext_matrix.events import BACKFILL_FILTER, MESSAGE_TYPE, SYNC_FILTER, media_parts

CLIENT_PATH = "/_matrix/client/v3"
MEDIA_PATH = "/_matrix/media/v3"
AUTHENTICATED_MEDIA_PATH = "/_matrix/client/v1/media"
BACKFILL_PAGE = 100
SYNC_TIMEOUT_MS = 30_000
REQUEST_TIMEOUT_SECONDS = 20.0


class MatrixError(Exception):
    """A homeserver answer other than success. `retry_after_ms` is the homeserver's own ask on a
    rate limit, so a retry waits as long as it was told to."""

    def __init__(
        self, endpoint: str, status: int, errcode: str | None, retry_after_ms: int | None
    ) -> None:
        self.endpoint = endpoint
        self.status = status
        self.errcode = errcode
        self.retry_after_ms = retry_after_ms
        super().__init__(f"{endpoint} answered {status} {errcode or ''}".rstrip())

    @property
    def unauthorized(self) -> bool:
        return self.status == 401 or self.errcode == "M_UNKNOWN_TOKEN"


class MatrixClient:
    """One bot user's session on one homeserver. `transport` is the seam a test hands a fake
    homeserver through; a deploy leaves it unset."""

    def __init__(
        self,
        homeserver: str,
        access_token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._root = homeserver.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self._root + CLIENT_PATH,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS + SYNC_TIMEOUT_MS / 1000),
            transport=transport,
        )

    def __repr__(self) -> str:
        return f"MatrixClient({self._http.base_url!s})"

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._http.aclose()

    async def whoami(self) -> str:
        return (await self.identity())[0]

    async def identity(self) -> tuple[str, str | None]:
        """The user the access token belongs to, and the device it is bound to — None for a token
        bound to no device."""
        answer = await self._call("GET", "/account/whoami", "whoami")
        user_id = answer.get("user_id")
        if not isinstance(user_id, str):
            raise MatrixError("whoami", 200, "M_BAD_JSON", None)
        device_id = answer.get("device_id")
        return user_id, device_id if isinstance(device_id, str) and device_id else None

    async def sync(self, since: str | None) -> Mapping[str, Any]:
        """One long poll. The first sync of a stream (`since` None) returns at once: it only fixes
        where the stream stands."""
        params: dict[str, str | int] = {
            "filter": SYNC_FILTER,
            "timeout": 0 if since is None else SYNC_TIMEOUT_MS,
        }
        if since is not None:
            params["since"] = since
        return await self._call("GET", "/sync", "sync", params=params)

    async def send_message(self, room_id: str, txn_id: str, content: Mapping[str, Any]) -> str:
        """Send one `m.room.message` under a caller-chosen transaction id and return its event id.
        The homeserver answers a repeated transaction id with the event it already created."""
        return await self.send_event(room_id, MESSAGE_TYPE, txn_id, content)

    async def send_event(
        self, room_id: str, event_type: str, txn_id: str, content: Mapping[str, Any]
    ) -> str:
        """Send one room event of any type under a caller-chosen transaction id and return its event
        id — a message, the edit that rewrites one, or the poll a question is tapped through."""
        room = quote(room_id, safe="")
        path = f"/rooms/{room}/send/{quote(event_type, safe='')}/{quote(txn_id, safe='')}"
        answer = await self._call("PUT", path, "send", json=content)
        event_id = answer.get("event_id")
        if not isinstance(event_id, str):
            raise MatrixError("send", 200, "M_BAD_JSON", None)
        return event_id

    async def typing(self, room_id: str, user_id: str, active: bool, timeout_ms: int) -> None:
        """Say the bot is typing, or has stopped. The homeserver ends an indicator of its own accord
        once `timeout_ms` passes, so a stream that drops stops the room typing without being
        told."""
        path = f"/rooms/{quote(room_id, safe='')}/typing/{quote(user_id, safe='')}"
        body: dict[str, Any] = {"typing": active}
        if active:
            body["timeout"] = timeout_ms
        await self._call("PUT", path, "typing", json=body)

    async def read_receipt(self, room_id: str, event_id: str) -> None:
        """Mark one event read, which is what a room shows for a message the bot has taken up."""
        room = quote(room_id, safe="")
        path = f"/rooms/{room}/receipt/m.read/{quote(event_id, safe='')}"
        await self._call("POST", path, "receipt", json={})

    async def display_name(self, user_id: str) -> str:
        """The name a room shows for a user, which is what a member types instead of an MXID. A
        profile that names none is the empty string."""
        path = f"/profile/{quote(user_id, safe='')}/displayname"
        answer = await self._call("GET", path, "displayname")
        name = answer.get("displayname")
        return name if isinstance(name, str) else ""

    async def upload(self, filename: str, media_type: str, data: bytes) -> str:
        """Put one file in the media repository and return the `mxc://` URI a message carries it
        by. The media repository is its own API rather than a client-server endpoint, so this is
        the one call that does not go through the session's base URL."""
        response = await self._http.post(
            f"{self._root}{MEDIA_PATH}/upload",
            content=data,
            params={"filename": filename},
            headers={"Content-Type": media_type or "application/octet-stream"},
        )
        answer = self._answer(response, "upload")
        content_uri = answer.get("content_uri")
        if not isinstance(content_uri, str):
            raise MatrixError("upload", 200, "M_BAD_JSON", None)
        return content_uri

    async def download(self, uri: str) -> bytes:
        """The bytes an `mxc://` names. The counterpart to `upload`, and like it the one call that
        does not go through the session's base URL, since the media repository is its own API.

        The download is the authenticated endpoint rather than the media repository's own: a
        homeserver holding `enable_authenticated_media`, which is Synapse's default, refuses
        authenticated media on the unauthenticated one. `upload` stays where it is, which the same
        default does not move.

        The uri is an event's claim about where a file lives, so its two parts are taken and quoted
        into this homeserver's path: one that names a path of its own is refused rather than
        followed."""
        parts = media_parts(uri)
        if parts is None:
            raise MatrixError("download", 400, "M_INVALID_PARAM", None)
        server, media_id = parts
        path = (
            f"{self._root}{AUTHENTICATED_MEDIA_PATH}/download"
            f"/{quote(server, safe='')}/{quote(media_id, safe='')}"
        )
        response = await self._http.get(path)
        if not response.is_success:
            self._answer(response, "download")
        return response.content

    async def encryption(self, room_id: str) -> Mapping[str, Any] | None:
        """The room's `m.room.encryption` state, or None for a room that is not encrypted."""
        path = f"/rooms/{quote(room_id, safe='')}/state/m.room.encryption/"
        try:
            answer = await self._call("GET", path, "encryption")
        except MatrixError as error:
            if error.status == 404:
                return None
            raise
        return answer if answer.get("algorithm") else None

    async def upload_keys(self, keys: Mapping[str, Any]) -> Mapping[str, int]:
        """Publish device keys, one-time keys, or a fallback key, and return how many one-time
        keys the homeserver holds for this device, by algorithm."""
        answer = await self._call("POST", "/keys/upload", "keys_upload", json=keys)
        counts = answer.get("one_time_key_counts")
        return counts if isinstance(counts, Mapping) else {}

    async def query_keys(self, users: Sequence[str]) -> Mapping[str, Any]:
        """Every device each user has published keys for, as the homeserver lists them."""
        body = {"device_keys": {user: [] for user in users}}
        answer = await self._call("POST", "/keys/query", "keys_query", json=body)
        devices = answer.get("device_keys")
        return devices if isinstance(devices, Mapping) else {}

    async def claim_keys(self, devices: Mapping[str, Sequence[str]]) -> Mapping[str, Any]:
        """One signed one-time key (or fallback key) per device, to open an Olm session with."""
        wanted = {user: dict.fromkeys(ids, "signed_curve25519") for user, ids in devices.items()}
        answer = await self._call(
            "POST", "/keys/claim", "keys_claim", json={"one_time_keys": wanted}
        )
        claimed = answer.get("one_time_keys")
        return claimed if isinstance(claimed, Mapping) else {}

    async def send_to_device(
        self, event_type: str, txn_id: str, messages: Mapping[str, Mapping[str, Any]]
    ) -> None:
        path = f"/sendToDevice/{quote(event_type, safe='')}/{quote(txn_id, safe='')}"
        await self._call("PUT", path, "send_to_device", json={"messages": messages})

    async def join(self, room_id: str) -> None:
        await self._call("POST", f"/join/{quote(room_id, safe='')}", "join", json={})

    async def joined_members(self, room_id: str) -> frozenset[str]:
        answer = await self._call(
            "GET", f"/rooms/{quote(room_id, safe='')}/joined_members", "joined_members"
        )
        joined = answer.get("joined")
        return frozenset(joined) if isinstance(joined, Mapping) else frozenset()

    async def messages_before(
        self, room_id: str, start: str, stop: str
    ) -> tuple[list[Mapping[str, Any]], str | None]:
        """One page of a room's messages walking back from `start` toward `stop`, newest first,
        and the token the next page starts from — None once the walk reached `stop`."""
        params: dict[str, str | int] = {
            "dir": "b",
            "from": start,
            "to": stop,
            "limit": BACKFILL_PAGE,
            "filter": BACKFILL_FILTER,
        }
        path = f"/rooms/{quote(room_id, safe='')}/messages"
        answer = await self._call("GET", path, "messages", params=params)
        chunk = answer.get("chunk")
        events = [e for e in chunk if isinstance(e, Mapping)] if isinstance(chunk, list) else []
        end = answer.get("end")
        return events, end if events and isinstance(end, str) and end != start else None

    async def _call(
        self,
        method: str,
        path: str,
        endpoint: str,
        *,
        params: Mapping[str, str | int] | None = None,
        json: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        response = await self._http.request(method, path, params=params, json=json)
        return self._answer(response, endpoint)

    def _answer(self, response: httpx.Response, endpoint: str) -> Mapping[str, Any]:
        try:
            answer = response.json()
        except ValueError:
            answer = {}
        if not isinstance(answer, Mapping):
            answer = {}
        if response.is_success:
            return answer
        errcode = answer.get("errcode")
        retry_after = answer.get("retry_after_ms")
        raise MatrixError(
            endpoint,
            response.status_code,
            errcode if isinstance(errcode, str) else None,
            retry_after if isinstance(retry_after, int) else _retry_after_header(response),
        )


def _retry_after_header(response: httpx.Response) -> int | None:
    value = response.headers.get("retry-after", "")
    return int(value) * 1000 if value.isdigit() else None
