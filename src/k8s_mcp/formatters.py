"""Output formatters: YAML / Table / Describe.

中文说明：
本模块集中所有"对象 → 字符串"的格式化逻辑，输出风格对 LLM 友好：

  - `to_yaml(obj)`：标准 YAML 序列化。
  - `short_table(rows, columns)`：紧凑文本表（无边框、列对齐）。
  - `describe(obj)`：仿 `kubectl describe` 风格的分组输出。
  - `mask_secret_data(obj)`：把 Secret.data / stringData 里的值替换为
    `***`，是 `get_resource_yaml` 默认行为的安全后盾。
"""
from __future__ import annotations

from typing import Any

import yaml

SECRET_MASK = "***"


def to_yaml(obj: Any) -> str:
    """Serialize any object to YAML. Datetime fields become ISO strings."""
    return yaml.safe_dump(obj, default_flow_style=False, sort_keys=False, allow_unicode=True)


def mask_secret_data(obj: dict) -> dict:
    """Return a copy of a Secret-like object with values masked.

    Only masks when kind == 'Secret' (defense in depth — caller should already
    gate on kind). Masks both `data` (base64) and `stringData` (plaintext) fields.
    Non-Secret kinds pass through untouched.
    """
    if not isinstance(obj, dict):
        return obj
    if obj.get("kind") != "Secret":
        return obj
    masked = dict(obj)
    if "data" in masked and isinstance(masked["data"], dict):
        masked["data"] = {k: SECRET_MASK for k in masked["data"]}
    if "stringData" in masked and isinstance(masked["stringData"], dict):
        masked["stringData"] = {k: SECRET_MASK for k in masked["stringData"]}
    return masked


def short_table(items: list[dict], columns: list[str]) -> str:
    """Render a compact table from a list of dicts."""
    if not items:
        return "(empty)"
    rows = []
    for item in items:
        rows.append({c: _display_value(item.get(c)) for c in columns})
    widths = {c: max(len(c), max(len(str(r.get(c, ""))) for r in rows)) for c in columns}
    header = "  ".join(c.ljust(widths[c]) for c in columns)
    sep = "  ".join("-" * widths[c] for c in columns)
    lines = [header, sep]
    for row in rows:
        lines.append("  ".join(str(row.get(c, "")).ljust(widths[c]) for c in columns))
    return "\n".join(lines)


def _display_value(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return yaml.safe_dump(v, default_flow_style=True).strip()
    return str(v)


def describe(obj: dict) -> str:
    """kubectl-describe-style text summary of a resource."""
    if not isinstance(obj, dict):
        return str(obj)
    md = obj.get("metadata") or {}
    spec = obj.get("spec") or {}
    status = obj.get("status") or {}

    name = md.get("name", "?")
    namespace = md.get("namespace", "")
    kind = obj.get("kind", "?")
    labels = md.get("labels") or {}
    annotations = md.get("annotations") or {}

    lines = [
        f"Name:       {name}",
    ]
    if namespace:
        lines.append(f"Namespace:  {namespace}")
    lines.append(f"Kind:       {kind}")
    if labels:
        lines.append(f"Labels:     {labels}")
    if annotations:
        lines.append(f"Annotations: {annotations}")

    created = md.get("creationTimestamp")
    if created:
        lines.append(f"Created:    {created}")

    # Spec highlights
    for k in ("replicas", "selector", "template", "ports", "type",
             "serviceAccount", "schedule", "suspend", "concurrencyPolicy",
             "serviceName", "volumes"):
        if k in spec:
            lines.append(f"Spec.{k}: {_compact(spec[k])}")

    # Status highlights
    for k in ("phase", "readyReplicas", "availableReplicas",
             "conditions", "loadBalancer", "active", "succeeded", "failed"):
        if k in status:
            lines.append(f"Status.{k}: {_compact(status[k])}")

    return "\n".join(lines)


def _compact(v: Any, max_len: int = 200) -> str:
    """Compact YAML dump, hard-truncated with an explicit marker when it
    would exceed max_len.

    A plain trailing '...' is invisible to an LLM reading the output: the
    model can mistake the ellipsis for a normal YAML value continuation
    (YAML uses '...' as a document-end marker too). The marker is the
    explicit signal that this single field was cut, so the model knows to
    re-fetch with a narrower scope rather than trust the partial value.
    """
    s = yaml.safe_dump(v, default_flow_style=True, sort_keys=False).strip()
    if len(s) <= max_len:
        return s
    marker = f" ...[TRUNCATED; full={len(s)}b]"
    budget = max_len - len(marker)
    head = s[:budget]
    # Trim to a YAML-flow-friendly break point (comma between items,
    # closing brace/bracket) when one exists in the back half — produces
    # a cleaner cut than slicing mid-token.
    for sep in (",", "}", "]", " "):
        cut = head.rfind(sep)
        if cut > budget // 2:
            head = head[: cut + 1]
            break
    return head + marker
