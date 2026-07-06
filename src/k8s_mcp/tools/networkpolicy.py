"""NetworkPolicy shortcut — apply common ingress/egress rules.

For complex multi-port orcid logic prefer apply_yaml. This helper covers the
90% case: deny-all baseline + open specific ingress from a namespace.

中文说明：
`create_networkpolicy(name, namespace, pod_selector, policy_types,
ingress=[], egress=[])`：

  - `policy_types` 至少要传一个，元素必须是 `Ingress` / `Egress`。
  - 不填 ingress/egress 时只创建空策略，效果是"什么都不允许"（deny-all）。
  - 复杂多端口 / CIDR 规则建议直接用 `apply_yaml`。
"""
from __future__ import annotations

import logging
from typing import Any

import yaml
from kubernetes import dynamic
from kubernetes.dynamic.exceptions import NotFoundError, ResourceNotFoundError

from ..client import get_api_client
from ..formatters import short_table
from . import generic

logger = logging.getLogger(__name__)


def create_networkpolicy(
    name: str,
    namespace: str,
    pod_selector: dict[str, str],
    policy_types: list[str],
    ingress: list[dict[str, Any]] | None = None,
    egress: list[dict[str, Any]] | None = None,
) -> str:
    """Create a NetworkPolicy.

    Pick THIS to *write* a policy. To *verify* an existing policy graph
    (which policies select a pod, what's the effective per-direction
    posture, where's the exposure surface), use the read-only
    `analyze_networkpolicy(namespace=..., pod=...)` instead.

    Args:
        name: policy name.
        namespace: target namespace.
        pod_selector: label selector, e.g. {"app": "db"}.
        policy_types: list of {"Ingress", "Egress"}; pass what you want
            to control. (If only Ingress is listed, Egress is unrestricted.)
        ingress: list of ingress rules, each:
            {
              "from": [{"podSelector": {...}} | {"namespaceSelector": {...}}],
              "ports": [{"protocol": "TCP", "port": 5432}],
            }
        egress: same shape, but applied to egress.

    By convention, listing a policy type without rules of that type means
    "deny all" for that direction.
    """
    if not policy_types:
        raise ValueError("policy_types must contain at least one of Ingress/Egress")
    for t in policy_types:
        if t not in ("Ingress", "Egress"):
            raise ValueError(f"Invalid policy_type: {t!r}; must be Ingress or Egress")

    spec: dict = {
        "podSelector": pod_selector,
        "policyTypes": policy_types,
    }
    if ingress is not None:
        spec["ingress"] = [_normalize_rule(r, "ingress") for r in ingress]
    if egress is not None:
        spec["egress"] = [_normalize_rule(r, "egress") for r in egress]

    manifest = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": name, "namespace": namespace},
        "spec": spec,
    }
    return generic.apply_yaml(yaml.safe_dump(manifest))


def _normalize_rule(rule: dict, direction: str) -> dict:
    out: dict = {}
    if "from" in rule:
        out["from"] = list(rule["from"])
    if "to" in rule:
        out["to"] = list(rule["to"])
    if "ports" in rule:
        out["ports"] = list(rule["ports"])
    if not out:
        raise ValueError(f"{direction} rule must have at least one of from/to/ports")
    return out


# ---------- analyze_networkpolicy -------------------------------------------


def _dyn_client() -> dynamic.DynamicClient:
    return dynamic.DynamicClient(get_api_client())


def _np_resource(dc: dynamic.DynamicClient):
    return dc.resources.get(api_version="networking.k8s.io/v1", kind="NetworkPolicy")


def _pod_resource(dc: dynamic.DynamicClient):
    return dc.resources.get(api_version="v1", kind="Pod")


def _expr_matches(expr: dict, labels: dict) -> bool:
    """Evaluate a single matchExpressions entry against pod labels."""
    key = expr.get("key")
    op = expr.get("operator")
    values = expr.get("values") or []
    present = key in labels
    if op == "In":
        return present and labels.get(key) in values
    if op == "NotIn":
        return (not present) or labels.get(key) not in values
    if op == "Exists":
        return present
    if op == "DoesNotExist":
        return not present
    return True  # unknown operator → don't exclude


def _selector_matches(selector: dict | None, labels: dict) -> bool:
    """K8s label-selector semantics. Empty / None selector matches all pods."""
    if not selector:
        return True
    for k, v in (selector.get("matchLabels") or {}).items():
        if labels.get(k) != v:
            return False
    return all(
        _expr_matches(e, labels) for e in (selector.get("matchExpressions") or [])
    )


