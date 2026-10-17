# hermes-nextcloud-talk

Two independent Nextcloud Talk connectors for [Hermes Agent](https://github.com/NousResearch/hermes-agent),
packaged as one plugin that registers a single `nextcloud_talk` platform:

- **`bot/`** -- a webhook bot, using Nextcloud Talk's own
  [Bot API](https://nextcloud-talk.readthedocs.io/en/latest/bots/)
  (HMAC-SHA256 signed requests both directions). No Nextcloud user account
  for the agent; a moderator enables the bot per room with
  `occ talk:bot:setup`.
- **`user/`** -- a genuine Nextcloud user account (an app password, not a
  webhook), long-polling the Chat REST API the way a human client would.
  Supports DMs, federation invitations (allowlisted), and every room the
  account is a member of.

They are not merged into one adapter class -- a webhook receiver and a
long-polling REST client are different I/O models, and forcing a shared
base class across them would make both harder to read and riskier to
change independently. What they share (room modes, chunking, the
working-reaction UX, the allowlist env vars) is registered once at the
repo root; see [`__init__.py`](./__init__.py) for how mode selection works
and why.

Both modes support three per-room behaviors:

| Mode | Behavior |
|---|---|
| `mention` (default) | Reply only when the agent is @-mentioned (rich mention parameter, or plain-text `@name`/`@"Display Name"`). |
| `watch` | Buffer every message; a periodic background task decides whether anything is worth saying (reply with the literal token `NO_REPLY` to say nothing -- the adapter intercepts and discards it). |
| `free` | Reply to every message (a dedicated room for the agent). DMs always run in `free` mode in user mode, since a DM has no third party to be quiet in front of. |

## Which mode do I want?

- **Bot mode** if you want a clean, no-personal-account integration that
  a moderator opts specific rooms into, and you are comfortable running an
  inbound HTTP listener (even a private one) for the webhook.
- **User mode** if you want the agent to behave like any other Nextcloud
  user -- joinable to rooms by invite, usable in DMs, reachable across
  federated servers -- without exposing any inbound port at all (it only
  makes outbound long-poll requests).

You can run only one mode per Hermes instance (both register the same
platform name). Set whichever credential applies and the plugin picks the
matching mode automatically (see "Mode selection" below).

## Setup

### Bot mode

1. On the Nextcloud server, install the bot:
   ```
   occ talk:bot:install "Hermes" "<64+ char secret>" "https://your-hermes-host:18797/webhooks/nextcloud-talk" --feature webhook --feature response
   ```
   `--feature webhook` lets Talk call the URL for every message;
   `--feature response` lets the bot post replies. Generate the secret
   with something like `openssl rand -hex 32`.
2. Enable the bot in specific rooms (it is not enabled everywhere by
   default):
   ```
   occ talk:bot:setup <bot-id> <room-token>
   ```
3. On the Hermes side, set:
   ```
   NEXTCLOUD_TALK_SECRET=<the same 64+ char secret>
   NEXTCLOUD_TALK_BASE_URL=https://your-nextcloud-instance.example.com
   ```
   and optionally `NEXTCLOUD_TALK_HOST` / `NEXTCLOUD_TALK_PORT` /
   `NEXTCLOUD_TALK_PATH` to match the URL you registered above (defaults:
   `127.0.0.1:18797/webhooks/nextcloud-talk` -- change the host to a
   private interface your Nextcloud server can actually reach; never bind
   a public interface to this).

See [`bot/plugin.yaml`](./bot/plugin.yaml) for every optional env var
(per-room modes, mention trigger words, allowlisting, reaction emoji).

### User mode

1. Create a dedicated Nextcloud account for the agent (do not reuse a
   human account).
2. Generate an app password for it: **Settings -> Security -> Devices &
   sessions -> Create new app password**. Use the app password, never the
   account's real login password.
3. On the Hermes side, set:
   ```
   NEXTCLOUD_TALK_SERVER_URL=https://your-nextcloud-instance.example.com
   NEXTCLOUD_TALK_APP_PASSWORD=<the app password>
   ```
4. Invite the account to rooms normally, or set up server-to-server
   federation and invite it from a remote instance (see "Federation"
   below).

See [`user/plugin.yaml`](./user/plugin.yaml) for every optional env var
(room modes, federation inviter allowlist, discovery interval, reaction
emoji, home channel for cron delivery).

### Federation (user mode)

Nextcloud Talk supports inviting a user on another federated server into
a room. This adapter auto-accepts a pending federation invitation **only**
from an inviter on `NEXTCLOUD_TALK_ALLOWED_INVITERS` (comma-separated;
matches either the bare local username or the full `user@remote-instance`
cloud id). Everyone else's invitation is left pending -- never
auto-declined, never silently accepted -- so a human still has to look at
it. Once accepted, the federated room is discovered and polled exactly
like a local one; `_extract_actor()` keeps a federated sender's fully
qualified cloud id so downstream memory/attribution can tell local
senders from federated ones.

## Mode selection

The root `register(ctx)` picks a mode once, by which credential is set:

- `NEXTCLOUD_TALK_APP_PASSWORD` set (and no secret) -> user mode.
- `NEXTCLOUD_TALK_SECRET` set (and no app password) -> bot mode.
- Both set -> `NEXTCLOUD_TALK_MODE=bot` or `NEXTCLOUD_TALK_MODE=user`
  breaks the tie (bot wins if that is also unset).
- Neither set -> the platform reports not-configured; Hermes does not try
  to connect it.

## Installing

Combined (both modes available, pick one via credentials as above):

```
mkdir -p ~/.hermes/plugins/nextcloud_talk
cp -r __init__.py plugin.yaml bot user ~/.hermes/plugins/nextcloud_talk/
```

Bot mode only:

```
mkdir -p ~/.hermes/plugins/nextcloud_talk_bot
cp bot/adapter.py bot/core.py bot/plugin.yaml ~/.hermes/plugins/nextcloud_talk_bot/
cp bot/__init__.py ~/.hermes/plugins/nextcloud_talk_bot/__init__.py
```

User mode only:

```
mkdir -p ~/.hermes/plugins/nextcloud_talk_user
cp user/adapter.py user/plugin.yaml ~/.hermes/plugins/nextcloud_talk_user/
cp user/__init__.py ~/.hermes/plugins/nextcloud_talk_user/__init__.py
```

## Repo layout

- `__init__.py`, `plugin.yaml` -- combined plugin entry point (mode
  dispatch) and manifest.
- `bot/` -- webhook bot connector. `core.py` is pure logic (HMAC
  sign/verify, mention detection, payload parsing, dedupe, per-room
  buffer, watch-mode rate limiting), zero dependency on the Hermes
  gateway, fully unit-tested on its own. `adapter.py` is the
  `BasePlatformAdapter` subclass.
- `user/` -- real-user connector. `adapter.py` has both the pure helpers
  and the adapter class (it is a single, much simpler transport than the
  bot's webhook/HMAC machinery, so it was not worth splitting into a
  separate core module).
- `tests/bot/`, `tests/user_mode/` -- each mode's tests (the user-mode test
  directory is not named `tests/user` to avoid shadowing the `user/`
  package on `sys.path`), against
  `tests/stubs/gateway/` (a minimal, dependency-free stand-in for the
  pieces of Hermes's `gateway` package these adapters use -- no real
  Hermes checkout required to run either suite).

## Running the tests

```
pip install aiohttp httpx pytest pytest-asyncio
python -m pytest tests/ -q
```

## Security model

See [SECURITY.md](./SECURITY.md).

## License

MIT, see [LICENSE](./LICENSE).
