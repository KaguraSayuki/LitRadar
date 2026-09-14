# Repository instructions

- Read [CONTRIBUTING.md](CONTRIBUTING.md) before preparing commits. Follow the repository's existing commit style: an English `type(scope): subject` title, with a body explaining the problem, resulting behaviour, and relevant reasoning. The scope may be omitted for a change that crosses components.
- Keep each commit focused on one coherent change. Review batches are not commit boundaries; separate unrelated fixes when they can be verified independently.
- Never create Git branches with the `codex/` prefix.
- Design ordinary user flows for researchers without programming experience. Basic agent assistance may be needed for installation, but routine configuration should use web forms, clear defaults, and actionable messages. Do not require users to write YAML, JSON, regular expressions, or shell commands to operate ordinary features.
- Distinguish implemented behaviour from design proposals. Saving preferences, running a pipeline, and deploying or restarting an instance are separate actions; describe their actual effects accurately.

- Never copy personal research directions, queries, journal preferences, watched authors, seed papers, or subscription emails into tracked examples, tests, screenshots, documentation, or release materials. Use empty templates or fully synthetic fixtures, and inspect publication content and history for personal data before sharing.
