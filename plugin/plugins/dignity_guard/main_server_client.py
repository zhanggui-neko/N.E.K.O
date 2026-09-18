"""Read-only client for the local N.E.K.O main server, plus the poll loop.

Two things live here:

* :class:`MainServerClient` — the only place in this plugin that touches the
  network. It talks to ``http://127.0.0.1:<port>`` and never writes.
* :class:`SettingsWatcher` — decides *when* to read what, using the
  conversation-settings revision as a cheap first judge, and folds the result
  into a :class:`~.settings_guard.GuardState`.

Why the client is configured the way it is
------------------------------------------
``trust_env=False`` and ``proxy=None`` are deliberate. On Windows machines
with a system-wide or 360 proxy, ``httpx`` honours ``HTTP_PROXY`` /
``NO_PROXY`` from the environment and can route a request to ``127.0.0.1``
through that proxy, where it fails. The same workaround is already used by
``plugin/plugins/lifekit/_api.py``.

Nothing here writes to the main server. The plugin cannot intercept config
writes at all (see DESIGN.md §2); it only observes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Mapping

import httpx

from .settings_guard import (
    GuardState,
    Evaluation,
    Snapshot,
    build_snapshot,
)

__all__ = [
    "CONVERSATION_PATH",
    "DEFAULT_BASE_URL",
    "DEFAULT_FULL_RESCAN_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "MainServerClient",
    "MainServerUnreachable",
    "RevisionProbe",
    "SNAPSHOT_SOURCES",
    "SettingsWatcher",
    "USER_LANGUAGE_PATH",
]

DEFAULT_BASE_URL = "http://127.0.0.1:48911"
DEFAULT_TIMEOUT_SECONDS = 3.0

CONVERSATION_PATH = "/api/config/conversation-settings"
USER_LANGUAGE_PATH = "/api/config/user_language"

#: Endpoints that together describe "her settings", and the prefix each one is
#: flattened under. ``conversation-settings`` is handled separately because it
#: is also the cheap change judge.
SNAPSHOT_SOURCES: tuple[tuple[str, str], ...] = (
    ("characters", "/api/characters"),
    ("page_config", "/api/config/page_config"),
    ("core_api", "/api/config/core_api"),
    ("preferences", "/api/config/preferences"),
)

#: The revision only covers conversation settings, so it cannot see an avatar
#: or a nickname change. Force a full compare at least this often.
DEFAULT_FULL_RESCAN_SECONDS = 60.0


class MainServerUnreachable(RuntimeError):
    """A read failed; the caller should keep the previous snapshot."""

    def __init__(self, endpoint: str, cause: object) -> None:
        super().__init__(f"main server read failed: {endpoint}: {cause}")
        self.endpoint = endpoint
        self.cause = cause


@dataclass(frozen=True, slots=True)
class RevisionProbe:
    """The cheap "did anything change?" answer."""

    revision: int | None = None
    etag: str | None = None

    def same_as(self, other: "RevisionProbe | None") -> bool:
        if other is None:
            return False
        if self.etag and other.etag:
            return self.etag == other.etag
        if self.revision is not None and other.revision is not None:
            return self.revision == other.revision
        return False

    def to_payload(self) -> dict[str, Any]:
        return {"revision": self.revision, "etag": self.etag}


class MainServerClient:
    """Minimal read-only HTTP client for the local main server."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = str(base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = float(timeout)
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                trust_env=False,
                proxy=None,
                transport=self._transport,
                headers={"Accept": "application/json"},
            )
        return self._client

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    # -- low level ---------------------------------------------------------

    async def _get_json(self, endpoint: str) -> Any:
        try:
            response = await self._ensure_client().get(endpoint)
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # noqa: BLE001 - re-raised as our own type
            raise MainServerUnreachable(endpoint, exc) from exc

    async def _get_with_headers(self, endpoint: str) -> tuple[Any, Mapping[str, str]]:
        try:
            response = await self._ensure_client().get(endpoint)
            response.raise_for_status()
            return response.json(), dict(response.headers)
        except Exception as exc:  # noqa: BLE001 - re-raised as our own type
            raise MainServerUnreachable(endpoint, exc) from exc

    # -- reads -------------------------------------------------------------

    async def fetch_conversation(self) -> tuple[RevisionProbe, Any]:
        """Read conversation settings plus the revision/ETag cheap judge."""
        payload, headers = await self._get_with_headers(CONVERSATION_PATH)
        if isinstance(payload, Mapping) and payload.get("success") is False:
            raise MainServerUnreachable(CONVERSATION_PATH, payload.get("error"))
        probe = RevisionProbe(
            revision=_as_int(payload.get("revision")) if isinstance(payload, Mapping) else None,
            etag=headers.get("etag") or headers.get("ETag"),
        )
        return probe, payload

    async def fetch_others(self) -> dict[str, Any]:
        """Read the remaining snapshot sources."""
        payloads: dict[str, Any] = {}
        for prefix, endpoint in SNAPSHOT_SOURCES:
            payloads[prefix] = await self._get_json(endpoint)
        return payloads

    async def fetch_snapshot(self) -> Snapshot:
        """Read everything and build a snapshot (used for forced refreshes)."""
        _probe, conversation = await self.fetch_conversation()
        payloads = await self.fetch_others()
        payloads["conversation"] = conversation_slice(conversation)
        return build_snapshot(payloads)

    async def fetch_user_language(self) -> str | None:
        """Best-effort read of the user's UI language (short code, e.g. ``zh``).

        Used to pick the locale of the text we hand to the model. A failure
        here must never break polling, so it returns ``None``.
        """
        try:
            payload = await self._get_json(USER_LANGUAGE_PATH)
        except MainServerUnreachable:
            return None
        if not isinstance(payload, Mapping):
            return None
        language = payload.get("language")
        return language if isinstance(language, str) and language.strip() else None