def _policy_types(policy: dict) -> list[str]:
    """Return the policy's effective policyTypes.

    The apiserver normally sets policyTypes explicitly; when absent we
    infer per the K8s rule: Ingress is always implied, Egress only if the
    policy carries egress rules.
    """
    spec = policy.get("spec") or {}
    declared = spec.get("policyTypes")
    if declared:
        return list(declared)
    types = ["Ingress"]
    if spec.get("egress"):
        types.append("Egress")
    return types


def _fmt_selector(sel: dict | None) -> str:
    if not sel:
        return "{} (all pods)"
    parts = [f"{k}={v}" for k, v in (sel.get("matchLabels") or {}).items()]
    if sel.get("matchExpressions"):
        parts.append("+expr")
    return ", ".join(parts) if parts else "{} (all pods)"


def _rule_peers(rule: dict, direction: str) -> tuple[list[str], list[str]]:
    """Summarize one ingress/egress rule → (peer strings, port strings)."""
    peers: list[str] = []
    for peer in (rule.get(direction) or []):
        if "podSelector" in peer:
            peers.append(f"podSelector {_fmt_selector(peer['podSelector'])}")
        if "namespaceSelector" in peer:
            peers.append(f"nsSelector {_fmt_selector(peer['namespaceSelector'])}")
        if "ipBlock" in peer:
            peers.append(f"ipBlock {peer['ipBlock'].get('cidr', '?')}")
    if not peers and not (rule.get(direction)):
        peers.append("anywhere (no peer restriction)")
    ports: list[str] = []
    for p in (rule.get("ports") or []):
        ports.append(f"{p.get('protocol', 'TCP')}/{p.get('port', '*')}")
    return peers, ports


def analyze_networkpolicy(namespace: str, pod: str | None = None) -> str:
    """🔍 NETWORKPOLICY analyzer — read-only connectivity / coverage inspector.

    Use this to *inspect* the NetworkPolicy graph. To *write* a new
    policy, use `create_networkpolicy` (not this). Two modes:

    - **`pod=` view** → which policies select this pod (by podSelector,
      evaluating both matchLabels and matchExpressions), the merged
      ingress/egress rules, and the effective posture per direction:
      `🔒 default-deny (isolated)` once *any* selecting policy lists that
      policyType, else `🔓 default-allow`.
    - **`namespace=` only** → coverage sweep: every pod's ingress/egress
      posture, highlighting the exposure surface (pods no policy selects,
      i.e. default-allow), plus a policy inventory.

    Note: this reports the *declared* policy graph. Whether it's actually
    enforced depends on the CNI plugin (Calico / Cilium / …); a cluster
    with a non-enforcing CNI will show policies here that do nothing.

    Args:
        namespace: namespace to analyze (NetworkPolicy is namespaced).
        pod: optional pod name for the per-pod connectivity view.

    Returns a multi-section report. Always read-only.
    """
    dc = _dyn_client()
    try:
        np_res = _np_resource(dc)
        pod_res = _pod_resource(dc)
    except ResourceNotFoundError as e:
        raise RuntimeError(
            "networking.k8s.io/v1 NetworkPolicy not available on this cluster"
        ) from e

    policies = list(np_res.get(namespace=namespace).items)

    if pod is not None:
        return _render_pod_view(namespace, pod, policies, pod_res)
    return _render_coverage_view(namespace, policies, pod_res)


def _pod_labels(pod_obj) -> dict:
    return (pod_obj.get("metadata") or {}).get("labels") or {}


def _selecting_policies(policies: list[dict], labels: dict) -> list[dict]:
    return [
        p for p in policies
        if _selector_matches((p.get("spec") or {}).get("podSelector"), labels)
    ]


