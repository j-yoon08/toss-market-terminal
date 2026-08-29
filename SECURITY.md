# Security Policy

## Supported versions

Security fixes are provided for the latest release on the `main` branch. Older releases may no
longer receive fixes.

| Version | Supported |
| --- | --- |
| 0.12.x | Yes |
| < 0.12 | No |

## Reporting a vulnerability

Do not disclose a vulnerability in a public Issue. Use GitHub's private vulnerability reporting
for this repository. If that option is unavailable, contact the maintainer through the GitHub
profile [@j-yoon08](https://github.com/j-yoon08) without including secrets in the first message.

Include a minimal reproduction, affected version, expected impact, and suggested mitigation when
possible. Remove or replace all of the following before sending evidence:

- Toss Securities Client ID and Client Secret
- OAuth access tokens
- account identifiers and portfolio data
- order identifiers, approval fingerprints, and live-order audit records
- local usernames, home-directory paths, and host information

Do not test a report by submitting a real order or by accessing another person's account. Prefer
`toss-market demo`, fixture credentials, and mocked HTTP transports. The maintainers will confirm
receipt and coordinate remediation and disclosure.

If a credential may have been exposed, stop the affected process and rotate or revoke the
credential through the official Toss Securities developer controls before sharing diagnostic
material.

## Security boundary

The default mode is PAPER. LIVE order submission requires an explicit live command, runtime gate,
order-specific review flow, and final confirmation. These controls reduce accidental execution but
do not make trading risk-free. This project is unofficial and is not affiliated with or endorsed
by Toss Securities or Viva Republica.
