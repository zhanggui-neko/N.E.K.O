"""Manifest, i18n and hosted-UI wiring checks.

Kept deliberately cheap: these run on every ``neko-plugin check --release``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

#: Every user-visible string the plugin can emit. The hosted panel reads the
#: same ``ui.*`` namespace through ``props.t()``.
REQUIRED_LOCALES = ("zh-CN", "en", "ja")

REQUIRED_KEYS = (
    "plugin.name",
    "plugin.description",
    "plugin.short_description",
    "panel.title",
    "entry.checkNow.name",
    "entry.checkNow.description",
    "entry.acceptSetting.name",
    "entry.acceptSetting.description",
    "entry.keepObjecting.name",
    "entry.keepObjecting.description",
    "entry.setGuardEnabled.name",
    "entry.setGuardEnabled.description",
    "entry.guardStatus.name",
    "entry.guardStatus.description",
    "actions.checkNow.label",
    "actions.acceptSetting.label",
    "actions.keepObjecting.label",
    "actions.setGuardEnabled.label",
    "speech.intro",
    "speech.item",
    "speech.outro",
    "speech.disable_request",
    "messages.guard_enabled",
    "messages.disable_requested",
    "messages.guard_disabled",
    "errors.unreachable",
    "errors.guard_disabled",
    "errors.unknown_dispute",
    "errors.consent_token_invalid",
    "errors.consent_too_early",
)


def test_plugin_manifest_exists() -> None:
    manifest = _ROOT / "plugin.toml"
    assert manifest.is_file()
    text = manifest.read_text(encoding="utf-8")
    assert 'id = "dignity_guard"' in text
    assert 'entry = "plugin.plugins.dignity_guard:DignityGuardPlugin"' in text


def test_entry_point_is_importable_and_decorated() -> None:
    from plugin.plugins.dignity_guard import DignityGuardPlugin

    assert DignityGuardPlugin.__name__ == "DignityGuardPlugin"
    assert hasattr(DignityGuardPlugin, "settings_watch")
    assert hasattr(DignityGuardPlugin, "get_dashboard")


def test_timer_literal_matches_the_reported_interval() -> None:
    """The decorator argument has to be a literal, so guard the duplication."""
    from plugin.plugins.dignity_guard import POLL_SECONDS

    source = (_ROOT / "__init__.py").read_text(encoding="utf-8")
    assert source.count("@timer_interval(") == 1, "keep a single poll cadence"
    assert f"@timer_interval(id=\"settings_watch\", seconds={POLL_SECONDS})" in source


def test_hosted_panel_is_declared_and_present() -> None:
    text = (_ROOT / "plugin.toml").read_text(encoding="utf-8")
    assert "[plugin.ui]" in text
    assert 'entry = "ui/panel.tsx"' in text
    assert 'mode = "hosted-tsx"' in text
    assert 'context = "dashboard"' in text
    assert (_ROOT / "ui" / "panel.tsx").is_file()


def test_every_locale_file_covers_the_required_keys() -> None:
    for locale in REQUIRED_LOCALES:
        path = _ROOT / "i18n" / f"{locale}.json"
        assert path.is_file(), f"missing locale file: {path.name}"
        messages = json.loads(path.read_text(encoding="utf-8"))
        missing = [key for key in REQUIRED_KEYS if key not in messages]
        assert not missing, f"{locale} is missing: {missing}"


def test_panel_copy_keys_exist_in_the_default_locale() -> None:
    """Every literal ``t("...")`` in the TSX must resolve.

    The hosted runtime falls back to returning the key itself, so a typo shows
    up as ``ui.section.pending`` rendered on screen rather than as an error.
    """
    panel = (_ROOT / "ui" / "panel.tsx").read_text(encoding="utf-8")
    used = set(re.findall(r'\bt\(\s*"([^"]+)"', panel))
    assert used, "no i18n keys found in the panel — did the copy regress to literals?"
    messages = json.loads((_ROOT / "i18n" / "zh-CN.json").read_text(encoding="utf-8"))
    missing = sorted(key for key in used if key not in messages)
    assert not missing, f"panel references undefined keys: {missing}"
