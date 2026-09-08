# OpenMail installed

1. Add your key to `~/.hermes/.env`:

   ```
   OPENMAIL_API_KEY=om_...
   OPENMAIL_ALLOW_ALL_USERS=true
   ```

   Keys: https://console.openmail.sh. Any scope works; an account key with no inbox creates one.
   Prefer an allowlist? Use `OPENMAIL_ALLOWED_USERS=a@x.com,b@y.io` instead of allow-all.

2. `hermes gateway restart`

3. Email the agent. In channel mode (default) its answer is the reply.

Docs: https://docs.openmail.sh/integrations/hermes
