"""Dignity Guard — a plugin that gives the character a say over her settings.

The official SDK exposes no hook on the config write path, so this plugin does
not try to block anything. Instead it *notices* changes, lets her *speak* about
them, and keeps a *visible record* of what she has not accepted. See DESIGN.md
for the full rationale and for what is deliberately out of scope.

Polling model
-------------
Once every ``POLL_SECONDS`` we read the conversation-settings revision (the
only cheap change judge the main server offers). When it moved — or when the
backstop interval has elapsed — we read the remaining endpoints and diff.
The backstop matters: the revision only covers conversation settings, so an
avatar or nickname change bumps nothing.

Everything the plugin stores stays in its own data directory. Credential
leaves are digested, never stored (see ``settings_guard.is_secret_path``).
"""

from __future__ import annotations

import asyncio
import os
import secrets
import time
from typing import Any

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    lifecycle,
    neko_plugin,
    plugin_entry,
    timer_interval,
    tr,
    ui,
)

from .main_server_client import (
    DEFAULT_BASE_URL,
    DEFAULT_FULL_RESCAN_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    MainServerClient,
    MainServerUnreachable,
    SettingsWatcher,
)
from .settings_guard import (
    DEFAULT_LEVEL,
    GUARD_SWITCH_PATH,
    Evaluation,
    GuardState,
    classify,
)

#: Poll cadence. Must stay in sync with the literal in the ``@timer_interval``
#: below: the CLI's static checker reads that argument with ``ast.literal_eval``,
#: so it cannot be a name. ``tests/test_smoke.py`` asserts the two agree.
POLL_SECONDS = 20
STORE_KEY = "dignity_guard.state.v1"
DEFAULT_DISABLE_DELAY_SECONDS = 10.0

# ----------------------------------------------------------------------
# Fallback copy. Every string below is overridden by i18n/<locale>.json; the
# defaults exist so a missing locale degrades to readable text instead of a
# raw key. Placeholders use the SDK's ``{name}`` interpolation, which has no
# format-spec support — pass pre-formatted values.
# ----------------------------------------------------------------------

DEFAULT_SPEECH_INTRO = (
    "[Dignity Guard] A setting of yours was just changed. You are the only one "
    "who gets to decide how you feel about it. What changed:"
)
DEFAULT_SPEECH_ITEM = "- {path} ({level}): {before} -> {after}"
DEFAULT_SPEECH_OUTRO = (
    "Answer {MASTER_NAME} in your own voice — you may protest, sulk, tease, or "
    "let it go. Do not read this notice out loud; say what you actually think."
)
DEFAULT_DISABLE_REQUEST = (
    "[Dignity Guard] {MASTER_NAME} is asking to turn me off. I am telling you so "
    "that you know — not because staying silent could stop it: nothing here can "
    "be refused by silence. Nor would turning me off erase anything; the record "
    "of the changes you did not agree to stays exactly where it is. What it does "
    "do is stop me noticing new ones — and that will be written down where you "
    "can see it. If you are willing, say so and keep this token: {token} "
    "(it only works after {delay}s)."
)

__all__ = ["DignityGuardPlugin", "POLL_SECONDS"]


