# OpenMail plugin for Hermes Agent

Gives your Hermes agent an email address. Mail to it wakes the agent; the agent's answer goes out as the reply, in the same thread. Native tools let the agent send, read and provision inboxes on its own.

```bash
hermes plugins install openmailsh/hermes-plugin --enable
hermes openmail setup
hermes gateway run
```

## Setup

1. Create an API key at [console.openmail.sh](https://console.openmail.sh). Any scope.
2. Run `hermes openmail setup` and paste it. Pick an inbox, or let it create one.
3. Run `hermes gateway run`, then email the agent.

Check the result any time with `hermes openmail doctor`.

<details>
<summary>What setup writes, and how to do it by hand</summary>

Two lines in `.env`:

```
OPENMAIL_API_KEY=om_...
OPENMAIL_ALLOW_ALL_USERS=true
```

The second opens Hermes's sender gate; see [Who may write](#who-may-write-to-the-agent). Setup skips it when `OPENMAIL_ALLOWED_USERS` is already set.

**Key scope.** Any key works. An account key is swapped for a pod-scoped key over the chosen inbox's pod and never stored; the pod key can still create inboxes, mint inbox keys and cover the whole pod later (`OPENMAIL_POD_ID`), but can't reach other pods, webhooks or account-wide policy. Pod and inbox keys are stored as-is.

**Which inbox.** Without `OPENMAIL_INBOX_ID` or `OPENMAIL_POD_ID`, the adapter goes by what the key sees:

| Key sees | Adapter does |
| --- | --- |
| One inbox | Runs as it |
| No inbox | Creates one |
| Several inboxes, one pod | Runs the whole pod |
| Several inboxes, several pods | Stops; asks for `OPENMAIL_INBOX_ID` or `OPENMAIL_POD_ID` |

**Scripts.** `--api-key <key>`, `--api-key-stdin`, `-y` (no prompts, first inbox wins).

</details>

## Who may write to the agent

Two gates. OpenMail's allow/block rules run first, server-side; manage them in the console or with `openmail policy`. Then Hermes checks its own list, as on every platform:

- `OPENMAIL_ALLOWED_USERS=alice@x.com,bob@y.io`: only these reach the agent.
- `OPENMAIL_ALLOW_ALL_USERS=true`: OpenMail policy alone decides. `setup` writes this.

Hermes drops unknown senders silently; no pairing code goes out by email.

## Modes

`OPENMAIL_MODE`, default `channel`.

| Mode | Inbound mail | Agent's answer |
| --- | --- | --- |
| `channel` | Starts a turn | Sent as the email reply |
| `notify` | Starts a turn: "summarise, do not act" | Goes to your home channel (Telegram, Slack, Discord), never to the sender |
| `tool` | Ignored | None; tools only, on request |

Channel mode answers only mail a person could have sent. OpenMail classifies each message; `automated`, `marketing` and `bounce` reach the agent as a notification, `spam` and `malicious` not at all.

Per-inbox override, pod scope only, in `~/.hermes/config.yaml`:

```yaml
platforms:
  openmail:
    inboxes:
      sales@omail.sh: { mode: channel }
      alerts@omail.sh: { mode: notify }
      archive@omail.sh: { mode: tool }
```

## Tools

Toolset `openmail`. The API key never enters the model context.

| Tool | Does |
| --- | --- |
| `openmail_whoami` | Inboxes this agent can use, and its default |
| `openmail_send` | New thread: `to`, `subject`, `body`, optional `cc`, `attachments` |
| `openmail_reply` | Reply in a thread; recipient and subject come from it |
| `openmail_list_threads` | Threads in an inbox, newest first |
| `openmail_read_thread` | Every message in a thread; marks it read |
| `openmail_list_messages` | Messages in an inbox |
| `openmail_attachment_text` | Text from PDF, DOCX, XLSX, images |
| `openmail_list_inboxes` | Inboxes visible to the key |
| `openmail_create_inbox` | New inbox (pod or account key) |
| `openmail_create_inbox_key` | Inbox-scoped key for a subagent |

The bundled `openmail` skill covers the rest (pods, policy, the full API) through the `openmail` CLI.

## Subagents and Bots

A parent with a pod key creates an inbox, mints an inbox key, hands it to the child. `delegate_task` children inherit the parent's env and can do this themselves. A [Bot](https://hermes-agent.nousresearch.com/docs/user-guide/bot-mode) has its own `.env`: give each its own inbox key and each runs as its own address.

## Attachments

OpenMail extracts text server-side; the adapter inlines it (8k chars per file, 24k total) and downloads binaries into Hermes's media cache so the agent can open them.

## Reliability

Inbound rides a websocket: no public URL, no webhook. The last `event_id` is kept in `~/.hermes/openmail/`, so a restart replays what it missed. The frame is only a hint; the adapter re-fetches every message from the API first, so a forged frame can't impersonate a sender.

## Cron and notifications

`--deliver openmail` sends a job's output to `OPENMAIL_HOME_ADDRESS`; `--deliver openmail:alice@x.com` sends it to any address. Works with the gateway running or not.

## Development

```bash
ln -s "$PWD" ~/.hermes/plugins/openmail && hermes plugins enable openmail
python -m venv .venv && .venv/bin/pip install pytest httpx websockets
HERMES_AGENT_DIR=~/.hermes/hermes-agent .venv/bin/pytest -c tests/pytest.ini --rootdir=tests tests
```

Needs Hermes with `httpx` and `websockets`; both ship with it. `OPENMAIL_BASE_URL` points the plugin at another API host.

Every `OPENMAIL_*` variable also works under `platforms.openmail` in `config.yaml`, lower-cased without the prefix (`api_key`, `inbox_id`). Env wins.

## Related

- [OpenMail docs](https://docs.openmail.sh/integrations/hermes)
- [OpenClaw plugin](https://github.com/openmailsh/openclaw-plugin), same design for OpenClaw
- [Agent skill](https://github.com/openmailsh/skills), bundled here
