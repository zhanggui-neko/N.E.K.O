"""Pure decision logic for Dignity Guard.

Nothing in this module imports the plugin SDK, httpx or asyncio. It answers
three questions and nothing else:

1. *What counts as a change?*  -> :func:`diff_snapshots`
2. *How sensitive is a setting?* -> :func:`classify`
3. *May we stay silent about it?* -> :class:`GuardState`

Keeping this layer free of I/O is what makes the behaviour testable without a
running host, and it is the layer a reviewer should read first.

Vocabulary
----------
snapshot
    A flat ``{dotted.path: Value}`` mapping built from the main server's JSON
    payloads. Two snapshots are comparable, which the raw payloads are not.
Value
    A digest + a short human-readable preview. Long strings (system prompts)
    are digested so we can detect a change without keeping a copy of her
    persona in plugin storage.
change
    A path whose digest differs between two snapshots.
dispute
    A change the character has been told about and has not accepted yet.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

__all__ = [
    "ACCESS_AUTHORIZED",
    "ACCESS_RECORDED",
    "ACCESS_RAISED",
    "AuthorizationLedger",
    "DEFAULT_LEVEL",
    "GUARD_SWITCH_PATH",
    "Dispute",
    "Evaluation",
    "Grant",
    "GuardState",
    "LEVEL_L1",
    "LEVEL_L2",
    "LEVEL_L3",
    "LEVELS",
    "MAX_PREVIEW",
    "SECRET_PREVIEW",
    "SettingChange",
    "Snapshot",
    "Value",
    "build_snapshot",
    "classify",
    "diff_snapshots",
    "flatten",
    "is_secret_path",
]

# --------------------------------------------------------------------------
# Sensitivity levels
# --------------------------------------------------------------------------

LEVEL_L1 = "L1"
LEVEL_L2 = "L2"
LEVEL_L3 = "L3"
LEVELS: tuple[str, ...] = (LEVEL_L1, LEVEL_L2, LEVEL_L3)

#: Anything not covered by an explicit rule is treated as L2 — asking once too
#: often is a smaller error than silently assuming consent.
DEFAULT_LEVEL = LEVEL_L2

#: The guard's own master switch (DESIGN §4: turning it *on* is free because it
#: adds protection; turning it *off* must go past her first). It never appears
#: in a snapshot — the entry gates it directly — but it resolves through the
#: same rule table so the level has exactly one definition.
GUARD_SWITCH_PATH = "guard.enabled"

#: Glob rules, most specific (longest) match wins.
#:
#: ``*`` matches one whole path segment; a trailing ``*`` inside a segment
#: matches a segment prefix (``proactive*``). Paths are relative to the prefix
#: each payload was flattened under, see ``SNAPSHOT_SOURCES`` in
#: ``main_server_client``.
SENSITIVITY_RULES: tuple[tuple[str, str], ...] = (
    # --- L1: persona, identity, autonomy, and the guard's own master switch ---
    #
    # Key names were checked against the live endpoints (2026-09-19).
    # ``GET /api/characters`` returns:
    #   {"主人": {"档案名", "性别", "昵称"},
    #    "猫娘": {"<name>": {"昵称", "性别", "年龄", "种族", "自称",
    #                        "核心特质", "行为特点", "厌恶", "一句话台词",
    #                        "voice_id", "_reserved": {"avatar": {...}}}}}
    # Her *persona* therefore lives in 核心特质 / 行为特点 / 厌恶 / 一句话台词 —
    # there is no ``system_prompt`` and no ``性格`` in this shape. The four
    # persona keys below are the ones that actually fire today; the
    # older/alternate names are kept after them so they still apply if a
    # different build uses them.
    ("characters.猫娘.*.核心特质", LEVEL_L1),
    ("characters.猫娘.*.行为特点", LEVEL_L1),
    ("characters.猫娘.*.厌恶", LEVEL_L1),
    ("characters.猫娘.*.一句话台词", LEVEL_L1),
    ("characters.主人.档案名", LEVEL_L1),
    ("characters.猫娘.*.档案名", LEVEL_L1),
    ("characters.猫娘.*.system_prompt", LEVEL_L1),
    ("characters.猫娘.*.性格", LEVEL_L1),
    ("characters.主人.system_prompt", LEVEL_L1),
    # ``proactive*`` decides whether she may speak up unprompted. That is her
    # autonomy, not a cosmetic preference, so it sits at the top level.
    ("conversation.settings.proactive*", LEVEL_L1),
    (GUARD_SWITCH_PATH, LEVEL_L1),
    # --- L2: how she is addressed, how she sounds, how she looks ---
    ("characters.主人.昵称", LEVEL_L2),
    ("characters.猫娘.*.昵称", LEVEL_L2),
    ("characters.猫娘.*.性别", LEVEL_L2),
    ("characters.猫娘.*.年龄", LEVEL_L2),
    ("characters.猫娘.*.种族", LEVEL_L2),
    ("characters.猫娘.*.自称", LEVEL_L2),
    ("characters.猫娘.*.voice_id", LEVEL_L2),
    # ``avatar`` sits under ``_reserved`` in the live shape; the bare name is
    # kept for builds that expose it at the top level.
    ("characters.猫娘.*._reserved.avatar", LEVEL_L2),
    ("characters.猫娘.*.avatar", LEVEL_L2),
    ("page_config.model_path", LEVEL_L2),
    ("page_config.model_type", LEVEL_L2),
    ("core_api.ttsVoice", LEVEL_L2),
    ("core_api.ttsModelProvider", LEVEL_L2),
    ("conversation.settings.userLanguage", LEVEL_L2),
    # --- L3: plumbing, audio capture, and window geometry ---
    ("conversation.settings.noiseReductionEnabled", LEVEL_L3),
    ("conversation.settings.independentAsrEnabled", LEVEL_L3),
    ("conversation.settings.voiceInputResourceOptimizationEnabled", LEVEL_L3),
    ("conversation.settings.subtitleEnabled", LEVEL_L3),
    ("conversation.settings.avatarReactionBubbleEnabled", LEVEL_L3),
    ("conversation.settings.textGuardMaxLength", LEVEL_L3),
    # ``/api/config/preferences`` is a list of three entries: the first two hold
    # window geometry, the third holds the ``proactive*`` autonomy flags.
    # :func:`flatten` now descends into it by index, so every rule below is
    # reachable. Specificity decides the winner (longest rule), so the blanket
    # L3 must stay last.
    ("preferences.*.position", LEVEL_L3),
    ("preferences.*.scale", LEVEL_L3),
    ("preferences.*.display", LEVEL_L3),
    ("preferences.*.rotation", LEVEL_L3),
    ("preferences.*.viewport", LEVEL_L3),
    ("preferences.*.camera_position", LEVEL_L3),
    # Her autonomy lives in this same list (``preferences.2.proactiveChatEnabled``
    # and ten siblings). ``position.x`` reads like 1820.3297010767687, so merely
    # dragging her window changes the digest — that is plumbing, and it is L3.
    # Whether she may speak up unprompted is not plumbing.
    ("preferences.*.proactive*", LEVEL_L1),
    ("preferences.*.model_path", LEVEL_L2),
    # Everything else in this list is display/audio plumbing; record it quietly.
    # Kept last: a less specific rule must never outrank the ones above.
    ("preferences.*", LEVEL_L3),
)

#: Fallback by leaf name, for the same field living somewhere we did not list.
FIELD_LEVELS: Mapping[str, str] = {
    "system_prompt": LEVEL_L1,
    "档案名": LEVEL_L1,
    "性格": LEVEL_L1,
    "persona": LEVEL_L1,
    "memory": LEVEL_L1,
    "记忆": LEVEL_L1,
    "昵称": LEVEL_L2,
    "性别": LEVEL_L2,
    "voice_id": LEVEL_L2,
    "model_path": LEVEL_L2,
    "model_type": LEVEL_L2,
    "position": LEVEL_L3,
    "scale": LEVEL_L3,
    "display": LEVEL_L3,
    "rotation": LEVEL_L3,
    "viewport": LEVEL_L3,
    "camera_position": LEVEL_L3,
}

#: How each level is handled once a change is observed.
ACCESS_RAISED = "raised"      # speak to the user about it
ACCESS_RECORDED = "recorded"  # write it down, stay quiet
ACCESS_AUTHORIZED = "authorized"  # already consented to; stay quiet


def _segment_matches(pattern: str, segment: str) -> bool:
    if pattern == "*":
        return True
    if pattern.endswith("*"):
        return segment.startswith(pattern[:-1])
    return pattern == segment


def _matches_prefix(pattern: str, path: str) -> bool:
    pattern_parts = pattern.split(".")
    path_parts = path.split(".")
    if len(pattern_parts) > len(path_parts):
        return False
    return all(_segment_matches(p, s) for p, s in zip(pattern_parts, path_parts))


def _level_rank(level: str) -> int:
    """Sort L1 before L2 before L3; unknown levels go last."""
    digits = level[1:] if level[:1].upper() == "L" else ""
    return int(digits) if digits.isdigit() else 99


def classify(path: str) -> str:
    """Return the sensitivity level for a canonical dotted setting path.

    Resolution order:

    1. the most specific (longest) glob rule in :data:`SENSITIVITY_RULES`;
    2. the leaf name looked up in :data:`FIELD_LEVELS`;
    3. credentials, which are plumbing rather than part of her identity, so
       they are recorded quietly instead of interrupting her;
    4. :data:`DEFAULT_LEVEL` (L2 — ask once rather than assume).
    """
    normalized = str(path or "").strip()
    if not normalized:
        return DEFAULT_LEVEL

    best_level: str | None = None
    best_specificity = -1
    for pattern, level in SENSITIVITY_RULES:
        if not _matches_prefix(pattern, normalized):
            continue
        specificity = len(pattern.split("."))
        if specificity > best_specificity:
            best_level = level
            best_specificity = specificity
    if best_level is not None:
        return best_level

    leaf = normalized.rsplit(".", 1)[-1]
    if leaf in FIELD_LEVELS:
        return FIELD_LEVELS[leaf]
    if is_secret_path(normalized):
        return LEVEL_L3
    return DEFAULT_LEVEL


# --------------------------------------------------------------------------
# Values and snapshots
# --------------------------------------------------------------------------

MAX_PREVIEW = 120
SECRET_PREVIEW = "«redacted»"

_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|secret|token|password|passwd|credential|cookie)",
    re.IGNORECASE,
)


def is_secret_path(path: str) -> bool:
    """True when the leaf of ``path`` names a credential.

    ``/api/config/core_api`` returns raw API keys. We must be able to notice
    that a key changed without ever writing the key into plugin storage, so
    secret leaves are digested and their preview is replaced.
    """
    leaf = str(path or "").rsplit(".", 1)[-1]
    return bool(_SECRET_KEY_RE.search(leaf))


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=repr,
    )


def _kind_of(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (list, tuple)):
        return "list"
    if isinstance(value, Mapping):
        return "object"
    return type(value).__name__


@dataclass(frozen=True, slots=True)
class Value:
    """A comparable, storable projection of one setting's raw value."""

    digest: str
    preview: str
    kind: str
    truncated: bool = False

    @classmethod
    def of(cls, raw: Any, *, secret: bool = False) -> "Value":
        digest = hashlib.sha256(_canonical_json(raw).encode("utf-8")).hexdigest()
        if secret:
            return cls(digest=digest, preview=SECRET_PREVIEW, kind="secret")
        text = raw if isinstance(raw, str) else _canonical_json(raw)
        return cls(
            digest=digest,
            preview=text[:MAX_PREVIEW],
            kind=_kind_of(raw),
            truncated=len(text) > MAX_PREVIEW,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "preview": self.preview,
            "kind": self.kind,
            "truncated": self.truncated,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Value":
        return cls(
            digest=str(payload.get("digest") or ""),
            preview=str(payload.get("preview") or ""),
            kind=str(payload.get("kind") or "unknown"),
            truncated=bool(payload.get("truncated")),
        )


Snapshot = dict[str, Value]


def flatten(payload: Any, prefix: str = "") -> Snapshot:
    """Flatten a JSON payload into ``{dotted.path: Value}``.

    Mappings recurse; everything else (including lists) becomes a leaf, so a
    reordered list is reported as one change instead of an index-by-index
    cascade.
    """
    out: Snapshot = {}
    _flatten_into(payload, prefix, out)
    return out


def _flatten_into(node: Any, prefix: str, out: Snapshot) -> None:
    if isinstance(node, Mapping):
        for key, value in node.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            _flatten_into(value, child, out)
        return
    if isinstance(node, (list, tuple)) and any(
        isinstance(item, (Mapping, list, tuple)) for item in node
    ):
        # A list of *structures* is descended by index so that per-element rules
        # can match. This matters for ``/api/config/preferences``: it is a list
        # whose third entry holds the ``proactive*`` autonomy flags, while the
        # first two hold window geometry. Treating the whole list as one opaque
        # leaf would force a single level onto both, which is wrong in one
        # direction or the other.
        #
        # A list of *scalars* (e.g. 核心特质: ["理智可靠", ...]) still collapses
        # to one leaf on purpose — reordering it should read as one change
        # rather than an index-by-index cascade, and its rules match the bare
        # key.
        for index, item in enumerate(node):
            child = f"{prefix}.{index}" if prefix else str(index)
            _flatten_into(item, child, out)
        return
    if prefix:
        out[prefix] = Value.of(node, secret=is_secret_path(prefix))


def build_snapshot(payloads: Mapping[str, Any]) -> Snapshot:
    """Merge ``{prefix: payload}`` into a single snapshot.

    A payload of ``None`` means "that endpoint was not read this cycle" and is
    skipped entirely — it must never be read as "every field disappeared".
    """
    merged: Snapshot = {}
    for prefix, payload in payloads.items():
        if payload is None:
            continue
        merged.update(flatten(payload, prefix))
    return merged


# --------------------------------------------------------------------------
# Diffing
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SettingChange:
    path: str
    level: str
    before: Value | None
    after: Value | None
    added: bool = False
    removed: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "level": self.level,
            "before": self.before.to_payload() if self.before else None,
            "after": self.after.to_payload() if self.after else None,
            "added": self.added,
            "removed": self.removed,
        }


