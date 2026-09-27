# Security Policy

## Supported Versions

Only the latest release of InkDoc receives security updates and vulnerability fixes.

| Version | Supported          |
| ------- | ------------------ |
| 1.0.x   | :white_check_mark: |
| < 1.0   | :x:                |

---

## Reporting a Vulnerability

The InkDoc team takes the security of our application and users seriously. If you discover a security vulnerability in InkDoc, please report it responsibly.

### How to Report

1. **GitHub Security Advisories (Preferred)**:
   Please report security vulnerabilities privately through GitHub's advisory feature:
   [https://github.com/AbdoslamB/inkdoc/security/advisories/new](https://github.com/AbdoslamB/inkdoc/security/advisories/new)

2. **Email**:
   If you are unable to use GitHub Advisories, please contact the repository maintainer directly via GitHub profile contact information.

**Please include the following details in your report**:
- Description of the issue and potential impact
- Affected component or endpoint (e.g., API route, engine adapter, UI)
- Step-by-step reproduction instructions or a minimal Proof of Concept (PoC)
- Any proposed mitigations or remediation steps

### Response Timeline
- **Acknowledgment**: Within 48 hours of receipt.
- **Triage & Assessment**: Within 5 business days.
- **Fix & Disclosure**: Coordinated security release once the patch is prepared and validated.

Please **do not disclose or discuss the vulnerability publicly** (such as in GitHub issues, pull requests, or social media) until a fix has been released and an advisory published.

---

## Deployment & Security Guidelines

InkDoc is designed as a **local-first desktop tool and workbench**:
- **Loopback Binding**: By default, the embedded server binds strictly to `127.0.0.1`. Never expose InkDoc on all network interfaces (`0.0.0.0`) or across public untrusted networks without placing it behind an authenticated reverse proxy (e.g. Nginx with TLS and token authentication).
- **CORS Configuration**: InkDoc restricts Cross-Origin Resource Sharing (CORS) to local loopback origins (`localhost` and `127.0.0.1`). Do not relax these restrictions in shared or multi-tenant environments.
- **URL Conversion Safety**: The `/convert/url` endpoint enforces SSRF protection, blocking local, loopback, private RFC1918/RFC4193, and cloud metadata addresses.
