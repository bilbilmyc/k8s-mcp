"""Secret tools: list secrets, decode a single value.

Why a narrow single-key tool instead of reusing get_resource_yaml with
reveal_secrets=True? Two reasons:
  1. **Narrow blast radius.** `reveal_secrets=True` exposes every key in
     the Secret; this tool only exposes the one you ask for.
  2. **Explicit confirmation.** `reveal=False` (the default) returns a
     `*** MASKED` marker and forces the agent to set `reveal=True` to
     see actual bytes.

Reads are always allowed (no read-only / allowlist gate). Writes use
`update_secret` or `apply_yaml` for full replacement.
"""
from __future__ import annotations

import base64
import logging

from kubernetes import dynamic
from kubernetes.client.rest import ApiException

from ..client import get_api_client
from ..formatters import short_table

logger = logging.getLogger(__name__)

SECRET_API_VERSION = "v1"


def _dyn():
    return dynamic.DynamicClient(get_api_client())


def list_secrets(
    namespace: str | None = None,
    label_selector: str | None = None,
) -> str:
    """List Secrets (metadata only; never returns values).

    Args:
        namespace: namespace to list in. None = all namespaces.
        label_selector: e.g. "app=db".

    Returns a compact NAME / NAMESPACE / TYPE / AGE table. The `data`
    field is never returned here — use `get_secret_value` for that.
    """
    dc = _dyn()
    try:
        resource = dc.resources.get(api_version=SECRET_API_VERSION, kind="Secret")
    except Exception as e:
        raise ValueError("Unknown kind: Secret") from e

    kwargs: dict = {}
    if label_selector:
        kwargs["label_selector"] = label_selector

    if namespace:
        ret = resource.get(namespace=namespace, **kwargs)
    else:
        ret = resource.get(**kwargs)

    rows = []
    for item in ret.items:
        obj = item.to_dict() if hasattr(item, "to_dict") else dict(item)
        md = obj.get("metadata") or {}
        rows.append({
            "NAME": md.get("name"),
            "NAMESPACE": md.get("namespace", ""),
            "TYPE": obj.get("type", ""),
            "DATA": len(obj.get("data") or {}),
            "AGE": _age(md.get("creationTimestamp")),
        })
    return short_table(rows, ["NAME", "NAMESPACE", "TYPE", "DATA", "AGE"])


def get_secret_value(
    name: str,
    namespace: str,
    key: str,
    reveal: bool = False,
) -> str:
    """Fetch one key from a Secret and return its decoded value.

    Args:
        name: Secret name.
        namespace: Secret namespace.
        key: the data key (e.g. "password", "tls.crt").
        reveal: must be True to actually return the secret bytes. When
            False (the default) the function returns a `*** MASKED`
            marker, so the LLM cannot leak secrets by accident.

    Behavior:
      - Secret.data is base64-encoded per K8s convention; this tool
        decodes it for you.
      - Secret.stringData is already plaintext; this tool returns it as-is.
      - If `reveal=True` and the value is non-printable (binary cert /
        key), the function prints the base64 encoding plus the byte length
        so you can still pipe it to base64 -d downstream.

    Raises LookupError if Secret or key is not found.
    """
    dc = _dyn()
    try:
        resource = dc.resources.get(api_version=SECRET_API_VERSION, kind="Secret")
    except Exception as e:
        raise ValueError("Unknown kind: Secret") from e

    try:
        item = resource.get(name=name, namespace=namespace)
    except ApiException as e:
        if e.status == 404:
            raise LookupError(f"Secret '{namespace}/{name}' not found") from e
        raise

    obj = item.to_dict() if hasattr(item, "to_dict") else dict(item)

    if not reveal:
        # Surface key existence without leaking value, so the LLM at least
        # knows whether the key is present.
        data = obj.get("data") or {}
        string_data = obj.get("stringData") or {}
        if key in data:
            padded = data[key] + "=" * (-len(data[key]) % 4)
            approx_len = len(base64.b64decode(padded))
        elif key in string_data:
            approx_len = len(string_data[key])
        else:
            available = sorted(list(data.keys()) + list(string_data.keys()))
            raise LookupError(
                f"Key '{key}' not found in Secret '{namespace}/{name}'. "
                f"Available keys: {available}"
            )
        return f"Secret '{namespace}/{name}' key '{key}': value MASKED ({approx_len} bytes); set reveal=True to expose actual bytes"

    # reveal=True path
    data = obj.get("data") or {}
    string_data = obj.get("stringData") or {}

    if key in data:
        raw_b64 = data[key]
        try:
            padded = raw_b64 + "=" * (-len(raw_b64) % 4)
            value = base64.b64decode(padded)
        except Exception as e:
            raise ValueError(f"Could not base64-decode value for key '{key}': {e}") from e
        return _format_decoded(key, value)
    if key in string_data:
        return string_data[key]

    available = sorted(list(data.keys()) + list(string_data.keys()))
    raise LookupError(
        f"Key '{key}' not found in Secret '{namespace}/{name}'. "
        f"Available keys: {available}"
    )


# ---------- internals ----------------------------------------------------------


def _format_decoded(key: str, raw: bytes) -> str:
    """Return a printable representation of a decoded secret value.

    Tries UTF-8 first; if the result has non-printable characters, falls
    back to "<binary, N bytes, base64=...>" so the caller can still
    reconstruct it.
    """
    try:
        text = raw.decode("utf-8")
        if text.isprintable() or key.endswith(".txt"):
            return text
    except UnicodeDecodeError:
        pass

    import base64 as _b64
    return f"<binary, {len(raw)} bytes, base64={_b64.b64encode(raw).decode('ascii')}>"


def _age(created: str | None) -> str:
    if not created:
        return ""
    from datetime import UTC, datetime
    try:
        ts = datetime.fromisoformat(created.replace("Z", "+00:00"))
        delta = datetime.now(UTC) - ts
    except (ValueError, TypeError):
        return created
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def register(mcp) -> None:
    mcp.tool()(list_secrets)
    mcp.tool()(get_secret_value)
