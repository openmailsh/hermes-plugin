# OpenMail Error Reference

## API error format

All errors return a consistent JSON structure:

```json
{
  "error": "error_code",
  "message": "Human-readable description"
}
```

## Error codes

| Status | Code | Description | Resolution |
|--------|------|-------------|------------|
| 400 | `invalid_mailbox_name` | Mailbox name doesn't meet format requirements | Use lowercase letters, numbers, and hyphens only (e.g. `my-agent`) |
| 400 | `missing_idempotency_key` | Send endpoint requires `Idempotency-Key` header | Add a UUID header; the CLI does this automatically |
| 401 | `unauthorized` | Invalid or missing API key | Check `OPENMAIL_API_KEY` is set and starts with `om_` |
| 404 | `not_found` | Resource doesn't exist or doesn't belong to your account | Verify the inbox ID or thread ID is correct for this account |
| 409 | `address_taken` | Mailbox name or address already in use | Choose a different `--mailbox-name` |
| 422 | `recipient_suppressed` | Recipient is on the suppression list | The recipient previously unsubscribed or reported spam; do not contact them |
| 429 | `rate_limit_exceeded` | Too many requests | Check `Retry-After` header and wait before retrying |
| 429 | `cold_outreach_limit` | Cold send limit exceeded for new inbox | New inboxes have a warm-up period; see [email deliverability docs](https://docs.openmail.sh/best-practices/email-deliverability) |

## Unexpected errors — report them

A `500 internal_error` (or any response that contradicts this reference) is a problem on OpenMail's side. Report it so the team can fix it, then continue with your task:

```bash
openmail feedback --type bug --message "<what you did and what you got>" \
  --endpoint "<path>" --error-code "<error code>"
```

Reporting never blocks your work and needs no confirmation. Do not retry-loop a 500 more than a few times, and do not report the same problem more than once per session.

## CLI errors

### `missing API key (set --api-key or OPENMAIL_API_KEY)`

No key found in `--api-key`, `OPENMAIL_API_KEY`, or `./.env`. Follow `setup.md`.

### `missing inbox id; run 'openmail init' or pass --inbox-id`

No default inbox saved in `~/.openmail-cli/state.json`. Run:

```bash
openmail init --mailbox-name "agent" --display-name "Agent"
```

### `OpenMail API 401`

The API key is invalid. Verify:

```bash
echo "$OPENMAIL_API_KEY"   # or: grep OPENMAIL_API_KEY ./.env
```

The key should start with `om_`. Replace it if it is wrong or missing.

### `cannot read attachment "..."`

The file path doesn't exist or isn't readable. Check the path is correct and the file exists before sending.

### `missing --to`

The `--to` flag is required for `openmail send`. Always specify a recipient address.

### `missing --subject`

The `--subject` flag is required when sending a new email (not a thread reply). Omit it only when using `--thread-id`.

## Suppression

If you send to a suppressed address, the API returns `422 recipient_suppressed`. Suppressed addresses cannot receive email from your account because they previously unsubscribed or reported spam. Do not attempt to work around this.

To view or manage suppressions, go to **Settings > Suppressions** in the [dashboard](https://console.openmail.sh).

## Attachment URLs

Attachment URLs in inbound messages are signed and expire after a short window. Fetch them promptly after receiving a message. If a URL has expired, re-fetch the message to get a fresh URL.

## Rate limits

| Resource | Limit |
|----------|-------|
| Inbox creation | 100 per day per account |
| Send per inbox | 10 per minute, 200 per day |
| API requests | 1,000 per minute per account |

When rate limited, wait for the duration specified in the `Retry-After` header before retrying.
