# OpenMail installed

1. `hermes openmail setup`

   Prompts for a key from https://console.openmail.sh (any scope), picks or creates the inbox,
   and writes `~/.hermes/.env`. Prefer a sender allowlist? Set
   `OPENMAIL_ALLOWED_USERS=a@x.com,b@y.io` afterwards; otherwise OpenMail policy decides who gets through.

2. `hermes gateway restart`

3. Email the agent. In channel mode (default) its answer is the reply.

`hermes openmail doctor` checks the setup. Docs: https://docs.openmail.sh/integrations/hermes
