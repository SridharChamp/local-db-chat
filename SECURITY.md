# Security policy

local-db-chat connects to production databases, so security reports are taken seriously.

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's private
[security advisory form](https://github.com/SridharChamp/local-db-chat/security/advisories/new).
You'll get a reply within a few days.

Especially interested in:
- SQL that gets past the guard in `safety.py` and changes data, reads protected columns, reads files,
  or runs commands.
- Any way data could leave the machine (non-local model hosts, proxies, the web UI).
- Ways for another website or network user to reach the local web server.

## Hardening checklist for users

1. Connect with a **read-only database user** (see the README). This is the most important step.
2. Prefer a **read replica** to the primary database.
3. Keep `web_host: 127.0.0.1` unless you need network access. If you do, keep it inside your VPN.
4. List extra private columns in `sensitive_columns` (phone numbers, addresses, dates of birth).
5. Keep `config.yaml` out of version control (it is git-ignored by default).
6. Run `ldbc doctor` to see which columns are protected.

## Supported versions

Security fixes go into the latest release.
