# OpenMail plugin for Hermes Agent

Gives your Hermes agent an email address. Mail sent to it wakes the agent; what the agent writes back goes out as the reply, in the same thread. The agent can also send, read and provision mail on its own through native tools.

```bash
hermes plugins install openmailsh/hermes-plugin --enable
hermes openmail setup
hermes gateway run
```

`setup` asks for a key from [console.openmail.sh](https://console.openmail.sh) (any scope), picks or creates the inbox, swaps an account key for a pod-scoped one before writing `~/.hermes/.env` (a pod key can still create inboxes and later run the whole pod; it cannot reach other pods, webhooks or account policy), and opens Hermes's sender gate (who actually gets through is OpenMail policy, see below). `hermes openmail doctor` checks the result. Flags: `--api-key`, `--api-key-stdin`, `-y` for scripts.

Setting `.env` by hand works too:

```
OPENMAIL_API_KEY=om_...
OPENMAIL_ALLOW_ALL_USERS=true
```

What the adapter does with each key scope:

| Key | What happens |
| --- | --- |
| Inbox-scoped | That inbox is the agent's address. |
| Account or pod key, one inbox visible | Uses it. |
| Account key, no inbox yet | Creates one. |
| Pod key, several inboxes | Runs the whole pod: every inbox streams to the agent. |
| Account key, several pods | Stops and asks for `OPENMAIL_INBOX_ID` or `OPENMAIL_POD_ID`. |

## Who may write to the agent

Hermes denies unknown senders by default, like every other platform. Pick one:

- `OPENMAIL_ALLOWED_USERS=alice@x.com,bob@y.io`: only these addresses reach the agent.
- `OPENMAIL_ALLOW_ALL_USERS=true`: anyone can. OpenMail's own allow/block rules still run first, server-side; manage those in the console or with `openmail policy`.

Unknown senders are dropped silently. No pairing code goes out by email.

## Modes

`OPENMAIL_MODE`, default `channel`.

| Mode | Inbound mail | Agent's answer |
| --- | --- | --- |
| `channel` | Starts an agent turn | Sent as the email reply |
| `notify` | Starts an agent turn with "summarise, do not act" | Delivered to your home channel (Telegram, Slack…), never to the sender |
| `tool` | Ignored | n/a; the agent uses the tools when you ask |

Channel mode only answers mail a person could have sent. OpenMail classifies every inbound message; `automated`, `marketing` and `bounce` mail is handed to the agent as a notification instead of a reply turn. `spam` and `malicious` never reach the agent.

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

Registered in the `openmail` toolset. The API key never enters the model context.

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
| `openmail_create_inbox` | New inbox (pod or account key) |
| `openmail_create_inbox_key` | Inbox-scoped key for a subagent |

The bundled `openmail` skill covers the rest (pods, domains, policy) via the `openmail` CLI.

## Subagents and Bots

A parent with a pod key creates an inbox and mints an inbox key with the two provisioning tools, then hands the key to the child. `delegate_task` children inherit the parent's env, so they can do this themselves. A [Bot](https://hermes-agent.nousresearch.com/docs/user-guide/bot-mode) is a profile with its own `.env`: give each Bot its own inbox key and each runs this plugin as its own address.

## Attachments

Text is extracted server-side and inlined (8k chars per file, 24k total). Binary files under `OPENMAIL_MEDIA_MAX_MB` (default 10) are downloaded into Hermes's media cache so the agent can open them. Larger ones are named in the message; the agent can fetch them with `openmail_attachment_text`.

## Reliability

Inbound rides a websocket; no public URL or webhook. The last processed `event_id` is stored in `~/.hermes/openmail/`, so a restart replays what was missed. The websocket frame is a hint: every message is re-fetched from the API before the agent sees it, so a forged frame cannot impersonate a sender.

## Cron and notifications

`OPENMAIL_HOME_ADDRESS=you@example.com` lets cron jobs deliver to `openmail`. Works without the gateway running.

## Config reference

| Env | Default | |
| --- | --- | --- |
| `OPENMAIL_API_KEY` | | Required |
| `OPENMAIL_INBOX_ID` | | Pin one inbox |
| `OPENMAIL_POD_ID` | | Run a whole pod |
| `OPENMAIL_MODE` | `channel` | `channel` / `notify` / `tool` |
| `OPENMAIL_ALLOWED_USERS` | | Sender allowlist |
| `OPENMAIL_ALLOW_ALL_USERS` | `false` | Open inbox |
| `OPENMAIL_HOME_ADDRESS` | | Cron delivery target |
| `OPENMAIL_MEDIA_MAX_MB` | `10` | Attachment download cap |
| `OPENMAIL_BASE_URL` | `https://api.openmail.sh` | |

Every key also works under `platforms.openmail` in `config.yaml` (`api_key`, `inbox_id`, …). Env wins.

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
