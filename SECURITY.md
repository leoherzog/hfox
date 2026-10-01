# Security Policy

## Supported versions

Only the latest release receives security fixes.

## Reporting a vulnerability

Report through GitHub private vulnerability reporting: open
<https://github.com/leoherzog/hfox/security/advisories/new>, or use "Report a vulnerability"
on the repository's Security tab. Do not open a public issue for a suspected vulnerability.

Include the hfox version (`hfox --version`), the command line with secrets removed, and what
you expected to happen.

## Credential storage model

`hfox auth login` writes the HappyFox API key and auth code to `token.json` in the config
directory: `$XDG_CONFIG_HOME/hfox`, else `~/.config/hfox`, unless `HFOX_CONFIG_DIR` names
another one. The file is plaintext at mode 0600 inside a directory at mode 0700. There is no
keyring integration, so anyone who can read the file as your user holds the credentials.

`HFOX_API_KEY` and `HFOX_AUTH_CODE` override the stored values and let a script or CI job run
without a token file. Pass secrets through these variables or the hidden login prompt, not on
the command line, where the shell history and the process list expose them.

## Transport

Requests use HTTP Basic auth over TLS. Redirects are not followed, so credentials stay on the
configured host. An `http` base URL on a non-loopback host sends them in cleartext; hfox
warns on stderr and proceeds. A base URL with embedded credentials (`https://user:pass@host`)
is rejected, and the error never prints a value that holds `@`, `?` or `#`.

## Guard against credential exfiltration

An AI agent driving hfox can be told by ticket or contact text to attach or post a file.
Commands therefore refuse to read a user-named file inside the config directory. The check
compares both the resolved path and the file identity, so a symlink or a hard link to
`token.json` is refused as well. Commands also refuse to send a file or stdin whose content
holds the configured credentials.

The guards do not cover the choice of host. hfox sends the credentials to the host named by
the subdomain or by a base URL from `HFOX_BASE_URL`, `token.json` or `config.toml`.
`hfox auth login --subdomain <host>` sends the API key and auth code in the environment to
that host without a prompt when stdin is not a terminal, then stores the host. Never take a
subdomain, base URL or config directory from ticket or contact text.

Ticket and contact text comes from outside your organization. Treat it as untrusted input.
