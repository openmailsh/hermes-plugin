# OpenMail installed

1. For a new profile, run `hermes openmail setup --mode alert`. For an existing profile, run `hermes openmail setup` to keep its mode.

   Prompts for a key from https://console.openmail.sh (any scope), picks or creates the inbox,
   asks who may email the agent (list addresses, let OpenMail's sender rules decide, or decide later),
   and writes `~/.hermes/.env`. `-y` alone never opens the sender gate; pass `--allow-all` for that.

2. `hermes gateway restart`

3. Send mail from an allowed address. In alert mode, your home channel receives an arrival notice with the thread reference. Configure a home channel with `hermes gateway setup`.

`hermes openmail doctor` checks the setup. Docs: https://docs.openmail.sh/integrations/hermes