def _render_pod_view(
    namespace: str, pod: str, policies: list[dict], pod_res,
) -> str:
    try:
        pod_obj = pod_res.get(namespace=namespace, name=pod)
    except NotFoundError as e:
        raise ValueError(f"pod {namespace}/{pod} not found") from e

    labels = _pod_labels(pod_obj)
    lines = [
        f"## NetworkPolicy analysis: pod {namespace}/{pod}",
        f"Labels: {', '.join(f'{k}={v}' for k, v in labels.items()) or '(none)'}",
    ]

    selecting = _selecting_policies(policies, labels)
    if not selecting:
        lines.append("\nSelected by 0 policies.")
        lines.append("\n## Effective posture")
        lines.append("Ingress: 🔓 default-allow (no policy selects this pod)")
        lines.append("Egress:  🔓 default-allow (no policy selects this pod)")
        return "\n".join(lines)

    lines.append(f"\nSelected by {len(selecting)} policy(ies):")
    for p in selecting:
        pmeta = p.get("metadata") or {}
        spec = p.get("spec") or {}
        types = _policy_types(p)
        lines.append(f"### {pmeta.get('name', '?')} {types}")
        lines.append(f"  podSelector: {_fmt_selector(spec.get('podSelector'))}")
        for direction, key in (("ingress", "from"), ("egress", "to")):
            if direction not in [t.lower() for t in types]:
                continue
            rules = spec.get(direction)
            if not rules:
                lines.append(f"  {direction}: (none — deny all {direction})")
                continue
            lines.append(f"  {direction}:")
            for r in rules:
                peers, ports = _rule_peers(r, key)
                port_str = f"; ports: {', '.join(ports)}" if ports else "; ports: all"
                lines.append(f"    - {'; '.join(peers)}{port_str}")

    ingress_isolated = any("Ingress" in _policy_types(p) for p in selecting)
    egress_isolated = any("Egress" in _policy_types(p) for p in selecting)
    lines.append("\n## Effective posture")
    lines.append(
        "Ingress: 🔒 default-deny (isolated) — only rules above allowed"
        if ingress_isolated
        else "Ingress: 🔓 default-allow (no ingress policy selects this pod)"
    )
    lines.append(
        "Egress:  🔒 default-deny (isolated) — only rules above allowed"
        if egress_isolated
        else "Egress:  🔓 default-allow (no egress policy selects this pod)"
    )
    return "\n".join(lines)


def _render_coverage_view(
    namespace: str, policies: list[dict], pod_res,
) -> str:
    pods = list(pod_res.get(namespace=namespace).items)
    lines = [
        f"## NetworkPolicy coverage: namespace '{namespace}'",
        f"Pods: {len(pods)} | NetworkPolicies: {len(policies)}",
    ]

    if not policies:
        lines.append(
            "\n⚠️ No NetworkPolicy in this namespace — every pod is "
            "default-allow (fully open ingress + egress)."
        )
        return "\n".join(lines)

    rows = []
    open_ingress = 0
    for pod_obj in pods:
        pmeta = pod_obj.get("metadata") or {}
        labels = _pod_labels(pod_obj)
        selecting = _selecting_policies(policies, labels)
        ing = any("Ingress" in _policy_types(p) for p in selecting)
        eg = any("Egress" in _policy_types(p) for p in selecting)
        if not ing:
            open_ingress += 1
        rows.append({
            "POD": pmeta.get("name", "?"),
            "INGRESS": "🔒 covered" if ing else "🔓 open",
            "EGRESS": "🔒 covered" if eg else "🔓 open",
        })

    lines.append(
        f"\n### Pod posture ({open_ingress} pod(s) with open ingress = exposure surface)"
    )
    if rows:
        lines.append(short_table(rows, ["POD", "INGRESS", "EGRESS"]))
    else:
        lines.append("(no pods in namespace)")

    lines.append("\n### Policies")
    prows = []
    for p in policies:
        pmeta = p.get("metadata") or {}
        spec = p.get("spec") or {}
        prows.append({
            "NAME": pmeta.get("name", "?"),
            "PODSELECTOR": _fmt_selector(spec.get("podSelector")),
            "TYPES": ",".join(_policy_types(p)),
            "INGRESS": (
                "deny-all" if spec.get("ingress") == [] or (
                    "Ingress" in _policy_types(p) and not spec.get("ingress")
                ) else str(len(spec.get("ingress") or []))
            ),
            "EGRESS": (
                "deny-all" if (
                    "Egress" in _policy_types(p) and not spec.get("egress")
                ) else str(len(spec.get("egress") or []))
            ),
        })
    lines.append(short_table(prows, ["NAME", "PODSELECTOR", "TYPES", "INGRESS", "EGRESS"]))
    return "\n".join(lines)


def register(mcp) -> None:
    mcp.tool()(create_networkpolicy)
    mcp.tool()(analyze_networkpolicy)
