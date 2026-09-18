# OpenMail installed

1. `hermes openmail setup`

   Prompts for a key from https://console.openmail.sh (any scope), picks or creates the inbox,
   asks who may email the agent (`OPENMAIL_ALLOWED_USERS`, the default), and writes `~/.hermes/.env`.
   To let OpenMail policy alone decide who gets through, pass `--allow-all` or answer yes when asked;
   `-y` alone never opens the sender gate.

2. `hermes gateway restart`

3. Email the agent. In channel mode (default) its answer is the reply.

`hermes openmail doctor` checks the setup. Docs: https://docs.openmail.sh/integrations/hermes
