# OpenMail CLI & API Reference

Base URL: `https://api.openmail.sh`  
Authentication: `Authorization: Bearer <OPENMAIL_API_KEY>`  
All responses are JSON.

---

## CLI commands

### Send email

```bash
openmail send \
  --to "alex@example.com" --to "sam@example.com" \
  --subject "Subject" \
  --body "Plain text body."

# Reply in thread (subject ignored)
openmail send \
  --to "recipient@example.com" \
  --thread-id "thr_..." \
  --body "Reply body."

# With HTML and attachments
openmail send \
  --to "recipient@example.com" \
  --subject "Report" \
  --body "See attached." \
  --body-html "<p>See attached.</p>" \
  --attach ./report.pdf \
  --attach ./data.csv
```

Flags:
- `--to` (required) — repeat once per visible To address
- `--subject` (required unless `--thread-id` is set) — email subject
- `--body` (required) — plain text body
- `--body-html` — optional HTML body
- `--thread-id` — reply in an existing thread
- `--attach` — file path to attach (repeatable)
- `--inbox-id` — override default inbox

Response:

```json
{
  "id": "msg_...",
  "threadId": "thr_...",
  "inboxId": "inb_...",
  "direction": "outbound",
  "createdAt": "2024-01-01T00:00:00.000Z"
}
```

### Threads

```bash
# List unread threads (preferred for checking new mail)
openmail threads list --is-read false

# List all threads
openmail threads list

# List with pagination
openmail threads list --limit 20 --offset 0

# Get messages in a thread (oldest-first)
openmail threads get --thread-id "thr_..."

# Mark thread as read
openmail threads read --thread-id "thr_..."

# Mark thread as unread
openmail threads unread --thread-id "thr_..."
```

Thread object:

```json
{
  "id": "thr_...",
  "inboxId": "inb_...",
  "subject": "Re: Hello",
  "lastMessageAt": "2024-01-01T00:00:00.000Z",
  "isRead": false,
  "messageCount": 3
}
```

### Messages

```bash
# List inbound messages
openmail messages list --direction inbound --limit 20

# List outbound messages
openmail messages list --direction outbound

# With pagination
openmail messages list --direction inbound --limit 50 --offset 100
```

Message object:

```json
{
  "id": "msg_...",
  "threadId": "thr_...",
  "inboxId": "inb_...",
  "fromAddr": "Alice <alice@example.com>",
  "toAddr": "agent@openmail.sh",
  "toRecipients": ["agent@openmail.sh", "sam@example.com"],
  "subject": "Hello",
  "bodyText": "Plain text content",
  "bodyHtml": "<p>HTML content</p>",
  "direction": "inbound",
  "attachments": [
    {
      "id": "att_...",
      "filename": "report.pdf",
      "url": "https://api.openmail.sh/v1/attachments/msg_.../att_...",
      "sizeBytes": 12345
    }
  ],
  "createdAt": "2024-01-01T00:00:00.000Z"
}
```

Use `toRecipients` for the visible To addresses. It can be empty.

### Folders

```bash
openmail folders list --inbox-id inb_support --json
openmail folders messages --inbox-id inb_support --folder-id 7 --limit 20 --offset 0 --json
```

Use the numeric folder ID returned for the selected inbox. The folder list includes empty folders and full paths such as `Finance/Receipts`.

### Inboxes

```bash
# List all inboxes
openmail inbox list

# Create an inbox
openmail inbox create --mailbox-name "support" --display-name "Support"

# Get inbox details
openmail inbox get --inbox-id "inb_..."

# Delete an inbox
openmail inbox delete --inbox-id "inb_..."
```

Inbox object:

```json
{
  "id": "inb_...",
  "address": "support@example.openmail.sh",
  "displayName": "Support",
  "createdAt": "2024-01-01T00:00:00.000Z"
}
```

---

## REST API

All endpoints require `Authorization: Bearer <OPENMAIL_API_KEY>`.

### POST /v1/inboxes/{id}/send

Send an email. Requires `Idempotency-Key` header (UUID).

```http
POST /v1/inboxes/{id}/send
Authorization: Bearer om_...
Content-Type: application/json
Idempotency-Key: <uuid>

{
  "to": ["alex@example.com", "sam@example.com"],
  "subject": "Hello",
  "body": "Plain text body.",
  "bodyHtml": "<p>HTML body.</p>",
  "threadId": "thr_...",
  "attachments": [
    {
      "filename": "file.pdf",
      "contentType": "application/pdf",
      "data": "<base64>"
    }
  ]
}
```

### Saved drafts