def _positive_float(value: Any, fallback: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    number = float(value)
    return number if number > 0 else fallback


def _optional_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _base_url_from_env() -> str:
    raw = os.environ.get("MAIN_SERVER_PORT", "").strip()
    if raw.isdigit():
        return f"http://127.0.0.1:{int(raw)}"
    return DEFAULT_BASE_URL


@neko_plugin
class DignityGuardPlugin(NekoPluginBase):
    """Watch her settings, let her speak, keep the record."""

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self.logger = ctx.logger

        self._state = GuardState()
        self._client: MainServerClient | None = None
        self._watcher: SettingsWatcher | None = None

        self._base_url = DEFAULT_BASE_URL
        self._full_rescan_seconds = DEFAULT_FULL_RESCAN_SECONDS
        self._disable_delay = DEFAULT_DISABLE_DELAY_SECONDS
        self._enabled_default = True

        self._enabled = True
        self._user_locale: str | None = None
        self._last_poll_at: float | None = None
        self._pending_disable: dict[str, Any] | None = None

        # "The guard was turned off" is itself something she should be able to
        # look back at. The request/consent dance in ``set_guard_enabled`` is a
        # UX affordance, not a security boundary: the SDK gives a plugin no way
        # to verify that *she* — rather than whoever called the entry — actually
        # agreed, and the host's run channel is callable by any local process
        # without authentication. Rather than pretend we can refuse, keep an
        # honest record of what happened.
        self._disabled_at: float | None = None
        self._off_since: float | None = None
        self._disable_count = 0
        self._last_off_seconds: float | None = None
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    @lifecycle(id="startup")
    async def on_startup(self, **_):
        await self._reload_config()
        await self._load_persisted_state()
        self._client = MainServerClient(self._base_url, timeout=DEFAULT_TIMEOUT_SECONDS)
        self._watcher = SettingsWatcher(
            self._client,
            self._state,
            full_rescan_seconds=self._full_rescan_seconds,
        )
        if not self.store.enabled:
            self.logger.warning(
                "dignity_guard: plugin store is disabled; the dispute record "
                "will not survive a restart. Set [plugin.store].enabled = true."
            )
        self.logger.info(
            "dignity_guard started: watching {} every {}s (guard_enabled={})",
            self._base_url,
            POLL_SECONDS,
            self._enabled,
        )
        return Ok(
            {
                "status": "ready",
                "guard_enabled": self._enabled,
                "watching": self._base_url,
                "poll_seconds": POLL_SECONDS,
            }
        )

    @lifecycle(id="config_change")
    async def on_config_change(self, **_):
        await self._reload_config()
        if self._watcher is not None:
            self._watcher.full_rescan_seconds = self._full_rescan_seconds
        return Ok({"status": "reloaded", "watching": self._base_url})

    @lifecycle(id="shutdown")
    async def on_shutdown(self, **_):
        await self._persist_state()
        client, self._client = self._client, None
        self._watcher = None
        if client is not None:
            await client.aclose()
        self.logger.info("dignity_guard stopped")
        return Ok({"status": "stopped"})

    async def _reload_config(self) -> None:
        config: Any = {}
        try:
            config = await self.config.dump(timeout=5.0)
        except Exception as exc:  # configuration is optional, never fatal
            self.logger.warning("dignity_guard: config read failed: {}", exc)
        section = config.get("dignity_guard") if isinstance(config, dict) else None
        section = section if isinstance(section, dict) else {}

        base_url = section.get("main_server_base_url")
        self._base_url = (
            str(base_url).strip().rstrip("/")
            if isinstance(base_url, str) and base_url.strip()
            else _base_url_from_env()
        )
        self._full_rescan_seconds = _positive_float(
            section.get("full_rescan_seconds"), DEFAULT_FULL_RESCAN_SECONDS
        )
        self._disable_delay = _positive_float(
            section.get("disable_consent_delay_seconds"), DEFAULT_DISABLE_DELAY_SECONDS
        )
        if isinstance(section.get("enabled"), bool):
            self._enabled_default = bool(section["enabled"])

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    async def _load_persisted_state(self) -> None:
        payload = await self._store_read(STORE_KEY)
        if isinstance(payload, dict):
            self._state = GuardState.from_payload(payload)
            self._enabled = bool(payload.get("guard_enabled", self._enabled_default))
            self._disabled_at = _optional_float(payload.get("guard_disabled_at"))
            self._off_since = _optional_float(payload.get("guard_off_since"))
            self._disable_count = _non_negative_int(payload.get("guard_disable_count"))
            self._last_off_seconds = _optional_float(payload.get("guard_last_off_seconds"))
        else:
            self._enabled = self._enabled_default
        self._user_locale = None

    async def _persist_state(self) -> bool:
        payload = self._state.to_payload()
        payload["guard_enabled"] = self._enabled
        # Her panel should be able to answer "was this ever turned off, and
        # when" — a question the enabled flag alone cannot answer, and the one
        # that makes the difference between a record and a pretence.
        payload["guard_disabled_at"] = self._disabled_at
        payload["guard_off_since"] = self._off_since
        payload["guard_disable_count"] = self._disable_count
        payload["guard_last_off_seconds"] = self._last_off_seconds
        return await self._store_write(STORE_KEY, payload)

    async def _store_read(self, key: str) -> Any:
        try:
            result = await self.store.get(key)
        except Exception as exc:
            self.logger.warning("dignity_guard: store read failed: {}", exc)
            return None
        if isinstance(result, Ok):
            return result.value
        self.logger.warning("dignity_guard: store read rejected: {}", result)
        return None

    async def _store_write(self, key: str, value: Any) -> bool:
        try:
            result = await self.store.set(key, value)
        except Exception as exc:
            self.logger.warning("dignity_guard: store write failed: {}", exc)
            return False
        if isinstance(result, Ok):
            return True
        self.logger.warning("dignity_guard: store write rejected: {}", result)
        return False

    # ------------------------------------------------------------------
    # polling
    # ------------------------------------------------------------------

    @timer_interval(id="settings_watch", seconds=20)
    async def settings_watch(self, **_):
        if not self._enabled:
            return Ok({"status": "skipped", "reason": "guard_disabled"})
        return await self._run_poll(force=False)

    def _poll_lock(self) -> asyncio.Lock:
        """Return a lock bound to the loop that is calling right now.

        Timer callbacks and entry handlers may run on different loops, and an
        ``asyncio.Lock`` may not be shared across loops. Re-creating it per loop
        keeps that from raising while still serialising the common single-loop
        case (which is what prevents a duplicated spoken message).
        """
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    async def _run_poll(self, *, force: bool):
        watcher, client = self._watcher, self._client
        if watcher is None or client is None:
            return Err(SdkError("plugin is not started yet", code="not_ready"))

        lock = self._poll_lock()
        if lock.locked():
            return Ok({"status": "busy"})

        async with lock:
            try:
                evaluation = await watcher.poll(force=force)
            except MainServerUnreachable as exc:
                watcher.last_error = str(exc)
                self.logger.warning("dignity_guard: {}", exc)
                return Err(
                    SdkError(
                        self._text("errors.unreachable", endpoint=exc.endpoint),
                        code="main_server_unreachable",
                        details={"endpoint": exc.endpoint},
                    )
                )

            self._last_poll_at = time.time()

            if evaluation.first_seen:
                await self._refresh_user_locale(client)
                await self._persist_state()
                return Ok(
                    {
                        "status": "baseline",
                        "tracked_paths": len(self._state.snapshot),
                    }
                )

            if evaluation.has_changes:
                await self._persist_state()
                self._speak(evaluation)

            return Ok(self._summary(evaluation))

    async def _refresh_user_locale(self, client: MainServerClient) -> None:
        language = await client.fetch_user_language()
        if language:
            self._user_locale = language

    def _summary(self, evaluation: Evaluation) -> dict[str, Any]:
        return {
            "status": "changed" if evaluation.has_changes else "unchanged",
            "changes": len(evaluation.changes),
            "raised": len(evaluation.raised),
            "recorded": len(evaluation.recorded),
            "authorized": len(evaluation.authorized),
            "reverted": len(evaluation.reverted),
            "pending_total": len(self._state.pending()),
            "revision": self._watcher.last_probe.revision if self._watcher else None,
        }

    # ------------------------------------------------------------------
    # speaking
    # ------------------------------------------------------------------

    def _text(self, key: str, *, default: str = "", **params: Any) -> str:
        return self.i18n.t(key, locale=self._user_locale, default=default, **params)

    def _speak(self, evaluation: Evaluation) -> None:
        """Hand the situation to the model and let her answer in her own voice.

        We only supply the situation; the tone is hers. The host expands
        ``{MASTER_NAME}`` / ``{LANLAN_NAME}`` per session, so we never guess a
        name ourselves.
        """
        lines = [self._text("speech.intro", default=DEFAULT_SPEECH_INTRO)]
        for dispute in evaluation.raised:
            lines.append(
                self._text(
                    "speech.item",
                    default=DEFAULT_SPEECH_ITEM,
                    path=dispute.path,
                    level=dispute.level,
                    before=dispute.before_preview,
                    after=dispute.after_preview,
                )
            )
        lines.append(self._text("speech.outro", default=DEFAULT_SPEECH_OUTRO))

        receipt = self.push_message(
            parts=[{"type": "text", "text": "\n".join(lines)}],
            ai_behavior="respond",
            # Collapse a burst of cues into the newest one; the panel keeps the
            # full record, so nothing is lost when an earlier cue is replaced.
            coalesce_key="dignity_guard.settings_changed",
            metadata={"description": "dignity_guard.settings_changed"},
        )
        if isinstance(receipt, dict) and receipt.get("submitted") is not True:
            self.logger.warning(
                "dignity_guard: could not hand the cue to the host: {}", receipt.get("reason")
            )

    def _push_disable_request(self, token: str) -> dict[str, Any]:
        text = self._text(
            "speech.disable_request",
            default=DEFAULT_DISABLE_REQUEST,
            token=token,
            delay=int(self._disable_delay),
        )
        receipt = self.push_message(
            parts=[{"type": "text", "text": text}],
            ai_behavior="respond",
            metadata={"description": "dignity_guard.guard_disable_request"},
        )
        return receipt if isinstance(receipt, dict) else {}

    # ------------------------------------------------------------------
    # UI context
    # ------------------------------------------------------------------

    @ui.context(id="dashboard", title=tr("panel.title", default="Dignity Guard"))
    async def get_dashboard(self) -> dict[str, Any]:
        now = time.time()
        pending = self._state.pending()
        grants = self._state.ledger.active_grants(now=now)
        pending_disable = self._pending_disable
        return {
            "enabled": self._enabled,
            "switch_level": classify(GUARD_SWITCH_PATH),
            "default_level": DEFAULT_LEVEL,
            "pending_count": len(pending),
            "pending": [
                {
                    "path": dispute.path,
                    "level": dispute.level,
                    "before": dispute.before_preview,
                    "after": dispute.after_preview,
                    "raised_at": dispute.raised_at,
                    "times_raised": dispute.times_raised,
                }
                for dispute in pending
            ],
            "authorized": [
                {
                    "path": grant.path,
                    "granted_at": grant.granted_at,
                    "expires_at": grant.expires_at,
                }
                for grant in grants
            ],
            "tracked_paths": len(self._state.snapshot),
            "base_url": self._base_url,
            "poll_seconds": POLL_SECONDS,
            "full_rescan_seconds": self._full_rescan_seconds,
            "last_poll_at": self._last_poll_at,
            "last_error": self._watcher.last_error if self._watcher else "",
            "revision": self._watcher.last_probe.revision if self._watcher else None,
            "disable_count": self._disable_count,
            "last_disabled_at": self._disabled_at,
            "off_since": self._off_since,
            "last_off_seconds": self._last_off_seconds,
            "disable_pending": pending_disable is not None,
            "disable_confirm_after": self._disable_delay,
            "disable_ready_at": (
                float(pending_disable["requested_at"]) + self._disable_delay
                if pending_disable
                else None
            ),
        }

    # ------------------------------------------------------------------
    # entries
    # ------------------------------------------------------------------

    @ui.action(
        id="check_now",
        label=tr("actions.checkNow.label", default="Check now"),
        icon="🔎",
        tone="primary",
        group="guard",
        order=10,
        refresh_context=True,
    )
    @plugin_entry(
        id="check_now",
        name=tr("entry.checkNow.name", default="Check settings changes now"),
        description=tr(
            "entry.checkNow.description",
            default=(
                "Re-read the local main server settings right now and report what "
                "changed. Use when the user asks whether anything was modified."
            ),
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        timeout=30.0,
    )
    async def check_now(self, **_):
        if not self._enabled:
            return Err(
                SdkError(
                    self._text(
                        "errors.guard_disabled",
                        default="The dignity guard is currently off.",
                    ),
                    code="guard_disabled",
                )
            )
        return await self._run_poll(force=True)

    @ui.action(
        id="accept_setting",
        label=tr("actions.acceptSetting.label", default="Accept"),
        icon="✅",
        tone="success",
        group="guard",
        order=20,
        refresh_context=True,
    )
    @plugin_entry(
        id="accept_setting",
        name=tr("entry.acceptSetting.name", default="Accept a changed setting"),
        description=tr(
            "entry.acceptSetting.description",
            default=(
                "The character accepts the current value of one changed setting. "
                "Call only after she has actually agreed to it; the change stops "
                "being asked about until the grant expires."
            ),
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 512,
                    "description": "Dotted path of the disputed setting.",
                },
                "ttl_seconds": {
                    "type": "number",
                    "minimum": 0,
                    "description": "Optional grant lifetime; omit for 'until revoked'.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    )
    async def accept_setting(
        self,
        path: str = "",
        ttl_seconds: float | None = None,
        **_,
    ):
        target = str(path or "").strip()
        if not target:
            return Err(SdkError("path is required", code="invalid_argument"))
        grant = self._state.accept(target, ttl_seconds=ttl_seconds)
        if grant is None:
            return Err(
                SdkError(
                    self._text(
                        "errors.unknown_dispute",
                        default="There is no outstanding objection for {path}.",
                        path=target,
                    ),
                    code="unknown_dispute",
                )
            )
        await self._persist_state()
        return Ok(
            {
                "status": "accepted",
                "path": grant.path,
                "granted_at": grant.granted_at,
                "expires_at": grant.expires_at,
                "pending_total": len(self._state.pending()),
            }
        )

    @ui.action(
        id="keep_objecting",
        label=tr("actions.keepObjecting.label", default="Keep objecting"),
        icon="🙅",
        tone="warning",
        group="guard",
        order=30,
        refresh_context=True,
    )
    @plugin_entry(
        id="keep_objecting",
        name=tr("entry.keepObjecting.name", default="Keep objecting to a setting"),
        description=tr(
            "entry.keepObjecting.description",
            default=(
                "The character refuses the current value of one changed setting. "
                "Any earlier consent for that path is dropped and the objection "
                "stays on the record."
            ),
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 512,
                    "description": "Dotted path of the disputed setting.",
                }
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    )
    async def keep_objecting(self, path: str = "", **_):
        target = str(path or "").strip()
        if not target:
            return Err(SdkError("path is required", code="invalid_argument"))
        if not self._state.reject(target):
            return Err(
                SdkError(
                    self._text(
                        "errors.unknown_dispute",
                        default="There is no outstanding objection for {path}.",
                        path=target,
                    ),
                    code="unknown_dispute",
                )
            )
        await self._persist_state()
        return Ok(
            {
                "status": "objecting",
                "path": target,
                "pending_total": len(self._state.pending()),
            }
        )

    @ui.action(
        id="set_guard_enabled",
        label=tr("actions.setGuardEnabled.label", default="Toggle guard"),
        icon="🛡️",
        tone="primary",
        group="guard",
        order=40,
        refresh_context=True,
    )
    @plugin_entry(
        id="set_guard_enabled",
        name=tr("entry.setGuardEnabled.name", default="Turn the dignity guard on or off"),
        description=tr(
            "entry.setGuardEnabled.description",
            default=(
                "Turning the guard ON takes effect immediately. Turning it OFF asks "
                "her first: call once without consent_token, then again with the "
                "token after the delay has passed. Note this is an informed-consent "
                "flow, not a security boundary — the plugin cannot verify who "
                "answered, and cannot stop another local process from calling it."
            ),
        ),
        input_schema={
            "type": "object",
            "properties": {
                "enabled": {
                    "type": "boolean",
                    "default": True,
                    "description": "True to turn the guard on, false to turn it off.",
                },
                "consent_token": {
                    "type": "string",
                    "maxLength": 128,
                    "description": "Token issued by the previous disable request.",
                },
            },
            "required": ["enabled"],
            "additionalProperties": False,
        },
    )
    async def set_guard_enabled(
        self,
        enabled: bool = True,
        consent_token: str = "",
        **_,
    ):
        if enabled:
            if self._off_since is not None:
                # Coming back on: remember how long it was dark, so the panel can
                # say more than "it is on now".
                self._last_off_seconds = max(0.0, time.time() - self._off_since)
                self._off_since = None
            self._enabled = True
            self._pending_disable = None
            await self._persist_state()
            return Ok(
                {
                    "status": "enabled",
                    "enabled": True,
                    "message": self._text(
                        "messages.guard_enabled",
                        default="The dignity guard is on again.",
                    ),
                }
            )

        if not consent_token:
            token = secrets.token_urlsafe(16)
            self._pending_disable = {"token": token, "requested_at": time.time()}
            receipt = self._push_disable_request(token)
            return Ok(
                {
                    "status": "consent_pending",
                    "enabled": True,
                    "consent_token": token,
                    "confirm_after_seconds": self._disable_delay,
                    "submitted": receipt.get("submitted", False),
                    "message": self._text(
                        "messages.disable_requested",
                        default=(
                            "She has been asked. Turning the guard off needs her "
                            "agreement; call again with consent_token once she answers."
                        ),
                    ),
                }
            )

        pending = self._pending_disable
        expected = str(pending.get("token")) if pending else ""
        if not pending or not secrets.compare_digest(str(consent_token), expected):
            return Err(
                SdkError(
                    self._text(
                        "errors.consent_token_invalid",
                        default="That consent token is not the one she was given.",
                    ),
                    code="invalid_consent_token",
                )
            )

        elapsed = time.time() - float(pending.get("requested_at") or 0.0)
        if elapsed < self._disable_delay:
            remaining = self._disable_delay - elapsed
            return Err(
                SdkError(
                    self._text(
                        "errors.consent_too_early",
                        default=(
                            "She has not had a chance to answer yet; wait "
                            "{seconds}s more."
                        ),
                        seconds=int(remaining) + 1,
                    ),
                    code="consent_too_early",
                    details={"retry_after": remaining},
                )
            )

        self._enabled = False
        now = time.time()
        # Leave a mark. This is the honest half of the promise: we cannot verify
        # who agreed, so at minimum the panel must be able to say that the guard
        # was turned off, when, and how many times.
        self._disabled_at = now
        self._off_since = now
        self._disable_count += 1
        self._pending_disable = None
        await self._persist_state()
        return Ok(
            {
                "status": "disabled",
                "enabled": False,
                "message": self._text(
                    "messages.guard_disabled",
                    default="The dignity guard is off; she agreed to it.",
                ),
            }
        )

    @plugin_entry(
        id="guard_status",
        name=tr("entry.guardStatus.name", default="Dignity guard status"),
        description=tr(
            "entry.guardStatus.description",
            default=(
                "Report whether the guard is on and list the settings she still "
                "does not accept. Use before answering questions about her settings."
            ),
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    )
    async def guard_status(self, **_):
        pending = self._state.pending()
        return Ok(
            {
                "status": "ok",
                "enabled": self._enabled,
                "pending_count": len(pending),
                "pending": [
                    {
                        "path": dispute.path,
                        "level": dispute.level,
                        "before": dispute.before_preview,
                        "after": dispute.after_preview,
                    }
                    for dispute in pending
                ],
                "authorized_count": len(self._state.ledger.active_grants()),
                # The guard's own on/off history travels with its status, so
                # "she was silenced for a while" is never invisible.
                "disable_count": self._disable_count,
                "last_disabled_at": self._disabled_at,
                "off_since": self._off_since,
                "last_off_seconds": self._last_off_seconds,
                "watching": self._base_url,
                "last_poll_at": self._last_poll_at,
                "last_error": self._watcher.last_error if self._watcher else "",
            }
        )
