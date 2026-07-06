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

中文说明：
Secret 是 k8s-mcp 中最敏感的资源，专门做了三道防线：

  - `list_secrets`：只返回 metadata + data 的 key 个数，**绝不返回 value**。
  - `get_secret_value(reveal=False)`：返回 `*** MASKED (N bytes)`，
    Agent 看不到原文。
  - `get_secret_value(reveal=True)`：必须显式传 True 才会解码 value；
    非 UTF-8（二进制证书/密钥）会用 `<binary, N bytes, base64=...>`
    表示，便于重建。

改 Secret 仍然走通用 `apply_yaml`，read_only 检查会自动套上。
"""
from __future__ import annotations

import base64
import logging

from kubernetes import dynamic
from kubernetes.client.rest import ApiException

from ..client import get_api_client, get_caller_identity
from ..formatters import format_age, short_table

logger = logging.getLogger(__name__)

SECRET_API_VERSION = "v1"


def _dyn():
    return dynamic.DynamicClient(get_api_client())


def list_secrets(
    namespace: str | None = None,
    label_selector: str | None = None,
) -> str:
    """List Secrets (metadata + key count only; values NEVER returned) — pick
    THIS when you want to discover which Secrets exist. Step 1 of the
    three-step Secret workflow.

    Three-step workflow to read a Secret value:
      1. THIS tool — find the Secret and confirm the key exists
      2. `get_secret_value(reveal=False)` — confirms size, value stays MASKED
      3. `get_secret_value(reveal=True)` — explicit reveal, returns bytes

    For structured YAML where you don't need values, use
    `get_resource_yaml(kind="Secret")` instead — it masks values by default
    and never requires reveal.

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
            "AGE": format_age(md.get("creationTimestamp")),
        })
    return short_table(rows, ["NAME", "NAMESPACE", "TYPE", "DATA", "AGE"])


def get_secret_value(
    name: str,
    namespace: str,
    key: str,
    reveal: bool = False,
) -> str:
    """⚠️ SENSITIVE — fetch one key from a Secret. Two-call pattern enforces
    explicit consent before bytes leave the cluster:

      - `reveal=False` (default): returns `*** MASKED (N bytes)`. The agent
        MUST make a second call with `reveal=True` to see actual bytes.
      - `reveal=True`: decodes and returns the value (UTF-8 text, or
        `<binary, N bytes, base64=...>` for non-printable bytes).

    Step 2/3 of the three-step workflow (after `list_secrets`). For bulk
    YAML view of a Secret without ever exposing values, use
    `get_resource_yaml(kind="Secret")` instead — it masks values by default
    and never requires reveal.

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
    # Audit log — every successful reveal leaves an INFO line so SOC can
    # grep `secret_reveal` to see who exposed what. The actual bytes are
    # NOT logged (only key + secret + namespace + caller). Logged at INFO
    # so it shows up in default-config stdout, not just DEBUG.
    caller = get_caller_identity()
    logger.info(
        "secret_reveal name=%s namespace=%s key=%s caller_user=%s caller_uid=%s",
        name, namespace, key,
        caller.get("username", "(unknown)"),
        caller.get("uid", ""),
    )

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

    return f"<binary, {len(raw)} bytes, base64={base64.b64encode(raw).decode('ascii')}>"


def register(mcp) -> None:
    mcp.tool()(list_secrets)
    mcp.tool()(get_secret_value)
