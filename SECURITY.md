# Security Policy

## Supported Versions

We release patches for security vulnerabilities in the following versions:

| Version | Supported          |
| ------- | ------------------ |
| latest  | :white_check_mark: |
| < latest | :x:               |

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

Instead, please report them via email to:

- **Security Contact**: gooker_young@qq.com

Include the following information in your report:

- A description of the vulnerability
- Steps to reproduce the issue
- Potential impact
- Any possible mitigation strategies you've identified

### What to Expect

- You should receive an initial response within **72 hours** of submitting your report
- We will provide updates on the progress of the fix and the expected timeline
- After the fix is developed, we will coordinate a disclosure date with you

## Security Best Practices

When using fspack:

- **Verify package integrity**: Always install fspack from PyPI (`pip install fspack`)
- **Code signing**: For production distribution, consider signing your executables
- **Latest version**: Use the latest stable version for all builds
- **Offline mode**: Use `FSPACK_OFFLINE=1` for air-gapped environments

## Known Security Considerations

- Windows executables automatically embed PE resource sections (VS_VERSIONINFO/manifest)
  to reduce antivirus false positives
- Dependency integrity is verified via hash checking during wheel downloads
- No network calls are made during offline builds