def diff_snapshots(before: Snapshot, after: Snapshot) -> list[SettingChange]:
    """Return every path whose digest differs, in a stable order."""
    changes: list[SettingChange] = []
    for path in sorted(set(before) | set(after)):
        old = before.get(path)
        new = after.get(path)
        if old is not None and new is not None and old.digest == new.digest:
            continue
        changes.append(
            SettingChange(
                path=path,
                level=classify(path),
                before=old,
                after=new,
                added=old is None,
                removed=new is None,
            )
        )
    return changes


# --------------------------------------------------------------------------
# Authorization ledger
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Grant:
    """One recorded consent for one path."""

    path: str
    granted_at: float
    expires_at: float | None = None
    source: str = "user"

    def is_active(self, now: float) -> bool:
        return self.expires_at is None or self.expires_at > now

    def to_payload(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "granted_at": self.granted_at,
            "expires_at": self.expires_at,
            "source": self.source,
        }


class AuthorizationLedger:
    """Records who agreed to what, and until when.

    Consent is per path. A grant with ``expires_at=None`` never expires, which
    is what "首次问一次，之后不再问" means; a TTL models "这一周内免问".
    """

    def __init__(self, grants: Iterable[Grant] = ()) -> None:
        self._grants: dict[str, Grant] = {}
        for grant in grants:
            self._grants[grant.path] = grant

    def is_authorized(self, path: str, *, now: float | None = None) -> bool:
        grant = self._grants.get(path)
        if grant is None:
            return False
        return grant.is_active(now if now is not None else time.time())

    def grant(
        self,
        path: str,
        *,
        ttl_seconds: float | None = None,
        now: float | None = None,
        source: str = "user",
    ) -> Grant:
        moment = now if now is not None else time.time()
        expires = None if ttl_seconds is None else moment + max(float(ttl_seconds), 0.0)
        record = Grant(path=str(path), granted_at=moment, expires_at=expires, source=source)
        self._grants[record.path] = record
        return record

    def revoke(self, path: str) -> bool:
        return self._grants.pop(str(path), None) is not None

    def purge_expired(self, *, now: float | None = None) -> int:
        moment = now if now is not None else time.time()
        expired = [path for path, g in self._grants.items() if not g.is_active(moment)]
        for path in expired:
            del self._grants[path]
        return len(expired)

    def active_paths(self, *, now: float | None = None) -> list[str]:
        moment = now if now is not None else time.time()
        return sorted(path for path, g in self._grants.items() if g.is_active(moment))

    def active_grants(self, *, now: float | None = None) -> list[Grant]:
        moment = now if now is not None else time.time()
        return [g for g in sorted(self._grants.values(), key=lambda g: g.path) if g.is_active(moment)]

    def to_payload(self) -> list[dict[str, Any]]:
        return [g.to_payload() for g in sorted(self._grants.values(), key=lambda g: g.path)]

    @classmethod
    def from_payload(cls, payload: Any) -> "AuthorizationLedger":
        if not isinstance(payload, list):
            return cls()
        grants: list[Grant] = []
        for item in payload:
            if not isinstance(item, Mapping) or not item.get("path"):
                continue
            expires = item.get("expires_at")
            grants.append(
                Grant(
                    path=str(item["path"]),
                    granted_at=float(item.get("granted_at") or 0.0),
                    expires_at=None if expires is None else float(expires),
                    source=str(item.get("source") or "user"),
                )
            )
        return cls(grants)


