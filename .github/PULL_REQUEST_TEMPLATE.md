## Summary

<!-- What and why? -->

## Safety and compatibility

- [ ] This change preserves the default read-only posture, or explains why it must change.
- [ ] Any new Kubernetes permissions, network destinations, or destructive behavior are documented.
- [ ] Chinese and English core documentation were updated together when applicable.

## Validation

- [ ] `uv run ruff check .`
- [ ] `uv run pytest -q`
- [ ] `uv run python scripts/pre_release_check.py`
