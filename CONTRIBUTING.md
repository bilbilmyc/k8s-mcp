# Contributing to k8s-mcp

[中文说明](./docs/contributing.md) · [Documentation](./docs/README.en.md)

Thanks for improving k8s-mcp. This project prioritizes safe, understandable operations over adding unguarded API coverage.

## Before opening a pull request

1. Discuss a substantial tool or behavior change in an issue first.
2. Preserve the documented runtime policy: writes must honor `read_only` when enabled and namespace scoping when configured.
3. Add or update unit tests for every behavior change.
4. Update Chinese and English core documentation in the same pull request.
5. Run the local quality gate:

```bash
uv sync --all-extras --dev
uv run ruff check .
uv run pytest -q
uv run python scripts/pre_release_check.py
```

## Tool design checklist

- Use clear action-oriented names and constrained parameters.
- Return concise, actionable information suitable for an agent context window.
- Reuse shared authentication, error sanitization, read-only, and namespace guards.
- Do not expose raw tokens, Kubernetes API error bodies, private endpoints, or webhook secrets.

## Documentation policy

The Chinese and English README, quick-start, security, deployment, and environment-reference pages are paired documents. The release check enforces their public tool count. Update both sides when changing commands, defaults, environment variables, or safety behavior.