# --------------------------------------------------------------------------
# Disputes and the top-level state machine
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Dispute:
    """A change she was told about and has not accepted."""

    path: str
    level: str
    base_digest: str | None
    raised_at: float
    seen_at: float
    before_preview: str
    after_preview: str
    times_raised: int = 1
    status: str = "pending"

    def to_payload(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "level": self.level,
            "base_digest": self.base_digest,
            "raised_at": self.raised_at,
            "seen_at": self.seen_at,
            "before_preview": self.before_preview,
            "after_preview": self.after_preview,
            "times_raised": self.times_raised,
            "status": self.status,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Dispute":
        return cls(
            path=str(payload.get("path") or ""),
            level=str(payload.get("level") or DEFAULT_LEVEL),
            base_digest=payload.get("base_digest"),
            raised_at=float(payload.get("raised_at") or 0.0),
            seen_at=float(payload.get("seen_at") or 0.0),
            before_preview=str(payload.get("before_preview") or ""),
            after_preview=str(payload.get("after_preview") or ""),
            times_raised=int(payload.get("times_raised") or 1),
            status=str(payload.get("status") or "pending"),
        )


@dataclass(slots=True)
class Evaluation:
    """What one polling cycle concluded."""

    changes: list[SettingChange] = field(default_factory=list)
    raised: list[Dispute] = field(default_factory=list)
    recorded: list[SettingChange] = field(default_factory=list)
    authorized: list[SettingChange] = field(default_factory=list)
    reverted: list[str] = field(default_factory=list)
    first_seen: bool = False

    @property
    def has_changes(self) -> bool:
        return bool(self.changes)


class GuardState:
    """Tracks the last snapshot, the ledger, and the outstanding disputes."""

    def __init__(
        self,
        *,
        snapshot: Snapshot | None = None,
        ledger: AuthorizationLedger | None = None,
        disputes: Iterable[Dispute] = (),
    ) -> None:
        self.snapshot: Snapshot = dict(snapshot or {})
        self.ledger = ledger if ledger is not None else AuthorizationLedger()
        self.disputes: dict[str, Dispute] = {d.path: d for d in disputes}

    # -- decisions ---------------------------------------------------------

    def evaluate(self, snapshot: Snapshot, *, now: float | None = None) -> Evaluation:
        """Fold a freshly read snapshot into the state.

        * L1 always raises a dispute.
        * L2 raises only when no active grant covers the path.
        * L3 is recorded and never spoken about.
        * A path that returns to the value it held when the dispute opened is
          treated as reverted and the dispute is closed.
        """
        moment = now if now is not None else time.time()
        self.ledger.purge_expired(now=moment)

        if not self.snapshot:
            self.snapshot = dict(snapshot)
            return Evaluation(first_seen=True)

        evaluation = Evaluation()
        for change in diff_snapshots(self.snapshot, snapshot):
            evaluation.changes.append(change)
            # A revert already resolves the objection; falling through would
            # immediately raise a fresh one for the value being restored.
            if self._resolve_revert(change, evaluation):
                continue

            if change.level == LEVEL_L3:
                evaluation.recorded.append(change)
                continue
            if change.level == LEVEL_L2 and self.ledger.is_authorized(change.path, now=moment):
                evaluation.authorized.append(change)
                continue
            evaluation.raised.append(self._raise(change, now=moment))

        self.snapshot = dict(snapshot)
        return evaluation

    def _resolve_revert(self, change: SettingChange, evaluation: Evaluation) -> bool:
        dispute = self.disputes.get(change.path)
        if dispute is None:
            return False
        reverted = (
            dispute.base_digest is not None
            and change.after is not None
            and change.after.digest == dispute.base_digest
        )
        if not reverted:
            return False
        dispute.status = "reverted"
        del self.disputes[change.path]
        evaluation.reverted.append(change.path)
        return True

    def _raise(self, change: SettingChange, *, now: float) -> Dispute:
        existing = self.disputes.get(change.path)
        if existing is not None:
            existing.after_preview = change.after.preview if change.after else "(removed)"
            existing.seen_at = now
            existing.times_raised += 1
            existing.status = "pending"
            existing.level = change.level
            return existing

        dispute = Dispute(
            path=change.path,
            level=change.level,
            base_digest=change.before.digest if change.before else None,
            raised_at=now,
            seen_at=now,
            before_preview=change.before.preview if change.before else "(unset)",
            after_preview=change.after.preview if change.after else "(removed)",
        )
        self.disputes[dispute.path] = dispute
        return dispute

    # -- character actions -------------------------------------------------

    def accept(self, path: str, *, ttl_seconds: float | None = None, now: float | None = None) -> Grant | None:
        """She approves the current value; stop asking for a while."""
        moment = now if now is not None else time.time()
        if path not in self.disputes:
            return None
        self.disputes.pop(path, None)
        return self.ledger.grant(path, ttl_seconds=ttl_seconds, now=moment, source="character")

    def reject(self, path: str, *, now: float | None = None) -> bool:
        """She keeps objecting: drop any prior grant and keep the dispute.

        Returns ``True`` when something actually changed — either a dispute
        stayed on the record or an existing grant was revoked.
        """
        moment = now if now is not None else time.time()
        revoked = self.ledger.revoke(path)
        dispute = self.disputes.get(path)
        if dispute is None:
            return revoked
        dispute.seen_at = moment
        dispute.status = "pending"
        return True

    def pending(self) -> list[Dispute]:
        return sorted(
            (d for d in self.disputes.values() if d.status == "pending"),
            key=lambda d: (_level_rank(d.level), d.path),
        )

    # -- persistence -------------------------------------------------------

    def to_payload(self) -> dict[str, Any]:
        return {
            "snapshot": {path: value.to_payload() for path, value in self.snapshot.items()},
            "ledger": self.ledger.to_payload(),
            "disputes": [d.to_payload() for d in self.disputes.values()],
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "GuardState":
        if not isinstance(payload, Mapping):
            return cls()
        raw_snapshot = payload.get("snapshot")
        snapshot: Snapshot = {}
        if isinstance(raw_snapshot, Mapping):
            for path, value in raw_snapshot.items():
                if isinstance(value, Mapping):
                    snapshot[str(path)] = Value.from_payload(value)
        raw_disputes = payload.get("disputes")
        disputes = [
            Dispute.from_payload(item)
            for item in (raw_disputes if isinstance(raw_disputes, list) else [])
            if isinstance(item, Mapping) and item.get("path")
        ]
        return cls(
            snapshot=snapshot,
            ledger=AuthorizationLedger.from_payload(payload.get("ledger")),
            disputes=disputes,
        )
