"""NetworkPolicy shortcut — apply common ingress/egress rules.

For complex multi-port orcid logic prefer apply_yaml. This helper covers the
90% case: deny-all baseline + open specific ingress from a namespace.
"""
from __future__ import annotations

import logging
from typing import Any

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
    import yaml
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


def register(mcp) -> None:
    mcp.tool()(create_networkpolicy)
