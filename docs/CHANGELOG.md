# Changelog

All notable changes to k8s-mcp are documented here. Versions follow
[Semantic Versioning](https://semver.org/) — backwards-incompatible tool
behavior changes bump the minor (we're pre-1.0).

## [0.5.3] — 2026-07-07

### Added — `top_pods` / `top_nodes` 3-tier cascade

`top_pods` and `top_nodes` no longer hard-fail when metrics-server isn't
installed. They walk a 3-tier cascade so `kubectl top` works on any
cluster that has at least *one* of: metrics-server, Prometheus
(cAdvisor + node-exporter), or write permission to `kube-system`:

1. **metrics-server** — `/apis/metrics.k8s.io/v1beta1/...` aggregation
   layer (the canonical `kubectl top` data source, fastest path).
2. **Prometheus fallback** — when metrics-server 404s, fall through to
   Prometheus using `container_cpu_usage_seconds_total[5m]` /
   `container_memory_working_set_bytes` (cAdvisor, Pods) and
   `node_cpu_seconds_total{mode!="idle"}[5m]` /
   `node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes`
   (node-exporter, Nodes). On the Prometheus path, `label_selector=` is
   translated to a pod-name regex by listing pods once (extra apiserver
   call) — no matches → namespace-only filter with a footer notice.
3. **`bootstrap_metrics_server` auto-bootstrap** — when both data sources
   fail AND `K8S_MCP_READ_ONLY=false` AND `kube-system` is in
   `K8S_MCP_NAMESPACE_ALLOWLIST`, apply the upstream
   `components.yaml`, patch `--kubelet-insecure-tls` for self-hosted
   kubelets, wait for Deployment ready, then retry path 1. One-shot per
   process: a failed bootstrap does NOT retry on every subsequent
   `top_pods` call (avoids hammering the apiserver when an agent loops).
   Restart the MCP server to retry.

When all three paths fail, `top_pods` / `top_nodes` raise a `RuntimeError`
that **literally lists the next-step tool names** (per the failure-path
output promotion rule): `bootstrap_metrics_server`,
`find_prometheus_service()`, and `prometheus_query(<PromQL>,
prometheus_url=<URL>)` — plus the literal `kubectl apply -f` install
command for users who'd rather install metrics-server by hand.

### Added — `bootstrap_metrics_server` tool

Public tool (also auto-invoked by the cascade):

```python
bootstrap_metrics_server(
    manifest_url: str | None = None,        # default upstream release URL
    kubelet_insecure_tls: bool = True,      # patch the Deployment after apply
    wait_seconds: int = 30,
) -> str
```

- Idempotent: probes `Deployment/metrics-server` first; if it exists,
  returns `status=AlreadyInstalled` without re-applying.
- Patches `--kubelet-insecure-tls` by default (self-hosted single-node
  clusters and most non-EKS/GKE/AKS distros ship kubelets with
  self-signed serving certs; without the flag metrics-server can't scrape
  them and `top` returns empty).
- Honors `K8S_MCP_NAMESPACE_ALLOWLIST` — refuses with `PermissionError`
  when `kube-system` isn't allowed (so a misconfigured allowlist
  refuses cluster-bootstrap rather than silently applying).
- Manifest override via `K8S_MCP_METRICS_SERVER_MANIFEST_URL` env var
  (offline / air-gapped installs point at a self-hosted copy).
- Returns a multi-line summary including `status=Installed |
  AlreadyInstalled`, the manifest URL, kubelet-insecure-tls flag,
  wait duration, and `desired/ready/available` replica counts.

### Fixed

- `_fmt_mem(0)` now returns `"0"` (was `"0B"`); the trailing `B` made
  the sort key round-trip through `_parse_mem` raise `ValueError`. Cost
  one byte; saved a confusing crash.

### Tests

- New `tests/test_metrics.py` — 15 tests covering all three cascade
  paths, label_selector translation, both-failed-read-only error
  message, both-failed-allowlist error, auto-bootstrap when perms
  allow, one-shot bootstrap gate, and `bootstrap_metrics_server` itself
  (happy path, idempotent Deployment-exists, READ_ONLY rejected,
  allowlist rejected, custom manifest URL).
- `tests/test_tool_inventory.py` — `EXPECTED_TOOL_COUNT = 73` (was 72);
  added `bootstrap_metrics_server`.

Total: 643 tests passing (was 630; +15 metrics tests, +1 tool inventory
constant, net minus ~3 obsolete import / helper tests dropped earlier
this session).

## [0.5.2] — 2026-07-07

### Changed — `delete_resource` is now single-step

Removed the two-step `preview → HMAC confirmation_token → execute` flow.
`delete_resource(kind, name, namespace=None, grace_period_seconds=30)` now
executes directly. In an LLM-driven MCP scenario the same agent both
issues and submits the confirmation token, so HMAC verification adds no
real defense — it's ceremony that costs configuration surface and
runtime cost without raising the security bar.

Gating is now solely:

- `K8S_MCP_READ_ONLY=true` — global kill switch, raises `PermissionError`
  for every write tool.
- `K8S_MCP_NAMESPACE_ALLOWLIST` — namespace allowlist for writes;
  cluster-scoped writes (no `namespace=`) are also refused when set.

### Removed

- `K8S_MCP_DELETE_TOKEN_SECRET` setting and its `change-me` startup gate
  (`enforce_write_safety()`). Any 32-byte secret was previously required
  to enable writes; that's gone now.
- `K8S_MCP_DELETE_TOKEN_TTL_SECONDS` setting.
- All `safety.py` token helpers (`TokenError`, `issue_token`,
  `verify_token`, `make_delete_payload`, `assert_payload_matches`,
  `assert_caller_matches`).
- `client.get_caller_identity()` and its 5-minute TTL cache (was used
  only for caller-binding on the deleted token flow).
- `tests/test_caller_binding.py`, `tests/test_safety_delete.py`,
  `tests/test_write_safety.py` (covered the deleted flow).

### Fixed — `find_prometheus_service` URL on NodePort / LoadBalancer

Previously `_resolve_prometheus_url` always built `clusterIP:port` via
`_service_url(svc)`, even when matching a NodePort / LoadBalancer
Service. Test
`test_resolve_wide_scan_prefers_nodeport_over_clusterip` was literally
documenting this as expected behavior. Added `_node_internal_ip()` and
`_external_service_url()`:

- NodePort → `http://<first-node-internal-ip>:<nodePort>`
- LoadBalancer → `http://<lb-ingress>:<port>` (falls through to
  ClusterIP if no ingress is provisioned yet)
- ClusterIP / unknown → `None` (caller falls back to `_service_url`)

URL fallback chain (priority order):

1. `K8S_MCP_PROMETHEUS_URL` (if set)
2. Service candidate from the hardcoded list (now externally reachable
   when type allows)
3. Wide-scan fallback (now externally reachable when type allows)

### Tests

- New `tests/test_delete.py` — 7 tests for single-step delete (read-only
  rejection, allowlist rejection, cluster-scoped block when allowlist
  set, happy path namespaced, happy path cluster-scoped, `NotFoundError`
  → `LookupError`, unknown kind → `ValueError`).
- Updated `test_prometheus.py` — 3 new tests cover NodePort URL
  construction, the no-InternalIP fallback, and the hardcoded
  candidate's external URL.
- `test_secret_audit.py` — drop `caller_user`/`caller_uid` assertions
  (caller identity removed).
- `test_call_tool_safety_nets.py` — drop obsolete "avoid the
  enforce_write_safety token check" comment.

Total: 630 tests passing (was 655; −37 token tests + 7 new delete + 5
other minor shifts).

## [0.5.1] — 2026-07-06

## [0.5.0] — 2026-07-06

## [0.4.6] — 2026-07-06

### Added — P0 hardening (production safety nets)

Three production-grade safeguards applied uniformly at the `_K8sMCP`
`call_tool` boundary in `server.py`. None of them are opt-in — the
defaults are conservative and the gates can be lifted independently.
32 new tests (`tests/test_safety_nets.py` + `tests/test_call_tool_safety_nets.py`),
709 passing total.

- **P0-1: per-tool rate limit** — `K8S_MCP_RATE_LIMIT_RPM` (default
  `120`) caps how often any single tool can be called per minute.
  Implementation: in-memory token bucket, one bucket per tool name.
  Burst size = rpm/6 (= 20 at 120 RPM — a 10-second window worth of
  calls). A runaway agent that hammers `list_pods` cannot saturate the
  apiserver or the MCP transport; busy agents can still mix call types
  (list + describe + logs in parallel) without one starving the other.
  Process-local; restarts reset (matches the existing "server restart
  clears in-memory caches" convention). Set `0` to disable.
- **P0-2: per-call wall-clock timeout** — `K8S_MCP_TOOL_TIMEOUT_S`
  (default `60.0`) bounds how long any single tool call can run.
  Implementation: bypasses FastMCP's async wrapper and dispatches the
  registered sync tool body on the default executor, then
  `asyncio.wait_for`s the resulting future from the main loop. The
  orphan executor task is **not** cancelled (Python has no portable way
  to kill a sync thread; the kubernetes client is fully blocking),
  but the MCP request returns immediately with a `ToolTimeoutError`
  carrying `tool` and `timeout_seconds`, and the LLM can move on. The
  orphan will finish (or hit the apiserver's own timeout) in the
  background. Set `0` to disable.
- **P1-4: apiserver error sanitization** — every `kubernetes.client.rest.ApiException`
  raised by a tool body is mapped to a curated `SafeApiError` whose
  message is a one-liner with status + operation context. The raw
  `body` / `reason` (RBAC details, internal hostnames, manifest field
  paths, audit-trail fragments) is **never** exposed to the LLM. A
  short `hint` naming the next tool to try (`whoami` for 403,
  `diff_resource` for 422, `retry with backoff` for 429/503/504, ...)
  is attached so the agent can recover without re-asking the user.
  Non-`ApiException` failures surface only the exception class name
  (never the `args` — `urllib3` may embed URLs+headers there). The
  boundary normalizes both classes via `safe_apiserver_error(...)`.
  Always on; there is no opt-out (turning it off would defeat the
  point).

### Rationale

Before this, a single misbehaving agent could `kubectl` a cluster into
the ground in seconds, a slow apiserver call could pin the MCP request
forever (no clean shutdown), and every 403/422/500 leaked the
apiserver's full body to the LLM. The three gates are layered:
rate-limit rejects before the apiserver call (cheap), timeout enforces
the upper bound on a single call (medium), error sanitization keeps
the failure messages terse (cheap + critical for LLM security).
Net result: a safer production posture without changing any of the
79 tool bodies.

## [0.4.5] — 2026-07-06

### Added
- `diagnose_deployment(name, namespace="default")` — one-shot Deployment
  triage. Aggregates the ~5 calls an agent otherwise makes serially (get
  Deployment, list owned ReplicaSets, list pods under the new RS, parse
  their phases, read events) into a layered report. Sections: **Rollout**
  (desired/ready/updated/available + the `Progressing` condition's own
  verdict — `NewReplicaSetAvailable` ✅ or `ProgressDeadlineExceeded` ❌);
  **ReplicaSets** (owned RS table with pod-template-hash, desired/current/
  ready, first-container image — old-vs-new image diff is visible in one
  row); **New ReplicaSet** (ready count, pod phase table; if any pods
  are `Pending` or `CrashLoopBackOff`, the report ends with a literal
  `Next step: call diagnose_pod(name=<pod>, namespace=<ns>)` so the agent
  doesn't have to guess where to drill down); **Recent events**.
  Complements `diagnose_pod` (depth on one Pod) and
  `cluster_health_snapshot` (breadth across the cluster) with the
  missing middle layer: depth on one Deployment. Read-only. 11 new tests
  in `tests/test_diagnose_deployment.py`.

## [0.4.4] — 2026-07-06

### Changed
- **LLM-tool-selection hygiene pass** — 8 of 79 tool docstrings got a
  short "USE X NOT Y" / "Pick THIS when …" sentence inserted right
  after the first line, so that LLM tool-selection (Claude / GPT /
  Cherry Studio) sees the boundary *in the first paragraph* rather
  than having to read the full docstring. Pure docstring change, no
  tool behavior affected, 666 tests still pass. Patched:
  - `list_events` — prefer `get_events_for_object` for per-object event
    streams (apiserver-side field selector vs client-side grep)
  - `create_networkpolicy` — write only; for verifying the policy
    graph use `analyze_networkpolicy`
  - `create_serviceaccount` — `image_pull_secrets` is for private
    registries; for inspecting SA permissions use `analyze_rbac`
  - `create_role` — namespaced only; cluster-wide = `create_clusterrole`
  - `create_rolebinding` — namespaced binding; cluster-wide =
    `create_clusterrolebinding`
  - `analyze_rbac` — read-only inspection; for current caller's own
    identity use `whoami`
  - `analyze_networkpolicy` — read-only inspection; for writing use
    `create_networkpolicy`
  - `delete_pod` / `delete_pvc` / `delete_configmap` / `delete_service` /
    `delete_ingress` (all 5 deprecated since v0.4.1) — first paragraph
    now leads with `⚠️ DEPRECATED` and the v0.5.0 removal date, so the
    LLM prefers `delete_resource(kind=...)` in the candidate list

### Not doing (and why)
- No full 79-tool description rewrite: ~50% were already in the
  4-section shape (what / when / NOT-when / example) thanks to the
  v0.4.3 + Phase B/B1 work; a uniform pass would be a large diff with
  no measurable win
- No tool-name changes or multi-MCP-server split — these would break
  existing agent call history for zero observed benefit
- No description-length lint rule — adds a gate that future docstring
  edits would have to argue with, for marginal benefit

## [0.4.3] — 2026-07-06

### Added
- `diagnose_pod(name, namespace="default")` — one-shot Pod triage that aggregates the ~5 calls an agent otherwise makes serially (read pod, parse container statuses, list events, tail previous logs, check PVC binding) into a single phase-dispatched report. **Pending** → scheduling diagnosis that surfaces the kube-scheduler's own `Unschedulable` verdict (not recomputed — re-deriving per-node fit would duplicate and risk contradicting the scheduler), PVC binding status, and the pod's resource requests. **Running / CrashLoopBackOff / Error** → runtime diagnosis: per-container `state` + `lastState` (CrashLoopBackOff / ImagePullBackOff / OOMKilled / non-zero exit), restart counts with the OOM hint, and an automatic tail of the *previous* container's logs for anything crash-looping. New `diagnostics.py` module. Read-only. Complements `cluster_health_snapshot` (breadth: which pods are unhealthy cluster-wide) with depth (why *this* pod is unhealthy) — the list-vs-describe relationship.
- `analyze_networkpolicy(namespace, pod=None)` — 🔍 read-only NetworkPolicy connectivity / coverage inspector that fills the verification gap after `create_networkpolicy`. Two modes: **`pod=` view** evaluates `podSelector.matchLabels` + `matchExpressions` against pod labels, lists every selecting policy's merged ingress/egress rules (peers + ports), and reports the effective posture per direction (`🔒 default-deny` once any selecting policy lists that `policyType`, else `🔓 default-allow`). **`namespace=` only** is a coverage sweep: every pod's ingress/egress posture with open-pods highlighted as the exposure surface, plus a policy inventory with `deny-all` markers for empty-rule policies. Reports the *declared* policy graph (actual enforcement depends on the CNI plugin). `policyTypes` inference follows the K8s rule (Ingress always implied, Egress only if egress rules present).
- `explain_pod(namespace, name)` — 🧭 top-down Pod inspector that aggregates what `kubectl describe pod` won't show in one place: **owner chain** walked via `metadata.ownerReferences` up to the top controller (Deployment / StatefulSet / DaemonSet / Job, or stops earlier on a dead-end), **sibling pods** sharing the parent's pod-template labels (so you can see "is this pod alone or part of a healthy set"), and the pod **spec essentials** (node, serviceAccount, container image refs). Tolerates dangling / unknown owners without raising — the chain displays the hole (e.g. "could not resolve MyCRD/thing") and stops walking. Uses DynamicClient so CRDs are walked the same way built-ins are. New `explain.py` module.
- `analyze_resource_usage(namespace="default", kind="Pod", mode="missing_requests")` — 📊 static resource-requests auditor. Sweeps `kind` in `namespace` for the requested compliance mode: **`missing_requests`** (containers without `resources.requests` — Burstable QoS, eviction-prone), **`missing_limits`** (containers without `resources.limits` — no CPU ceiling, no memory ceiling), **`inconsistent`** (`limits < requests` for any resource — scheduler silently bumps and the manifest is almost always wrong). Workload kinds (Deployment / StatefulSet / DaemonSet) audit the pod template's containers; `kind="Pod"` skips workload-owned pods (already covered) and audits only orphan / CRD-managed pods. Numeric comparison is best-effort (`500m` / `1Gi` / `2Gi` formats); unknown suffixes fall through to a conservative flag. New `resource_usage.py` module. Read-only. Companion to `diagnose_pod` (runtime) and `cluster_health_snapshot` (breadth) — this one reports static hygiene.
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

[Unreleased]: https://github.com/bilbilmyc/k8s-mcp/compare/0.4.3...HEAD
[0.4.3]: https://github.com/bilbilmyc/k8s-mcp/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/bilbilmyc/k8s-mcp/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/bilbilmyc/k8s-mcp/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/bilbilmyc/k8s-mcp/compare/0.3.3...v0.4.0
[0.3.3]: https://github.com/bilbilmyc/k8s-mcp/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/bilbilmyc/k8s-mcp/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/bilbilmyc/k8s-mcp/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/bilbilmyc/k8s-mcp/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/bilbilmyc/k8s-mcp/compare/0.2.0...0.2.1
[0.2.0]: https://github.com/bilbilmyc/k8s-mcp/compare/0.1.3...0.2.0
[0.1.3]: https://github.com/bilbilmyc/k8s-mcp/compare/0.1.2...0.1.3
[0.1.2]: https://github.com/bilbilmyc/k8s-mcp/compare/0.1.1...0.1.2
[0.1.1]: https://github.com/bilbilmyc/k8s-mcp/releases/tag/0.1.1
[0.4.5]: https://github.com/bilbilmyc/k8s-mcp/compare/v0.4.3...v0.4.5
[0.4.6]: https://github.com/bilbilmyc/k8s-mcp/compare/v0.4.5...v0.4.6
[0.5.0]: https://github.com/bilbilmyc/k8s-mcp/compare/v0.4.6...v0.5.0
[0.5.1]: https://github.com/bilbilmyc/k8s-mcp/compare/v0.5.0...v0.5.1
[0.5.2]: https://github.com/bilbilmyc/k8s-mcp/compare/v0.5.1...v0.5.2
