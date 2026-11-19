# OpenMail plugin for Hermes Agent

Gives your Hermes agent an email address. Mail to it wakes the agent; the agent's answer goes out as the reply, in the same thread. Native tools let the agent send, read and provision inboxes on its own.

```bash
hermes plugins install openmail --enable
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
OPENMAIL_ALLOWED_USERS=alice@x.com,bob@y.io
```

The second is Hermes's sender gate; see [Who may write](#who-may-write-to-the-agent). Setup asks: list addresses (default), let [OpenMail's sender rules](https://console.openmail.sh/sender-rules) decide (writes `OPENMAIL_ALLOW_ALL_USERS=true`), or decide later. Skipped when a sender setting already exists.

**Key scope.** Any key works. An account key is swapped for a pod-scoped key over the chosen inbox's pod and never stored; the pod key can still create inboxes and cover the whole pod later (`OPENMAIL_POD_ID`), but can't reach other pods, webhooks or account-wide policy. Pod and inbox keys are stored as-is. With `--inbox <id or address>` the key is narrowed further, to that one inbox.

**Which inbox.** Without `OPENMAIL_INBOX_ID` or `OPENMAIL_POD_ID`, the adapter goes by what the key sees:

| Key sees | Adapter does |
| --- | --- |
| One inbox | Runs as it |
| No inbox | Creates one |
| Several inboxes, one pod | Runs the whole pod |
| Several inboxes, several pods | Stops; asks for `OPENMAIL_INBOX_ID` or `OPENMAIL_POD_ID` |

**Scripts.** `--api-key <key>`, `--api-key-stdin`, `-y` (no prompts, first inbox wins, sender gate left closed), `--allow-all` (open the sender gate), `--inbox <id or address>` (run as exactly this inbox, store an inbox-scoped key).

</details>

## Who may write to the agent

Two gates. OpenMail's allow/block rules run first, server-side; manage them in the console or with `openmail policy`. Then Hermes checks its own list, as on every platform:

- `OPENMAIL_ALLOWED_USERS=alice@x.com,bob@y.io`: only these reach the agent.
- `OPENMAIL_ALLOW_ALL_USERS=true`: OpenMail's sender rules alone decide. `setup` writes this on `--allow-all` or when you choose it in the menu.

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
| `openmail_send` | New thread: `to`, `subject`, `body`, optional `cc`, `bcc`, `attachments` (paths under `~/.hermes/media` or `~/.hermes/output` only) |
| `openmail_create_draft` | Save an unsent email in its sending inbox |
| `openmail_list_drafts` | Find drafts across accessible inboxes or filter by inbox, pod and source thread |
| `openmail_get_draft` | Review the sender, recipients, content, files and version |
| `openmail_edit_draft` | Revise recipients, subject or HTML using the saved version, without sending |
| `openmail_send_draft` | Send the reviewed version from its owning inbox |
| `openmail_delete_draft` | Permanently discard the reviewed draft and its attachments without sending |
| `openmail_reply` | Reply in a thread; recipient and subject come from it |
| `openmail_list_threads` | Threads in an inbox, newest first |
| `openmail_read_thread` | Every message in a thread; marks it read |
| `openmail_list_messages` | Messages in an inbox |
| `openmail_attachment_text` | Extract text using a message ID and attachment ID |
| `openmail_list_inboxes` | Inboxes visible to the key |
| `openmail_create_inbox` | New inbox (pod or account key) |

The bundled `openmail` skill covers administration (sender rules, pods, deleting inboxes, feedback) through the `openmail` CLI.

## Drafts

When asked to discard a draft, read it with `openmail_get_draft`, then call `openmail_delete_draft` with `draft_id` and its exact `updatedAt` as `updated_at`. Optional `inbox_id` checks ownership. Deletion removes its files too and cannot cancel a send. Stale versions require reviewing the current draft before another attempt; pending or accepted sends block deletion. Never automatically fetch a newer version or retry deletion. Missing drafts return an error. Deploy the public `DELETE /v1/drafts/{draftId}` API before releasing this tool.

Pass an explicit `inbox_id` to `openmail_create_draft`. It fixes the sending inbox when the draft is saved. Recipients, subject and HTML are optional. Both creation and editing accept only `body_html` for body content; the API generates plain text. Legacy `body` inputs are rejected. Attach local files with `attachments` or embed raster images in `body_html`. For a reply, pass `thread_id` to inherit the thread’s subject. For a forward, pass only `source_thread_id` to link the draft to the original conversation and send in a new thread.

For example, save a reply for review with `openmail_create_draft`.

```json
{"inbox_id": "inb_support", "thread_id": "thr_case", "to": "customer@example.com", "body_html": "<p>We received your request.</p>"}
```

Call `openmail_list_drafts` with no filters to see drafts across accessible inboxes. Combine `inbox_id`, `pod_id`, `source_thread_id` and `query` to narrow the list. Before sending, read the draft with `openmail_get_draft` and pass its exact `updatedAt` as `updated_at` to `openmail_send_draft`. The server sends the saved content and attachments from the owning inbox.

To revise a saved draft, read it first, then call `openmail_edit_draft` with its `draft_id`, exact `updatedAt` as `updated_at`, and only the fields to change. For example:

```json
{"draft_id": "draft_id", "updated_at": "2026-11-18T10:00:00.000Z", "body_html": "<p>Revised proposal.</p>"}
```

Use `to: null`, `cc: []` or `bcc: []` to clear recipients, and an empty subject or `body_html` to clear content. The sending inbox and reply context stay fixed; optional `inbox_id` only checks ownership. On a stale version, read and reconcile current content before another edit. The tool never substitutes a new version, retries automatically or sends mail. Deploy the public draft editing and deletion endpoints and HTML-only draft creation before releasing this plugin.

## Subagents and Bots

- **Subagents** (`delegate_task`) run in the same process with the same key and tools. Give one its own address by creating an inbox (`openmail_create_inbox`) and passing that `inbox_id` on send, reply and list.
- **[Bots](https://hermes-agent.nousresearch.com/docs/user-guide/bot-mode)** are Hermes profiles with their own `.env`. Give each its own inbox-scoped key:

  ```bash
  openmail inbox create --mailbox-name research --display-name "Research"   # once, from your own machine
  hermes -p research plugins install openmail                               # plugins are per profile
  hermes -p research openmail setup --api-key-stdin --inbox research@<your-domain> -y < pod-key.txt
  ```

  Setup writes an inbox-scoped key to the profile's `.env`; the pod key on stdin is not stored.

## Attachments

OpenMail extracts text server-side. Hermes includes up to 8k characters per file and 24k per message, and stages binaries in its media cache. Find the message ID and each attachment's ID in the notification, `openmail_read_thread`, or `openmail_list_messages`. To read one of two files named `report.pdf`, pass the selected attachment's `id` as `attachment_id` and the message's `id` as `message_id` to `openmail_attachment_text`.

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
