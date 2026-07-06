# Changelog

All notable changes to k8s-mcp are documented here. Versions follow
[Semantic Versioning](https://semver.org/) — backwards-incompatible tool
behavior changes bump the minor (we're pre-1.0).

## [Unreleased]

### Added
- `diagnose_pod(name, namespace="default")` — one-shot Pod triage that aggregates the ~5 calls an agent otherwise makes serially (read pod, parse container statuses, list events, tail previous logs, check PVC binding) into a single phase-dispatched report. **Pending** → scheduling diagnosis that surfaces the kube-scheduler's own `Unschedulable` verdict (not recomputed — re-deriving per-node fit would duplicate and risk contradicting the scheduler), PVC binding status, and the pod's resource requests. **Running / CrashLoopBackOff / Error** → runtime diagnosis: per-container `state` + `lastState` (CrashLoopBackOff / ImagePullBackOff / OOMKilled / non-zero exit), restart counts with the OOM hint, and an automatic tail of the *previous* container's logs for anything crash-looping. New `diagnostics.py` module. Read-only. Complements `cluster_health_snapshot` (breadth: which pods are unhealthy cluster-wide) with depth (why *this* pod is unhealthy) — the list-vs-describe relationship.
- `analyze_networkpolicy(namespace, pod=None)` — 🔍 read-only NetworkPolicy connectivity / coverage inspector that fills the verification gap after `create_networkpolicy`. Two modes: **`pod=` view** evaluates `podSelector.matchLabels` + `matchExpressions` against pod labels, lists every selecting policy's merged ingress/egress rules (peers + ports), and reports the effective posture per direction (`🔒 default-deny` once any selecting policy lists that `policyType`, else `🔓 default-allow`). **`namespace=` only** is a coverage sweep: every pod's ingress/egress posture with open-pods highlighted as the exposure surface, plus a policy inventory with `deny-all` markers for empty-rule policies. Reports the *declared* policy graph (actual enforcement depends on the CNI plugin). `policyTypes` inference follows the K8s rule (Ingress always implied, Egress only if egress rules present).
- `analyze_rbac(subject=None, verb=None, resource=None, api_group=None, namespace=None)` — read-only multi-mode RBAC inspector that closes the loop with the 0.4.2 `allow_wildcard` blocker. Four modes: **subject** (forward lookup — every rule granted to a user / SA / group via any binding in scope), **verb+resource** (reverse lookup — every subject that can perform an action, plus the binding→role edge it flows through), **namespace** (list Roles + RoleBindings in a namespace), and **empty** (cluster-wide summary of Role / ClusterRole / RoleBinding / ClusterRoleBinding counts). Wildcard-bearing rules (`*` in verbs / resources / apiGroups) are surfaced in every mode as the cluster-admin risk surface. The reverse lookup flags roles that match but have no binding (`unreachable` — the rule exists but is unused). Aggregates via `DynamicClient` against `rbac.authorization.k8s.io/v1`.
- `search_resources(name_substring, namespace=None, kinds=None, label_selector=None, limit_per_kind=50, api_versions=None)` — cross-kind name substring search for the "I forgot what kind or namespace X is in" triage pattern. Defaults to ~25 built-in kinds (Pod / Deployment / Service / ...); CRDs are searchable via `kinds=[...]` + `api_versions={kind: api_version}`. Fans out per kind on a `ThreadPoolExecutor` (≥5 kinds, max 8 workers). Output table is `KIND / NAME / NAMESPACE / STATUS / AGE`, sorted by KIND then NAME. Kinds that fail (RBAC forbidden, CRD not installed) are skipped and the count surfaces in the footer.
- `add_label(kind, name, key, value, namespace=None, api_version=None)` — atomic single-label add/update via JSON Patch `add`. RFC 6901 token escaping for keys containing `/` or `~`. Touches only the targeted label; every other field (status, managedFields, other labels, annotations) is preserved.
- `remove_label(kind, name, key, namespace=None, api_version=None)` — atomic single-label remove via strategic-merge patch with `null` value (idempotent: missing label = no-op, mirrors `kubectl label foo bar-`).
- `exec_pod(pod_name, command, namespace="default", container=None, timeout_seconds=30)` — ⚠️ high-privilege batch-mode exec into a pod. Drives `kubernetes.stream.WSClient` directly to capture stdout / stderr separately and surface the real exit code. NOT a TTY / interactive shell — argv only (use `["sh", "-c", "..."]` for shell features). Wall-clock timeout closes the WebSocket; the command inside the pod may not be killed (K8s exec protocol has no cancel). Respects `K8S_MCP_READ_ONLY` + `K8S_MCP_NAMESPACE_ALLOWLIST`; trust K8s RBAC for who can pods/exec.
- `list_resources` refactored: extracted `_list_resource_rows` helper so `search_resources` can share the data path without re-parsing rendered text. No behavior change for `list_resources` callers (26 existing tests still pass).

## [0.4.2] — 2026-07-06

### Security
- **RBAC triple-wildcard blocker** — `create_role` / `create_clusterrole` now reject the dangerous `verbs=["*"] ∧ resources=["*"] ∧ apiGroups=["*"]` triple (i.e. cluster-admin) unless `allow_wildcard=True`. Footgun: a missing `resources` or `apiGroups` entry has silently granted cluster-admin to whoever applies the Role.
- **HMAC delete-token startup gate** — the server refuses to start when `K8S_MCP_DELETE_TOKEN_SECRET` is the literal default value `change-me` (or empty) with writes enabled. Real deployments must set a per-environment secret; the default was previously a soft warning that any operator could ignore.
- **Notifier URL scheme gate** — `_validate_notifier` rejects `http://`, `file://`, `gopher://`, and other non-https schemes by default, closing SSRF + cleartext-leak paths. Opt in with `K8S_MCP_NOTIFIER_URL_ALLOW_HTTP=true` for local-dev hooks.
- **Caller-bound confirmation tokens** — destructive-op tokens (`bulk_*`, `delete_resource`) now embed the MCP server's kube identity (`username` + `uid`) and reject on identity mismatch. A leaked token can no longer be replayed across MCP servers running as different ServiceAccounts.
- **Secret reveal audit log** — `get_secret_value(reveal=True)` now emits a structured audit line (`secret_reveal name=… namespace=… key=… caller_user=… caller_uid=…`). The reveal is the most damaging read in the toolset; before this it had no audit trail.

### Performance
- **Prometheus wide-scan** — replaced N×`list_namespaced_service` with one `list_service_for_all_namespaces` call. On a 50-ns cluster this is one apiserver round-trip instead of 50.
- **Bulk apply N+1 fix** — `_execute_patches` no longer re-reads each workload to verify existence before patching. The token's `matched_names` is the authoritative set.
- **`list_resources` server-side selectors** — added `field_selector=…` and `limit=…` parameters that push filtering to the apiserver, plus a footer hint when the response hits the limit so the agent knows there's more to narrow against.
- **Multi-pod log fan-out** — `_fetch_logs_multi` now uses a `ThreadPoolExecutor` (max 8 workers) when ≥ 5 pods match. Five-pod × 60ms fetches go from ~300ms serial to ~60ms parallel; below the threshold we stay serial to keep small-query call order stable for existing tests.
- **Multi-namespace events** — `_section_recent_warnings` no longer silently broadens a 2+-namespace query to cluster-wide. New `events.list_events(namespaces=[...])` fans out per-namespace and merges by last-seen desc.

### Fixed
- `health._section_recent_warnings` was passing a 2+-entry `namespaces` list to a path that collapsed to `None`, broadening the query to cluster-wide. Now properly per-namespace.
- `rollout.rollout_undo_statefulset` had a dead `if False else None` branch in the `label_selector` arg — leftover scaffolding cleaned up.
- `storage._list_matched_pvcs` had a second `.to_dict()` call after `_to_dict` already returned a plain dict, plus an unreachable fallback that could have lost `metadata.uid/labels` if it ever did fire.

### Internal
- `health._count_from_section` helper deleted — its only call site dropped the value on the floor.
- `safety.assert_caller_matches` extracted as a shared helper; `bulk._verify_bulk_token` and `storage.bulk_delete_pvc` now call it instead of duplicating the username/UID mismatch logic.
- `_wide_scan_prometheus_matches` now accepts `namespace=` and handles both single-namespace and cluster-wide discovery. `find_prometheus_service` no longer branches on namespace; the helper owns the dispatch.

## [0.4.1] — 2026-07-05

### Fixed
- `bulk_scale` / `bulk_restart` duplicated `label_selector` check removed

### Changed
- `bulk_scale` / `bulk_restart` / `bulk_set_image` / `bulk_delete_pvc` deprecated. Migration target:
  - `bulk_scale` → `scale_workload(kind="Deployment|StatefulSet", name=[...], namespace, replicas)`
  - `bulk_restart` → `restart_workload(kind="Deployment|StatefulSet", name=[...], namespace)`
  - `bulk_set_image` → `set_image(kind="Deployment|StatefulSet", name=[...], namespace, container, image)`
  - `bulk_delete_pvc` → `delete_pvc(name=[...], namespace)` (one-step, no label_selector)
  - For label_selector-based operations with the audited dry_run → confirm flow, keep using these tools until v0.5.0 removal
- `delete_pvc` now accepts a list of PVC names to delete serially

## [0.3.3] — 2026-07-05

### Changed
- `delete_pod` / `delete_service` / `delete_ingress` / `delete_configmap` / `delete_pvc` deprecated. Each return is now prefixed with `⚠️ DEPRECATED: ... will be removed in v0.5.0 — use delete_resource(kind='<Kind>') for the audited two-step flow.` Migration target: `delete_resource(kind=<Kind>, confirm=False)` → show preview → `delete_resource(... confirm=True, confirmation_token=...)`. Removal scheduled for v0.5.0

## [0.3.2] — 2026-07-05

### Fixed
- `cluster_health_snapshot`: `_section_workloads` no longer makes N+1 apiserver calls in multi-namespace mode (was: 6 kinds × N namespaces per call). Switches to `list_*_for_all_namespaces` once per kind + client-side filter, matching the existing `_section_hpa` pattern. On a 50-ns cluster this single section drops from ~300 calls to 6

## [0.3.1] — 2026-07-05

### Fixed
- notifier: 3x retry with exponential backoff (0.5s / 1s / 2s) on 5xx + connection errors (was: dropped messages on a one-off 503)
- notifier: payload size guard per type — Slack 40 KiB / WeCom 4 KiB / Feishu 30 KiB / generic 30 KiB. Above limit returns `❌ payload too large: N bytes exceeds <type> limit of M bytes` without burning 3 retry attempts on a payload the gateway will reject regardless
- notifier: `requests.Session` module-level + `HTTPAdapter(pool_connections=10, pool_maxsize=20)`. Was fresh TCP+TLS per `notify()` call
- notifier: error messages unified to English (`No notifiers configured` / `Notifier X not found` / `Invalid level` / `All configured notifiers are invalid`) so LLM agents get stable parsing

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

[Unreleased]: https://github.com/bilbilmyc/k8s-mcp/compare/0.3.3...HEAD
[0.3.3]: https://github.com/bilbilmyc/k8s-mcp/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/bilbilmyc/k8s-mcp/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/bilbilmyc/k8s-mcp/compare/v0.3.0...v0.3.1
[0.2.1]: https://github.com/bilbilmyc/k8s-mcp/compare/0.2.0...0.2.1
[0.2.0]: https://github.com/bilbilmyc/k8s-mcp/compare/0.1.3...0.2.0
[0.1.3]: https://github.com/bilbilmyc/k8s-mcp/compare/0.1.2...0.1.3
[0.1.2]: https://github.com/bilbilmyc/k8s-mcp/compare/0.1.1...0.1.2
[0.1.1]: https://github.com/bilbilmyc/k8s-mcp/releases/tag/0.1.1
[0.3.0]: https://github.com/bilbilmyc/k8s-mcp/compare/v0.2.1...v0.3.0
[0.4.1]: https://github.com/bilbilmyc/k8s-mcp/compare/v0.4.0...v0.4.1
