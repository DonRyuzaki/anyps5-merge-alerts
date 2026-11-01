# AnyPS5 merge alerts

Posts confirmed, previously undelivered DonRyuzaki pull-request merges in boykopovar/AnyPS5 to the configured Discord channel.

The workflow is scheduled at minutes 4, 19, 34 and 49 each hour (UTC). GitHub may delay scheduled execution. It runs only on this repository's default branch, with `NOTIFIER_ENABLED=true` and the encrypted Actions secret `DISCORD_WEBHOOK_URL` configured.

Delivery history is stored in `state.json` on `automation/anyps5-merge-notifications-state`. Preserve this branch: delivered PR IDs prevent replay. An uncertain Discord POST leaves a pending record and stops automatic retries until the channel and ledger are reconciled.

Run tests with `python3 -m unittest discover -s .github/merge-notifier -p 'test_*.py'`.

This is the standalone replacement for the notifier previously hosted in DonRyuzaki/AnyPS5. The old trigger must be retired before enabling this one, with its latest delivery ledger copied first. Only one production notifier should be active. To roll back, synchronize the latest delivery history before enabling the prior host.

AI-assisted implementation and configuration.
