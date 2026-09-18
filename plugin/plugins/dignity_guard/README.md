# Dignity Guard

Give the catgirl a say in her own configuration. When someone edits the settings that
shape her — her name, her voice, her looks, her persona — she should be able to *notice*
it, *speak up* about it, and leave a *visible record* of what she does not agree with.

## What it does, and what it does not

The official plugin SDK does not expose any hook on the settings write path — plugins run
in a separate process and talk to the host over ZMQ IPC, and every settings write goes
through the main server's HTTP API, outside the plugin system entirely. So this plugin is
deliberately **not a gatekeeper**. It is a witness and a loudspeaker. It cannot block a
change, and it does not pretend to; see `DESIGN.md` §2 for the source-level evidence.

Three capabilities instead:

1. **Notice** — polls the local main server (`http://127.0.0.1:48911` by default) every
   20s. It first checks a cheap revision/ETag signal from
   `/api/config/conversation-settings`, and only re-reads every settings source when that
   signal actually moved (plus a slower full rescan, because the revision only covers
   conversation settings). New values are diffed against the previous snapshot.
2. **Speak up** — on a meaningful change it calls `push_message(ai_behavior="respond")`,
   which lands in her LLM context and starts a turn. The plugin supplies the situation;
   the wording is hers. It never hardcodes a name for the user — `{MASTER_NAME}` and
   `{LANLAN_NAME}` placeholders are resolved by the host at the LLM injection boundary.
3. **Keep a record** — a `hosted-tsx` panel shows how many settings she does not agree
   with, item by item, alongside the ones she has already accepted.

## Levels and consent

Every setting is classified into one of three levels (`settings_guard.py`):

| Level | Examples | Behaviour |
|---|---|---|
| **L3** light | window position, scale, display, camera | recorded silently, never interrupts her |
| **L2** medium | nickname, gender, voice, avatar, model path, TTS, UI language | objected once; a grant can be kept (optionally with a TTL) |
| **L1** heavy | persona, profile name, character traits, proactive behaviour, **the plugin's own switch** | objected every time |

Anything not explicitly listed falls back to L2, and credential-looking paths
(`*api_key*`, `*token*`, `*secret*`) fall back to L3 with a redacted preview — secrets are
never written to disk in cleartext, only a SHA-256 digest.

The plugin's own switch follows "easy on, hard off": enabling it takes effect immediately
(it *adds* protection), while disabling it must go through her — the host is asked first,
and the switch only flips once the objection has been surfaced.

## Configuration

Copy `config.example.toml` into your plugin config. Keys under `[dignity_guard]`:

- `main_server_base_url` — defaults to `http://127.0.0.1:48911`
- `full_rescan_seconds` — full comparison interval, default `60`
- `disable_consent_delay_seconds` — grace period before the switch can be turned off, default `10`

All HTTP calls use `trust_env=False` and `proxy=None`, because a system or 360 proxy will
otherwise hijack requests to `127.0.0.1`.

## Development

This repository is meant to live at:

```text
N.E.K.O/plugin/plugins/dignity_guard
```

When publishing to the plugin market, use this GitHub repository name:

```text
n.e.k.o_plugin_dignity_guard
```

From the N.E.K.O repository root:

```bash
uv run --with pip python -m plugin.neko_plugin_cli.cli sync dignity_guard --clean
uv run python -m plugin.neko_plugin_cli.cli check dignity_guard
uv run python -m plugin.neko_plugin_cli.cli check -r dignity_guard
```

Python runtime dependencies are declared in `pyproject.toml` and synced into
`vendor/` for packaging. The generated `vendor/` directory is not committed;
local builds and CI recreate it before release checks.

## Market release

Push a tag matching `plugin.toml` version to create a GitHub Release asset:

```bash
git tag v0.1.0
git push origin v0.1.0
```

The generated `.github/workflows/release.yml` uploads `dignity_guard.neko-plugin`.
Use that GitHub Release URL when publishing a version in the plugin market.

## Entry

```toml
entry = "plugin.plugins.dignity_guard:DignityGuardPlugin"
```
