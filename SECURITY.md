# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.3.x   | :white_check_mark: |
| 0.2.x   | :white_check_mark: |
| < 0.2   | :x:                |

## Reporting a Vulnerability

We take security vulnerabilities seriously. If you discover a security issue, please report it responsibly.

### How to Report

1. **Do NOT** create a public GitHub issue for security vulnerabilities
2. Use [GitHub Security Advisories](../../security/advisories/new) to report vulnerabilities privately
3. Include in your report:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
   - Any suggested fixes (optional)

### What to Expect

- **Acknowledgment**: Within 48 hours of your report
- **Initial Assessment**: Within 5 business days
- **Resolution Timeline**: Depends on severity
  - Critical: 24-48 hours
  - High: 7 days
  - Medium: 30 days
  - Low: 90 days

### After Resolution

- We will credit reporters in release notes (unless anonymity is requested)
- A security advisory will be published for significant vulnerabilities
- Affected versions will be documented in the advisory

## Security Measures

This project implements several security measures:

### Network Security
- **SSRF Protection**: Blocks requests to private IP ranges (10.x, 172.16-31.x, 192.168.x) and cloud metadata endpoints (169.254.169.254)
- **URL Validation**: Scheme and format validation before processing
- **DNS Resolution**: Pre-flight DNS resolution to prevent DNS rebinding attacks

### Input Validation
- **URL Scheme Blocking**: `file://`, `javascript:`, and other dangerous schemes are blocked
- **Content Size Limits**: Configurable maximum download size (default: 50MB)
- **Rate Limiting**: Per-endpoint rate limits to prevent abuse

### Authentication & Authorization
- **Optional API Key**: Environment-based authentication via `DOWNLOADER_KEY`
- **No Default Credentials**: Authentication is disabled by default for development

### Browser Isolation (Playwright)
- **Process Isolation**: Each browser context runs in isolation
- **Memory Limits**: 512MB maximum per browser context via `--js-flags`
- **Disabled Features**: WebGL disabled, web security enforced
- **File URL Blocking**: `file://` URLs explicitly blocked in browser rendering

### Configuration Security
- **No Secrets in Code**: All sensitive configuration via environment variables
- **Secure Defaults**: Production-safe defaults for security-sensitive settings
- **CORS Secure by Default**: Only localhost origins allowed by default; production requires explicit configuration
- **Secrets Detection**: Pre-commit hooks scan for accidentally committed secrets using [detect-secrets](https://github.com/Yelp/detect-secrets)

## Security-Related Configuration

See the [Deployment Guide](docs/guides/deployment.md) for production security configuration including:

- **CORS configuration**: Default allows only localhost; must be explicitly configured for production domains
- Rate limiting settings
- Authentication setup
- Network isolation recommendations

### Secrets Detection

Pre-commit hooks automatically scan for accidentally committed secrets:

- Runs on every `git commit`
- Detects API keys, passwords, private keys, and high-entropy strings
- Baseline file (`.secrets.baseline`) whitelists known false positives

If you find a secret in the repository history, please report it through our [security vulnerability process](#reporting-a-vulnerability).

## Disclosure Policy

We follow coordinated disclosure:

1. We will work with you to understand and resolve the issue
2. We ask that you give us reasonable time to address issues before public disclosure
3. We will coordinate the timing of any public disclosure with you
4. We appreciate responsible disclosure and will acknowledge your contribution
