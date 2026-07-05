# Changelog

All notable changes to k8s-mcp are documented here. Versions follow
[Semantic Versioning](https://semver.org/) — backwards-incompatible tool
behavior changes bump the minor (we're pre-1.0).

## [Unreleased]

### Fixed
- notifier: 3x retry with exponential backoff (0.5s / 1s / 2s) on 5xx + connection errors
- notifier: payload size guard per type (Slack 40KB / WeCom 4KB / Feishu 30KB) with `truncated from N bytes` warning when capped
- notifier: requests.Session module-level connection pool (was fresh TCP+TLS per send)
- notifier: error messages unified to English (`No notifiers configured` / `Invalid level` / `Notifier X not found`)
- health._section_workloads: N+1 in multi-namespace mode — now uses `list_*_for_all_namespaces` + client-side filter (same pattern as `_section_hpa`)

### Changed
- (planned) delete_pod / delete_service / delete_ingress / delete_configmap / delete_pvc deprecated → use `delete_resource(kind=...)`
- (planned) bulk_scale / bulk_restart / bulk_set_image / bulk_delete_pvc deprecated → merged into single-tool list variants
- (planned) tool description boundaries clarified for 6 overlapping pairs
- (planned) shared HTTP connection pool + per-tool timeout split (prometheus / logs / notifier)

## [0.3.0] — 2026-07-05

### Fixed
- `list_events(warning_only=True)` no longer silently drops Warning events
  that fell below the top `limit` Normal events. Filtering now happens
  before the limit slice (was a real correctness bug in 0.2.x).
- `cluster_health_snapshot` no longer makes 4 redundant cluster-wide
  pod-list round-trips — pods are fetched once at the top and threaded
  through all the pod-using sections.
- `_section_hpa` and `_list_pods` no longer make N+1 per-namespace
  calls — they fetch cluster-wide and filter client-side.
- `bulk_*` success detection no longer relies on a fragile
  lowercase-kind prefix match; uses the structured `apply_yaml`
  records now.
- `set_resources` writes no longer silently fall back to "delete_token
  secret = change-me" defaults — `assert_write_safety()` raises at
  startup if the default is left in place.

### Added
- HTTP timeouts on `ApiClient`: 5s connect / 30s read. Previously
  infinite, so a half-dead apiserver could hang tool calls indefinitely.
- Prometheus diagnostic cache (5-min TTL per metric + URL) so repeated
  empty-result queries don't re-probe every call.
- OpenAPI schema cache (5-min TTL) for `explain_resource`.
- `__version__` exported from `k8s_mcp.__init__`; logged at startup,
  returned by `ping()`.
- `assert_write_safety()` logs explicit SECURITY warnings when
  `K8S_MCP_DELETE_TOKEN_SECRET` is left at its default and
  `read_only=False`.
- SIGTERM/SIGINT graceful shutdown in `server.py` — finishes in-flight
  tool calls before exiting.
- `formatters.format_age` / `formatters.format_relative_time` — single
  source of truth for the 5 previously-duplicated age helpers.
- `_apply_yaml_records()` — structured per-doc apply result for internal
  callers (bulk, etc.); `apply_yaml()` keeps its legacy string format.
- `tests/integration/` — opt-in end-to-end tests against a live cluster.
- GitHub Actions CI on Python 3.11 / 3.12 / 3.13 (lint + tests +
  coverage).
- `pytest-cov` coverage report (currently 79% line coverage).

### Changed
- `apply_yaml()` returns the legacy "kind/name: action" format unchanged
  for backward compat. Internal callers now use `_apply_yaml_records()`
  for the structured `{kind, name, namespace, action, error}` shape.
- `cluster_info()` no longer yields between sequential apiserver calls;
  the calls are already bounded by the new HTTP timeouts.
- All tool modules hoist their `import yaml` / `import copy` /
  `import re` / `import os` to the top of the file (no more
  function-internal imports).
- `_api_version_for()` is now imported by `wait_tool.py` from
  `generic.py` (single source).
- Server-managed metadata keys are defined once in `generic.py` as
  `_SERVER_MANAGED_METADATA_KEYS` / `_YAML_NOISE_METADATA_KEYS`.

### Internal
- 452 unit tests pass.

## [0.2.1] — 2026-06-xx
- Prometheus discovery: namespace allowlist + wide-scan fallback when
  the hardcoded candidate list yields nothing.

## [0.2.0] — 2026-05-xx
- Feishu rich-text / interactive-card notifier.
- 11-section `cluster_health_snapshot`.

## [0.1.3] — 2026-04-xx
- Dropped the port-forward subsystem.
- Restructured docs into per-section files.

## [0.1.2] — 2026-04-xx
- NodePort URL bug fix; `list_resources -o wide` columns.

## [0.1.1] — 2026-04-xx
- Initial PyPI release notes.

[Unreleased]: https://github.com/bilbilmyc/k8s-mcp/compare/0.2.1...HEAD
[0.2.1]: https://github.com/bilbilmyc/k8s-mcp/compare/0.2.0...0.2.1
[0.2.0]: https://github.com/bilbilmyc/k8s-mcp/compare/0.1.3...0.2.0
[0.1.3]: https://github.com/bilbilmyc/k8s-mcp/compare/0.1.2...0.1.3
[0.1.2]: https://github.com/bilbilmyc/k8s-mcp/compare/0.1.1...0.1.2
[0.1.1]: https://github.com/bilbilmyc/k8s-mcp/releases/tag/0.1.1
[0.3.0]: https://github.com/bilbilmyc/k8s-mcp/compare/v0.2.1...v0.3.0