def conversation_slice(payload: Any) -> dict[str, Any] | None:
    """Keep only the parts of conversation-settings that are settings.

    ``telemetryBranch`` and ``reset`` are transport/telemetry details that
    change on their own; diffing them would produce noise she never asked for.
    """
    if not isinstance(payload, Mapping):
        return None
    sliced: dict[str, Any] = {}
    for key in ("settings", "decisions"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            sliced[key] = dict(value)
    return sliced or None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class SettingsWatcher:
    """Budget-aware poll loop: cheap revision check first, full diff on demand."""

    def __init__(
        self,
        client: MainServerClient,
        state: GuardState,
        *,
        full_rescan_seconds: float = DEFAULT_FULL_RESCAN_SECONDS,
    ) -> None:
        self.client = client
        self.state = state
        self.full_rescan_seconds = float(full_rescan_seconds)
        self.last_probe: RevisionProbe | None = None
        self.last_full_rescan: float | None = None
        self.last_error: str = ""

    def mark_full_rescan(self, *, now: float | None = None) -> None:
        self.last_full_rescan = now if now is not None else time.time()

    def _full_rescan_due(self, probe: RevisionProbe, *, now: float) -> bool:
        if self.last_full_rescan is None:
            return True
        if not probe.same_as(self.last_probe):
            return True
        return (now - self.last_full_rescan) >= self.full_rescan_seconds

    async def poll(self, *, force: bool = False, now: float | None = None) -> Evaluation:
        """Read the main server once and fold the result into the state.

        Raises :class:`MainServerUnreachable` when a read fails; the caller is
        expected to log and retry on the next tick, leaving the previous
        snapshot untouched so no phantom changes are reported.
        """
        moment = now if now is not None else time.time()
        probe, conversation = await self.client.fetch_conversation()

        if not force and not self._full_rescan_due(probe, now=moment):
            self.last_probe = probe
            self.last_error = ""
            return Evaluation()

        payloads = await self.client.fetch_others()
        payloads["conversation"] = conversation_slice(conversation)
        snapshot = build_snapshot(payloads)

        evaluation = self.state.evaluate(snapshot, now=moment)
        self.last_probe = probe
        self.last_full_rescan = moment
        self.last_error = ""
        return evaluation
