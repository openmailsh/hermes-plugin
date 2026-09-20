# OpenMail installed

1. `hermes openmail setup`

   Prompts for a key from https://console.openmail.sh (any scope), picks or creates the inbox,
   asks who may email the agent (list addresses, let OpenMail's sender rules decide, or decide later),
   and writes `~/.hermes/.env`. `-y` alone never opens the sender gate; pass `--allow-all` for that.

2. `hermes gateway restart`

3. Email the agent. In channel mode (default) its answer is the reply.

`hermes openmail doctor` checks the setup. Docs: https://docs.openmail.sh/integrations/hermes
