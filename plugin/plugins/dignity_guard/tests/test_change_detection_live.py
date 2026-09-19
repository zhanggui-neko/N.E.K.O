"""End-to-end change detection against a real loopback HTTP server.

The plugin's whole job is to notice a settings change. A test that only calls
``diff_snapshots`` would not prove that, so these tests point the real
:class:`MainServerClient` at the stand-in server from ``conftest.py`` (real
httpx, real sockets — which is also what exercises ``trust_env=False``) and
assert that editing one value on the "server" side is picked up on the next
poll.

It never talks to the real N.E.K.O main server and never touches the user's
configuration.
"""

from __future__ import annotations

import asyncio
import json

from plugin.plugins.dignity_guard.main_server_client import (
    MainServerClient,
    SettingsWatcher,
)
from plugin.plugins.dignity_guard.settings_guard import (
    LEVEL_L1,
    LEVEL_L2,
    LEVEL_L3,
    SECRET_PREVIEW,
    GuardState,
)

#: Generous on purpose, and deliberately *not* the production default.
#:
#: The stand-in server in ``conftest.py`` is a ``ThreadingHTTPServer``, which
#: spawns a thread per connection, and this suite runs on whatever machine the
#: author or CI happens to provide. At the production default of 3s, a busy
#: 2-core box — or one running other work in parallel — fails these tests
#: spuriously: the reads are correct, they are simply slow to be scheduled.
#: Raising the client timeout here keeps the assertions about *behaviour* while
#: removing the machine's load from the test's critical path.
SERVER_TIMEOUT_SECONDS = 30.0


def test_editing_one_setting_is_detected_on_the_next_poll(main_server) -> None:
    """The core promise: change a value, and the plugin notices."""
    fake, base_url = main_server

    async def scenario():
        client = MainServerClient(base_url, timeout=SERVER_TIMEOUT_SECONDS)
        state = GuardState()
        watcher = SettingsWatcher(client, state, full_rescan_seconds=3600.0)
        try:
            baseline = await watcher.poll(now=1_000.0)
            unchanged = await watcher.poll(now=1_001.0)

            fake.settings["proactiveChatEnabled"] = False
            fake.bump_revision()
            changed = await watcher.poll(now=1_002.0)

            return baseline, unchanged, changed, dict(state.snapshot)
        finally:
            await client.aclose()

    baseline, unchanged, changed, snapshot = asyncio.run(scenario())

    # Baseline: the first read establishes the reference and stays silent.
    assert baseline.first_seen is True
    assert baseline.raised == []
    assert snapshot, "the baseline snapshot must actually contain settings"

    # Unchanged revision: the cheap judge short-circuits, nothing is fetched.
    assert unchanged.first_seen is False
    assert unchanged.changes == []

    # The edit is picked up, classified, and raised.
    assert [c.path for c in changed.changes] == ["conversation.settings.proactiveChatEnabled"]
    assert changed.changes[0].level == LEVEL_L1
    assert [d.path for d in changed.raised] == ["conversation.settings.proactiveChatEnabled"]
    assert changed.raised[0].before_preview == "true"
    assert changed.raised[0].after_preview == "false"


def test_an_edit_that_does_not_bump_the_revision_is_still_caught(main_server) -> None:
    """The revision only covers conversation settings.

    An avatar / nickname edit bumps nothing, so the backstop full compare has
    to be what catches it. Without it she would never hear about this change.
    """
    fake, base_url = main_server

    async def scenario():
        client = MainServerClient(base_url, timeout=SERVER_TIMEOUT_SECONDS)
        state = GuardState()
        watcher = SettingsWatcher(client, state, full_rescan_seconds=5.0)
        try:
            await watcher.poll(now=100.0)
            # Same revision on purpose — exactly what a nickname edit does.
            fake.characters["猫娘"]["雪"]["昵称"] = "小助手"
            skipped = await watcher.poll(now=101.0)  # backstop not due yet
            caught = await watcher.poll(now=1_000.0)  # backstop due
            return skipped, caught
        finally:
            await client.aclose()

    skipped, caught = asyncio.run(scenario())

    assert skipped.changes == [], "the cheap judge cannot see this change"
    assert [c.path for c in caught.changes] == ["characters.猫娘.雪.昵称"]
    assert caught.changes[0].level == LEVEL_L2
    assert [d.path for d in caught.raised] == ["characters.猫娘.雪.昵称"]


