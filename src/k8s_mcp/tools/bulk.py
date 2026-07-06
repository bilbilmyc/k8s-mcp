"""Bulk operations: apply a change to all workloads matching a label_selector.

Three tools, all sharing the same safety contract:

  - `bulk_set_image` — set the `image` of a named container on every
    matching Deployment / StatefulSet / DaemonSet.
  - `bulk_restart`   — trigger a rolling restart on every match (the
    `kubectl rollout restart` equivalent: patch
    `spec.template.metadata.annotations[kubectl.kubernetes.io/restartedAt]`
    to the current ISO timestamp; the controller sees the annotation
    change and rolls the Pods).
  - `bulk_scale`     — set `spec.replicas` on every match. Deployment
    and StatefulSet only (DaemonSet has no replica concept).

Safety flow — same shape as `delete_resource` (two-step + dry-run):

  1. `dry_run=True` (default): list matching resources, show current →
     planned state per resource, return. NO write, NO token.
  2. `dry_run=False, confirm=False`: re-list, render the same preview,
     plus a `confirmation_token` (HMAC-signed, default 5-min TTL).
  3. `dry_run=False, confirm=True, confirmation_token=...`: verify the
     token, then apply the change ONLY to the resources that were
     matched at preview time. New resources that appeared with the same
     label_selector between preview and confirm are NOT touched — the
     token's `matched_names` list is the authoritative scope.

The token payload records every "dangerous" parameter (image, container,
replicas, label_selector, kind, namespace) so a copy-paste of the same
token with a different image fails verification. The `matched_names`
list is the per-resource safety net against label-selector drift.

Read-only mode + namespace allowlist are checked up front (same as
`delete_resource`).

中文说明：
批量改一组同 label 的工作负载。走 `dry_run → preview → confirm` 三
步安全流程，token 里记录「这次会改哪 N 个资源」，确认时只动这 N 个
—— 即使在确认前集群里多出同 label 的 Deployment 也不会被误伤。改
image / replicas 都被 HMAC 签名覆盖，token 不能跨参数复用。

v0.4.0 起这三个工具标 `@deprecated`，v0.5.0 删除。迁移路径：单工具
（`scale_workload` / `restart_workload` / `set_image`）现在接受
`name: str | list[str]`，传入列表即可在多个同名工作负载上连用，不再需
要走 label_selector + token 三段流程（如果你确实需要 label_selector
的安全性，等到 v0.5.0 之前都还可以继续用）。
"""
from __future__ import annotations

import copy
import logging
from datetime import UTC, datetime
from typing import Any

from kubernetes.client.rest import ApiException

from ..client import get_caller_identity
from ..config import enforce_write_safety, get_settings
from ..formatters import short_table
from ..safety import (
    TokenError,
    assert_caller_matches,
    issue_token,
    verify_token,
)
from . import generic

logger = logging.getLogger(__name__)


# ---------- read-only + allowlist gate -------------------------------------


def _write_guard(namespace: str | None) -> None:
    settings = get_settings()
    if settings.read_only:
        raise PermissionError(
            "Server is in read-only mode (K8S_MCP_READ_ONLY=true). "
            "Bulk write disabled."
        )
    if not settings.ns_allowed(namespace):
        raise PermissionError(
            f"Namespace {namespace!r} not allowed by K8S_MCP_NAMESPACE_ALLOWLIST"
        )


# ---------- resource listing -----------------------------------------------


def _list_matched(
    kind: str, namespace: str | None, label_selector: str
) -> list[dict]:
    """Return matched resources as plain dicts.

    Tries the built-in `list_resources` table first to get names, then
    fetches each full spec — that lets us reuse the same CRD-aware
    resolution path the rest of the codebase uses.
    """
    dc = generic._dyn_client()
    resource = generic._resource_for_kind(dc, kind)
    get_kwargs: dict[str, Any] = {}
    if label_selector:
        get_kwargs["label_selector"] = label_selector
    if namespace:
        ret = resource.get(namespace=namespace, **get_kwargs)
    else:
        ret = resource.get(**get_kwargs)
    return [generic._to_dict(item) for item in ret.items]


