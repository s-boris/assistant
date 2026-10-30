# Security

## Reporting a problem

Please report security problems privately, not in a public issue: use GitHub's private vulnerability reporting
(the "Report a vulnerability" button on this repository's Security tab). If that button is not there, open an issue
that only asks for a private contact, without any details.

Useful to include: the file and line, what an attacker could do, and how to reproduce it. A leaked credential or
personal value in this repository counts too: say where, and do not repeat the value.

## Scope

The code in this repository: the tools in `scripts/`, the hooks and guards in `hooks/`, the hub (`packages/`,
`adapters/`, `ops/hub/`), the plugins and the publish pipeline. Third-party services and the separate privacy vault
project are out of scope.

## How the setup is meant to keep data safe

- No secrets in the repository: credentials live in files outside it, mode 0600 (see `docs/secrets.md`).
- No personal values in the code: they come from a private profile file; the repository ships placeholders.
- Every published snapshot passes a privacy scan first (gitleaks, secret patterns, a personal-value denylist, PII
  detectors), and any unreviewed hit blocks the publish.
- Guard hooks (`hooks/guards/`) check risky commands before they run, and actions that leave the machine (messages to
  other people, purchases, writes to a server) wait for the owner's explicit approval.

If you run your own instance: keep `~/.secrets`, the profile and `~/.claude` private, keep the Telegram allowlist to
your own account, and review what any automation may do on your behalf.