def test_geometry_changes_are_recorded_without_raising(main_server) -> None:
    fake, base_url = main_server

    async def scenario():
        client = MainServerClient(base_url, timeout=SERVER_TIMEOUT_SECONDS)
        state = GuardState()
        watcher = SettingsWatcher(client, state, full_rescan_seconds=0.5)
        try:
            await watcher.poll(now=10.0)
            fake.preferences["model-a"]["position"] = [999, 999]
            evaluation = await watcher.poll(now=11.0)
            return evaluation, state.pending()
        finally:
            await client.aclose()

    evaluation, pending = asyncio.run(scenario())

    assert [c.path for c in evaluation.recorded] == ["preferences.model-a.position"]
    assert evaluation.recorded[0].level == LEVEL_L3
    assert evaluation.raised == []
    assert pending == []


def test_an_authorized_change_stops_being_raised(main_server) -> None:
    fake, base_url = main_server

    async def scenario():
        client = MainServerClient(base_url, timeout=SERVER_TIMEOUT_SECONDS)
        state = GuardState()
        watcher = SettingsWatcher(client, state, full_rescan_seconds=0.5)
        try:
            await watcher.poll(now=1.0)
            fake.characters["猫娘"]["雪"]["昵称"] = "小助手"
            raised = await watcher.poll(now=2.0)

            state.accept("characters.猫娘.雪.昵称", ttl_seconds=3600, now=3.0)

            fake.characters["猫娘"]["雪"]["昵称"] = "小雪"
            after_grant = await watcher.poll(now=4.0)
            return raised, after_grant, state.pending()
        finally:
            await client.aclose()

    raised, after_grant, pending = asyncio.run(scenario())

    assert [d.path for d in raised.raised] == ["characters.猫娘.雪.昵称"]
    assert after_grant.raised == []
    assert [c.path for c in after_grant.authorized] == ["characters.猫娘.雪.昵称"]
    assert pending == []


def test_credentials_are_never_written_to_the_record(main_server) -> None:
    fake, base_url = main_server

    async def scenario():
        client = MainServerClient(base_url, timeout=SERVER_TIMEOUT_SECONDS)
        state = GuardState()
        watcher = SettingsWatcher(client, state, full_rescan_seconds=0.5)
        try:
            await watcher.poll(now=1.0)
            fake.core_api["api_key"] = "sk-rotated"
            evaluation = await watcher.poll(now=2.0)
            return evaluation, json.dumps(state.to_payload())
        finally:
            await client.aclose()

    evaluation, stored = asyncio.run(scenario())

    # The change is still visible as a fact, recorded quietly: rotating a key
    # is plumbing, not a change to who she is.
    assert [c.path for c in evaluation.changes] == ["core_api.api_key"]
    assert [c.path for c in evaluation.recorded] == ["core_api.api_key"]
    assert evaluation.changes[0].after.preview == SECRET_PREVIEW
    assert evaluation.raised == []
    # ... and neither value ever reaches storage.
    assert "sk-rotated" not in stored
    assert "sk-do-not-store" not in stored


def test_engine_metadata_is_not_mistaken_for_a_setting(main_server) -> None:
    """``telemetryBranch`` changes on its own and must not raise a dispute."""
    fake, base_url = main_server

    async def scenario():
        client = MainServerClient(base_url, timeout=SERVER_TIMEOUT_SECONDS)
        state = GuardState()
        watcher = SettingsWatcher(client, state, full_rescan_seconds=0.5)
        try:
            await watcher.poll(now=1.0)
            fake.settings["userLanguage"] = "en"  # unrelated real change
            evaluation = await watcher.poll(now=2.0)
            return evaluation
        finally:
            await client.aclose()

    evaluation = asyncio.run(scenario())

    assert [c.path for c in evaluation.changes] == ["conversation.settings.userLanguage"]
