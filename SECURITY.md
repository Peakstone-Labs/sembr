# Security Policy

## Supported Versions

Until sembr reaches 1.0, only the latest `main` branch receives security fixes. After 1.0, the current major version line will be supported.

## Reporting a Vulnerability

Please use **GitHub Private Vulnerability Reporting (PVR)** to disclose security issues:

→ <https://github.com/Peakstone-Labs/sembr/security/advisories/new>

PVR is the **only** intake channel for security reports. Please do **not** open public issues, email maintainers directly, or post details on social media before a fix is published.

### What to include

- A description of the issue and its potential impact
- Steps to reproduce or a proof-of-concept
- Affected version / commit / deployment topology, if known
- Your preferred public credit name (or "anonymous")

### Response timeline

sembr is currently maintained by a single person as a side project. Response times are **best-effort** and may slip on weekends, holidays, or outside business hours. The targets below are goals, not contractual SLAs.

| Stage | Target |
| --- | --- |
| Acknowledgment | within 72 hours |
| Initial triage / severity assessment | within 7 days |
| Coordinated public disclosure | within 90 days of the initial report |

If you have not heard back within 7 days, a polite ping on the same PVR thread is welcome.

### Scope

In scope:

- The `sembr` codebase under [`Peakstone-Labs/sembr`](https://github.com/Peakstone-Labs/sembr)
- Container images we publish (once we begin publishing them)

Out of scope:

- Vulnerabilities in third-party dependencies — please report upstream
- Issues that only reproduce in misconfigured deployments (e.g. admin API exposed without auth on the public internet) when the codebase itself is correct
- Self-hosted instances operated by third parties

### Safe harbor

Security research conducted in good faith and following this policy is welcome. Don't access user data you don't own, don't degrade service for other users, and don't publicly disclose before a fix has shipped.