| Request | Behavior |
| --- | --- |
| `POST /v1/drafts` | Saves an unsent draft in the required `inboxId` and returns it with HTTP 201. Optional content includes `to`, `cc`, `bcc`, `subject` and `bodyHtml`. HTML is the only body input; plain text is generated automatically. |
| `GET /v1/drafts` | Lists accessible drafts newest first as `{data, total}`. Combine `inboxId`, `podId`, `sourceThreadId`, `q`, `limit` (1–100) and `offset`. |
| `GET /v1/drafts/{draftId}` | Returns the sender inbox ID, recipients, body, timestamps, thread context and attachments. Attachment metadata includes `filename`, `contentType`, `sizeBytes`, a download `url` when available, and `contentId` for inline images. |
| `PATCH /v1/drafts/{draftId}` | Requires the exact `updatedAt`; changes only supplied recipients, subject or `bodyHtml` and returns the updated unsent draft. Empty HTML clears the body. `inboxId` may check ownership but cannot change it. |
| `POST /v1/drafts/{draftId}/send` | Requires the reviewed `updatedAt` and sends from the owning inbox. The optional `inboxId` must match that owner. Returns `messageId`, `threadId` and `status`. |
| `DELETE /v1/drafts/{draftId}` | JSON body requires exact `updatedAt`; optional `inboxId` checks ownership. Permanently discards the draft and its files without sending, returning `{id, deleted: true}`. Stale versions and pending or accepted sends return 409. Missing drafts return 404. MCP OAuth requires the `send` scope, as for draft creation, editing and sending. |

To address a draft to several people, pass up to 100 addresses in `to`. Read the saved list from `toRecipients`.

Set `threadId` on creation for a reply. This inherits the reply subject and records the source thread. For a forward, set only `sourceThreadId` to keep the conversation association and send on a new thread. Both IDs refer to threads in the draft’s owning inbox and must match when supplied together.

Send JSON with `bodyHtml` for draft content. Both creation and editing reject plain-text body inputs; the API generates the fallback. The API imports embedded raster images or public HTTP(S) image URLs in the HTML and rewrites them to use stored content IDs. For local uploads on creation, send `multipart/form-data` with ordinary files under `attachments`. For each inline PNG, JPEG, GIF or WebP, send a file under `inlineImages` with its `inline-<lowercase UUID>@openmail` content ID as the multipart filename and reference it as `cid:<content ID>` in the HTML. The total attachment limit is 25 MB.

A stale `updatedAt` returns `409 draft_changed`. Read the draft and confirm its current content before sending again. Failed or pending attempts retain the draft.

### GET /v1/inboxes/{id}/threads

```
GET /v1/inboxes/{id}/threads?is_read=false&limit=20&offset=0
```

### PATCH /v1/threads/{id}

```http
PATCH /v1/threads/{id}
Content-Type: application/json

{ "is_read": true }
```

### GET /v1/threads/{id}/messages

Returns messages in a thread, sorted oldest-first.

### GET /v1/inboxes/{id}/messages

```
GET /v1/inboxes/{id}/messages?direction=inbound&limit=20&offset=0
```

### Folders

| Endpoint | Returns |
| --- | --- |
| `GET /v1/inboxes/{id}/folders` | `data` contains each folder's numeric `id`, full `name` and `role`. Custom folders have a null role. |
| `GET /v1/inboxes/{id}/folders/{folderId}/messages` | `data` contains item summaries newest first, and `total` counts all items in that folder. Each item's `kind` is `message` or `draft`. Set `limit` (1–100, default 50) and `offset` (default 0) to page through the results. |

### GET /v1/attachments/{messageId}/{attachmentId}

Use the message attachment's `url` to download its bytes with the same bearer credentials used to read the message. Append `/text` for extracted text. Each attachment has its own ID, including files with identical names. A filename in the same route selects the first matching attachment.

---

## Rate limits

| Resource | Limit |
|---|---|
| Inbox creation | 100 per day |
| Sends per inbox per minute | Starts at 10. Pro and Enterprise inboxes can reach 30 as age and sending volume increase. |
| Recipients per inbox per day | Starts at 200. Pro and Enterprise inboxes can reach 2,000. Each To, Cc and Bcc recipient counts, including on replies. |
| API requests | 1,000 per minute |

Enterprise overrides and sender reputation can change these limits. See [rate limits](https://docs.openmail.sh/concepts/rate-limits).

---

## Idempotency

The send endpoint requires an `Idempotency-Key` header. Use a unique UUID per send attempt. Retrying with the same key within 24 hours returns the original response without resending.

The CLI generates idempotency keys automatically. For direct API calls, generate a UUID:

```bash
python3 -c "import uuid; print(uuid.uuid4())"
```