def _grouped_list_names(items: list[dict]) -> list[tuple[str, str]]:
    """(namespace, name) pairs, sorted for stable token / output."""
    out = sorted(
        ((i.get("metadata", {}).get("namespace") or "", i["metadata"]["name"])
         for i in items),
        key=lambda p: (p[0], p[1]),
    )
    return out


# ---------- spec helpers ---------------------------------------------------


def _container_image(workload: dict, container: str) -> str | None:
    spec = workload.get("spec", {}) or {}
    template = spec.get("template", {}) or {}
    containers = template.get("spec", {}).get("containers", []) or []
    for c in containers:
        if c.get("name") == container:
            return c.get("image")
    return None


def _patch_image(workload: dict, container: str, new_image: str) -> dict:
    """Return a copy of `workload` with the named container's image updated."""
    out = copy.deepcopy(workload)
    spec = out.get("spec", {}) or {}
    template = spec.get("template", {}) or {}
    for c in template.get("spec", {}).get("containers", []) or []:
        if c.get("name") == container:
            c["image"] = new_image
            return out
    raise ValueError(
        f"Container {container!r} not found in {workload['metadata']['name']!r}. "
        f"Available: {[c.get('name') for c in (template.get('spec', {}).get('containers') or [])]}"
    )


def _patch_restart_annotation(workload: dict) -> dict:
    """Trigger a rolling restart by stamping the `restartedAt` annotation."""
    out = copy.deepcopy(workload)
    spec = out.get("spec", {}) or {}
    template = spec.setdefault("template", {})
    md = template.setdefault("metadata", {})
    anns = md.setdefault("annotations", {})
    anns["kubectl.kubernetes.io/restartedAt"] = datetime.now(UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return out


def _patch_replicas(workload: dict, replicas: int) -> dict:
    out = copy.deepcopy(workload)
    out.setdefault("spec", {})["replicas"] = int(replicas)
    return out


# ---------- token helpers --------------------------------------------------


def _issue_bulk_token(payload: dict) -> str:
    settings = get_settings()
    enforce_write_safety(settings)
    # Bind the token to the MCP server's authenticated identity so a
    # leaked token cannot be replayed by a different MCP process running
    # as a different user (see safety.assert_payload_matches).
    caller = get_caller_identity()
    payload = dict(payload)
    payload["caller"] = {
        "username": caller.get("username", "(unknown)"),
        "uid": caller.get("uid", ""),
    }
    return issue_token(payload, settings.delete_token_secret,
                       settings.delete_token_ttl_seconds)


def _verify_bulk_token(token: str, *, expected_op: str) -> dict:
    settings = get_settings()
    enforce_write_safety(settings)
    try:
        payload = verify_token(token, settings.delete_token_secret)
    except TokenError:
        raise
    if payload.get("op") != expected_op:
        raise TokenError(
            f"Token was issued for op={payload.get('op')!r}, "
            f"but you called an op={expected_op!r} tool with it."
        )
    # Caller binding — see safety.assert_caller_matches. A token issued
    # by another MCP process running as a different user is rejected
    # here so a leaked token can't be replayed across identities.
    assert_caller_matches(payload.get("caller"), get_caller_identity())
    return payload


# ---------- preview rendering ----------------------------------------------


def _render_preview(
    op_label: str,
    plans: list[dict],
    *,
    kind: str,
    label_selector: str,
    namespace: str | None,
    extra_lines: list[str] | None = None,
    token: str | None = None,
    note: str | None = None,
) -> str:
    ns_part = f" in namespace {namespace!r}" if namespace else " cluster-wide"
    header = (
        f"{op_label} — {kind} matching label_selector={label_selector!r}{ns_part}\n"
        f"Matched {len(plans)} resource(s):"
    )
    body = short_table(plans, list(plans[0].keys())) if plans else "(no resources)"
    parts = [header, body]
    if extra_lines:
        parts.extend(extra_lines)
    if note:
        parts.append(note)
    if token:
        parts.append(
            f"\nconfirmation_token (HMAC-signed, "
            f"{get_settings().delete_token_ttl_seconds}s TTL):\n{token}\n\n"
            f"To execute, re-call with dry_run=False, confirm=True, and the "
            f"token above. The change will apply ONLY to the {len(plans)} "
            f"resource(s) listed — new resources matching the label_selector "
            f"in the meantime are NOT touched."
        )
    return "\n".join(parts)


# ---------- deprecation helper --------------------------------------------


_DEPRECATION_NOTE = (
    "⚠️ DEPRECATED: {tool} will be removed in v0.5.0 — pass a list of "
    "names to {single_tool} instead. For label_selector-based operations "
    "with the audited dry_run → confirm flow, keep using this tool until "
    "v0.5.0."
)


def _deprecate(tool: str, single_tool: str, body: str) -> str:
    """Prepend the deprecation note for a bulk_* tool to its return."""
    note = _DEPRECATION_NOTE.format(tool=tool, single_tool=single_tool)
    return f"{note}\n{body}"


# ---------- bulk_set_image -------------------------------------------------


def bulk_set_image(
    label_selector: str,
    container: str,
    image: str,
    kind: str = "Deployment",
    namespace: str | None = None,
    dry_run: bool = True,
    confirm: bool = False,
    confirmation_token: str | None = None,
) -> str:
    """⚠️ WRITE — set the `image` of a named container on every workload
    matching `label_selector`. `kubectl set image -l <sel> <kind>/<*> <c>=<img>`

    .. deprecated::
        Use :func:`set_image` with a list of names passed as `name`
        instead. This label_selector-based bulk tool will be removed
        in v0.5.0; the dry_run → confirm two-step flow is being
        consolidated into the audited two-step `delete_resource` /
        `replace_resource` family instead.

    Args:
        label_selector: e.g. `"app=nginx,tier=frontend"`. Required.
        container: the container name within each workload's pod template.
        image: the new image, e.g. `"nginx:1.25.3"`.
        kind: `Deployment` (default), `StatefulSet`, or `DaemonSet`.
        namespace: limit to one namespace; None = cluster-wide.
        dry_run: when True (default), list matches + show current→new diff
            per resource. NO write. No token issued.
        confirm: must be True to actually apply. See safety flow above.
        confirmation_token: required when `confirm=True`; obtained from a
            prior `confirm=False` call. Token's kind/selector/image/
            container/namespace must all match this call.

    Containers whose current image already matches `image` are listed but
    reported as "no change" — the patch is still issued, so the rollout
    may still occur (some teams rely on this; we don't suppress).
    """
    return _deprecate(
        "bulk_set_image", "set_image(name=[...])",
        _bulk_set_image_impl(
            label_selector, container, image, kind, namespace,
            dry_run, confirm, confirmation_token,
        ),
    )


def _bulk_set_image_impl(
    label_selector: str,
    container: str,
    image: str,
    kind: str = "Deployment",
    namespace: str | None = None,
    dry_run: bool = True,
    confirm: bool = False,
    confirmation_token: str | None = None,
) -> str:
    """Internal body of `bulk_set_image` — kept separate so the public
    function can prepend the deprecation marker without tangling the
    label_selector / dry_run / confirm flow."""
    if not label_selector:
        raise ValueError("label_selector is required for bulk operations")
    _write_guard(namespace)
    matched = _list_matched(kind, namespace, label_selector)
    plans = []
    for r in matched:
        cur = _container_image(r, container)
        plans.append({
            "NAMESPACE": r["metadata"].get("namespace", ""),
            "NAME": r["metadata"]["name"],
            "CONTAINER": container,
            "CURRENT": cur or "(container not found)",
            "TARGET": image,
        })

    if dry_run:
        return _render_preview(
            "bulk_set_image (DRY-RUN)", plans,
            kind=kind, label_selector=label_selector, namespace=namespace,
            note=(
                "Re-call with dry_run=False, confirm=False to get a "
                "confirmation_token; then dry_run=False, confirm=True + token "
                "to apply."
            ),
        )

    if not confirm:
        token = _issue_bulk_token({
            "op": "bulk_set_image",
            "kind": kind,
            "label_selector": label_selector,
            "namespace": namespace or "",
            "container": container,
            "image": image,
            "matched_names": [(p["NAMESPACE"], p["NAME"]) for p in plans],
        })
        return _render_preview(
            "bulk_set_image (PREVIEW)", plans,
            kind=kind, label_selector=label_selector, namespace=namespace,
            token=token,
        )

    payload = _verify_bulk_token(confirmation_token or "", expected_op="bulk_set_image")
    if payload.get("kind") != kind:
        raise TokenError(f"Token kind={payload.get('kind')!r} ≠ {kind!r}")
    if payload.get("label_selector") != label_selector:
        raise TokenError("Token label_selector does not match this call")
    if (payload.get("namespace") or "") != (namespace or ""):
        raise TokenError("Token namespace does not match this call")
    if payload.get("container") != container:
        raise TokenError(f"Token container={payload.get('container')!r} ≠ {container!r}")
    if payload.get("image") != image:
        raise TokenError(f"Token image={payload.get('image')!r} ≠ {image!r}")

    matched_set = {tuple(p) for p in payload.get("matched_names", [])}
    by_key = {(r["metadata"].get("namespace", ""), r["metadata"]["name"]): r
              for r in matched}
    return _execute_patches(
        "bulk_set_image", plans, matched_set, by_key,
        lambda r: _patch_image(r, container, image),
    )


# ---------- bulk_restart ---------------------------------------------------


def bulk_restart(
    label_selector: str,
    kind: str = "Deployment",
    namespace: str | None = None,
    dry_run: bool = True,
    confirm: bool = False,
    confirmation_token: str | None = None,
) -> str:
    """⚠️ WRITE — trigger a rolling restart on every workload matching
    `label_selector`. `kubectl rollout restart -l <sel> <kind>/<*>` equivalent.

    Implementation: stamp
    `spec.template.metadata.annotations[kubectl.kubernetes.io/restartedAt]`
    to the current UTC ISO timestamp. The controller treats the annotation
    change as a template change and rolls the Pods.

    .. deprecated::
        Use :func:`restart_workload` with a list of names passed as
        `name` instead. This label_selector-based bulk tool will be
        removed in v0.5.0.

    Args:
        label_selector: required.
        kind: Deployment / StatefulSet / DaemonSet.
        namespace: limit to one namespace; None = cluster-wide.
        dry_run / confirm / confirmation_token: same safety flow as
            `bulk_set_image`.
    """
    return _deprecate(
        "bulk_restart", "restart_workload(name=[...])",
        _bulk_restart_impl(
            label_selector, kind, namespace,
            dry_run, confirm, confirmation_token,
        ),
    )


def _bulk_restart_impl(
    label_selector: str,
    kind: str = "Deployment",
    namespace: str | None = None,
    dry_run: bool = True,
    confirm: bool = False,
    confirmation_token: str | None = None,
) -> str:
    """Internal body of `bulk_restart` — kept separate so the public
    function can prepend the deprecation marker."""
    if not label_selector:
        raise ValueError("label_selector is required for bulk operations")
    _write_guard(namespace)
    matched = _list_matched(kind, namespace, label_selector)
    plans = [{
        "NAMESPACE": r["metadata"].get("namespace", ""),
        "NAME": r["metadata"]["name"],
        "ACTION": "rolling restart (annotation stamp)",
    } for r in matched]

    if dry_run:
        return _render_preview(
            "bulk_restart (DRY-RUN)", plans,
            kind=kind, label_selector=label_selector, namespace=namespace,
            note=("Re-call with dry_run=False, confirm=False → token; "
                  "then confirm=True + token to apply."),
        )

    if not confirm:
        token = _issue_bulk_token({
            "op": "bulk_restart",
            "kind": kind,
            "label_selector": label_selector,
            "namespace": namespace or "",
            "matched_names": [(p["NAMESPACE"], p["NAME"]) for p in plans],
        })
        return _render_preview(
            "bulk_restart (PREVIEW)", plans,
            kind=kind, label_selector=label_selector, namespace=namespace,
            token=token,
        )

    payload = _verify_bulk_token(confirmation_token or "", expected_op="bulk_restart")
    if payload.get("kind") != kind:
        raise TokenError("Token kind mismatch")
    if payload.get("label_selector") != label_selector:
        raise TokenError("Token label_selector does not match this call")
    if (payload.get("namespace") or "") != (namespace or ""):
        raise TokenError("Token namespace does not match this call")

    matched_set = {tuple(p) for p in payload.get("matched_names", [])}
    by_key = {(r["metadata"].get("namespace", ""), r["metadata"]["name"]): r
              for r in matched}
    return _execute_patches(
        "bulk_restart", plans, matched_set, by_key,
        _patch_restart_annotation,
    )


# ---------- bulk_scale -----------------------------------------------------


_SCALE_KINDS = {"Deployment", "StatefulSet"}


def bulk_scale(
    label_selector: str,
    replicas: int,
    kind: str = "Deployment",
    namespace: str | None = None,
    dry_run: bool = True,
    confirm: bool = False,
    confirmation_token: str | None = None,
) -> str:
    """⚠️ WRITE — set `spec.replicas` on every workload matching
    `label_selector`. `kubectl scale -l <sel> --replicas=N` equivalent.

    .. deprecated::
        Use :func:`scale_workload` with a list of names passed as
        `name` instead. This label_selector-based bulk tool will be
        removed in v0.5.0.

    Args:
        label_selector: required.
        replicas: target replica count (int ≥ 0).
        kind: `Deployment` (default) or `StatefulSet`. DaemonSet is
            rejected because it has no replica concept.
        namespace: limit to one namespace; None = cluster-wide.
        dry_run / confirm / confirmation_token: same safety flow.
    """
    return _deprecate(
        "bulk_scale", "scale_workload(name=[...])",
        _bulk_scale_impl(
            label_selector, replicas, kind, namespace,
            dry_run, confirm, confirmation_token,
        ),
    )


def _bulk_scale_impl(
    label_selector: str,
    replicas: int,
    kind: str = "Deployment",
    namespace: str | None = None,
    dry_run: bool = True,
    confirm: bool = False,
    confirmation_token: str | None = None,
) -> str:
    """Internal body of `bulk_scale` — kept separate so the public
    function can prepend the deprecation marker without tangling the
    label_selector / dry_run / confirm flow."""
    if not label_selector:
        raise ValueError("label_selector is required for bulk operations")
    if kind not in _SCALE_KINDS:
        raise ValueError(
            f"bulk_scale only supports {sorted(_SCALE_KINDS)}; "
            f"{kind!r} has no replicas field. Did you mean bulk_restart?"
        )
    if replicas < 0:
        raise ValueError("replicas must be ≥ 0")
    _write_guard(namespace)
    matched = _list_matched(kind, namespace, label_selector)
    plans = []
    for r in matched:
        cur = (r.get("spec", {}) or {}).get("replicas")
        plans.append({
            "NAMESPACE": r["metadata"].get("namespace", ""),
            "NAME": r["metadata"]["name"],
            "CURRENT": str(cur) if cur is not None else "?",
            "TARGET": str(replicas),
        })

    if dry_run:
        return _render_preview(
            "bulk_scale (DRY-RUN)", plans,
            kind=kind, label_selector=label_selector, namespace=namespace,
            note=("Re-call with dry_run=False, confirm=False → token; "
                  "then confirm=True + token to apply."),
        )

    if not confirm:
        token = _issue_bulk_token({
            "op": "bulk_scale",
            "kind": kind,
            "label_selector": label_selector,
            "namespace": namespace or "",
            "replicas": int(replicas),
            "matched_names": [(p["NAMESPACE"], p["NAME"]) for p in plans],
        })
        return _render_preview(
            "bulk_scale (PREVIEW)", plans,
            kind=kind, label_selector=label_selector, namespace=namespace,
            token=token,
        )

    payload = _verify_bulk_token(confirmation_token or "", expected_op="bulk_scale")
    if payload.get("kind") != kind:
        raise TokenError("Token kind mismatch")
    if payload.get("label_selector") != label_selector:
        raise TokenError("Token label_selector does not match this call")
    if (payload.get("namespace") or "") != (namespace or ""):
        raise TokenError("Token namespace does not match this call")
    if int(payload.get("replicas", -1)) != int(replicas):
        raise TokenError(
            f"Token replicas={payload.get('replicas')} ≠ {replicas}"
        )

    matched_set = {tuple(p) for p in payload.get("matched_names", [])}
    by_key = {(r["metadata"].get("namespace", ""), r["metadata"]["name"]): r
              for r in matched}
    return _execute_patches(
        "bulk_scale", plans, matched_set, by_key,
        lambda r: _patch_replicas(r, replicas),
    )


# ---------- shared executor ------------------------------------------------


def _execute_patches(
    op_label: str,
    plans: list[dict],
    matched_set: set[tuple[str, str]],
    by_key: dict[tuple[str, str], dict],
    patcher,
) -> str:
    """Apply the patch ONLY to resources that were in the preview's
    matched_names set. Report per-resource results."""
    settings = get_settings()
    rows = []
    # Hoist the dynamic-client kind handle out of the loop. In practice
    # all matched resources for one bulk op share the same kind (the
    # bulk tool targets a single kind per call), so we resolve once.
    dc_resource_cache: dict[str, Any] = {}
    for ns, name in sorted(matched_set):
        resource = by_key.get((ns, name))
        if not resource:
            rows.append({
                "NAMESPACE": ns, "NAME": name,
                "RESULT": "SKIPPED (no longer matches selector at confirm time)",
            })
            continue

        api_version = resource.get("apiVersion")
        kind = resource.get("kind")
        if not settings.ns_allowed(ns):
            rows.append({
                "NAMESPACE": ns, "NAME": name,
                "RESULT": f"ERROR: namespace {ns!r} not allowed by K8S_MCP_NAMESPACE_ALLOWLIST",
            })
            continue

        try:
            new_manifest = patcher(resource)
            cached = dc_resource_cache.get(kind)
            if cached is None:
                cached = generic._dyn_client().resources.get(
                    api_version=api_version, kind=kind
                )
                dc_resource_cache[kind] = cached
            patch_kwargs: dict = {"body": new_manifest}
            if ns:
                patch_kwargs["namespace"] = ns
            cached.patch(**patch_kwargs)
            rows.append({
                "NAMESPACE": ns, "NAME": name,
                "RESULT": f"configured (patched) ({kind})",
            })
        except ApiException as e:
            rows.append({
                "NAMESPACE": ns, "NAME": name,
                "RESULT": f"ERROR: {e.reason} (status {e.status})",
            })
        except Exception as e:  # noqa: BLE001
            rows.append({
                "NAMESPACE": ns, "NAME": name,
                "RESULT": f"ERROR: {type(e).__name__}: {e}",
            })

    ok = sum(1 for r in rows if r["RESULT"].startswith(("created", "configured", "unchanged")))
    fail = sum(1 for r in rows if r["RESULT"].startswith(("ERROR", "SKIPPED")))
    header = f"{op_label} — applied to {ok}/{len(rows)} resources"
    if fail:
        header += f" ({fail} errors or skipped)"
    return header + "\n" + short_table(rows, ["NAMESPACE", "NAME", "RESULT"])


def register(mcp) -> None:
    mcp.tool()(bulk_set_image)
    mcp.tool()(bulk_restart)
    mcp.tool()(bulk_scale)
