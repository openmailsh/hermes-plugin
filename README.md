# OpenMail plugin for Hermes Agent

Gives your Hermes agent an email address. Mail sent to it wakes the agent; what the agent writes back goes out as the reply, in the same thread. The agent can also send and read mail on its own, and provision inboxes for subagents, through native tools.

```bash
hermes plugins install openmailsh/hermes-plugin --enable
hermes openmail setup
hermes gateway run
```

## Setup

`hermes openmail setup` asks for a key from [console.openmail.sh](https://console.openmail.sh); any scope works. It probes the key, picks the inbox (or creates one when you have none), and writes `~/.hermes/.env`. Two things it does that you'd otherwise have to know about:

- **It narrows the key.** Give it an account key and it mints a pod-scoped key for the chosen inbox's pod, stores that, and forgets the account key. A pod key can still create inboxes and mint inbox keys, and switching the agent to cover the whole pod later is one env change (`OPENMAIL_POD_ID`), not a trip to the console. It can't reach other pods, webhooks or account-wide policy. Pod and inbox keys stay as they are.
- **It opens Hermes's sender gate** (`OPENMAIL_ALLOW_ALL_USERS=true`). Hermes drops mail from unknown senders by default; OpenMail already decides who may write to the inbox, so the gate would only get in the way. Skip this by setting `OPENMAIL_ALLOWED_USERS` first.

Flags for scripts: `--api-key <key>`, `--api-key-stdin`, `-y` (no prompts, first inbox wins).

`hermes openmail doctor` checks the key, the inbox, the mode and the sender gate, and says what to change.

Writing `.env` by hand works too:

```
OPENMAIL_API_KEY=om_...
OPENMAIL_ALLOW_ALL_USERS=true
```

With no `OPENMAIL_INBOX_ID` or `OPENMAIL_POD_ID`, the adapter picks from what the key can see:

| Key sees | Adapter does |
| --- | --- |
| One inbox | Runs as that inbox |
| No inbox (fresh account) | Creates one |
| Several inboxes in one pod | Runs the whole pod: every inbox streams to the agent |
| Several inboxes across pods | Stops and asks for `OPENMAIL_INBOX_ID` or `OPENMAIL_POD_ID` |

## Who may write to the agent

Two gates, in order. OpenMail's allow/block rules run first, server-side; manage those in the console or with `openmail policy`. Then Hermes checks the sender against its own list, like it does for every platform:

- `OPENMAIL_ALLOWED_USERS=alice@x.com,bob@y.io`: only these addresses reach the agent.
- `OPENMAIL_ALLOW_ALL_USERS=true`: Hermes lets everyone through and OpenMail policy alone decides. This is what `setup` writes.

Hermes drops unknown senders silently; no pairing code goes out by email.

## Modes

`OPENMAIL_MODE`, default `channel`.

| Mode | Inbound mail | Agent's answer |
| --- | --- | --- |
| `channel` | Starts an agent turn | Sent as the email reply |
| `notify` | Starts an agent turn with "summarise, do not act" | Goes to your home channel (Telegram, Slack, Discord), never to the sender |
| `tool` | Ignored | None; the agent uses the tools when you ask |

Channel mode only answers mail a person could have sent. OpenMail classifies every inbound message, and the adapter hands `automated`, `marketing` and `bounce` mail to the agent as a notification instead of a reply turn. `spam` and `malicious` never reach the agent.

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

The plugin registers these in the `openmail` toolset. The API key never enters the model context.

| Tool | Does |
| --- | --- |
| `openmail_whoami` | Inboxes this agent can use, and its default |
| `openmail_send` | New thread: `to`, `subject`, `body`, optional `cc`, `attachments` |
| `openmail_reply` | Reply in a thread; recipient and subject come from the thread |
| `openmail_list_threads` | Threads in an inbox, newest first |
| `openmail_read_thread` | Every message in a thread, marks it read |
| `openmail_list_messages` | Messages in an inbox |
| `openmail_attachment_text` | Text extracted from PDF, DOCX, XLSX, images |
| `openmail_list_inboxes` | Inboxes visible to the key |
| `openmail_create_inbox` | New inbox (needs a pod or account key) |
| `openmail_create_inbox_key` | Inbox-scoped key for a subagent |

The bundled `openmail` skill covers the rest (pods, sender policy, the full API) through the `openmail` CLI.

## Subagents and Bots

A parent with a pod key creates an inbox and mints an inbox key with the two provisioning tools, then hands the key to the child. `delegate_task` children inherit the parent's env, so they can do this themselves. A [Bot](https://hermes-agent.nousresearch.com/docs/user-guide/bot-mode) is a profile with its own `.env`: give each Bot its own inbox key and each runs this plugin as its own address.

## Attachments

OpenMail extracts text server-side and the adapter inlines it (8k chars per file, 24k total). It downloads binary files under `OPENMAIL_MEDIA_MAX_MB` (default 10) into Hermes's media cache so the agent can open them, and names larger ones in the message; the agent can still pull their text with `openmail_attachment_text`.

## Reliability

Inbound rides a websocket; no public URL, no webhook. The adapter stores the last processed `event_id` in `~/.hermes/openmail/`, so a restart replays what it missed. The websocket frame is only a hint: the adapter re-fetches every message from the API before the agent sees it, so a forged frame can't impersonate a sender.

## Cron and notifications

`OPENMAIL_HOME_ADDRESS=you@example.com` lets cron jobs deliver to `openmail`. Works without the gateway running.

## Config reference

| Variable | Default | Meaning |
| --- | --- | --- |
| `OPENMAIL_API_KEY` | | Required |
| `OPENMAIL_INBOX_ID` | | Pin one inbox |
| `OPENMAIL_POD_ID` | | Run a whole pod |
| `OPENMAIL_MODE` | `channel` | `channel` / `notify` / `tool` |
| `OPENMAIL_ALLOWED_USERS` | | Sender allowlist |
| `OPENMAIL_ALLOW_ALL_USERS` | `false` | Open Hermes's sender gate |
| `OPENMAIL_HOME_ADDRESS` | | Cron delivery target |
| `OPENMAIL_MEDIA_MAX_MB` | `10` | Attachment download cap |
| `OPENMAIL_BASE_URL` | `https://api.openmail.sh` | API host |

Every variable also works under `platforms.openmail` in `config.yaml`, lower-cased without the prefix (`api_key`, `inbox_id`, `mode`). Env wins.

## Development

```bash
ln -s "$PWD" ~/.hermes/plugins/openmail && hermes plugins enable openmail
python -m venv .venv && .venv/bin/pip install pytest httpx websockets
HERMES_AGENT_DIR=~/.hermes/hermes-agent .venv/bin/pytest -c tests/pytest.ini --rootdir=tests tests
```

Requires Hermes with `httpx` and `websockets`, both in its default install.

## Related

- [OpenMail docs](https://docs.openmail.sh/integrations/hermes)
- [OpenClaw plugin](https://github.com/openmailsh/openclaw-plugin), the same design for OpenClaw
- [Agent skill](https://github.com/openmailsh/skills), bundled here
