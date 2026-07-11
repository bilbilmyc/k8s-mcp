"""Secret tools: list secrets, decode a single value, create from data.

Why a narrow single-key tool instead of reusing get_resource_yaml with
reveal_secrets=True? Two reasons:
  1. **Narrow blast radius.** `reveal_secrets=True` exposes every key in
     the Secret; this tool only exposes the one you ask for.
  2. **Explicit confirmation.** `reveal=False` (the default) returns a
     `*** MASKED` marker and forces the agent to set `reveal=True` to
     see actual bytes.

Reads are always allowed (no read-only / allowlist gate). Writes use
`create_secret` (auto-base64) or `apply_yaml` (full replacement).

中文说明：
Secret 是 k8s-mcp 中最敏感的资源，专门做了三道防线：
  - `list_secrets`：只返回 metadata + data 的 key 个数，**绝不返回 value**。
  - `get_secret_value(reveal=False)`：返回 `*** MASKED (N bytes)`，
    Agent 看不到原文。
  - `get_secret_value(reveal=True)`：必须显式传 True 才会解码 value；
    非 UTF-8（二进制证书/密钥）会用 `<binary, N bytes, base64=...>`
    表示，便于重建。
  - `create_secret`：⚠️ 写入；自动 base64 编码，普通 dict 即可，
    避免手算 base64 的痛点。Secret 一经 apply 即对所有 mount 该
    Secret 的 Pod 立即生效——小心使用。
"""
from __future__ import annotations

import base64
import logging

import yaml
from kubernetes import dynamic
from kubernetes.client.rest import ApiException

from ..client import get_api_client
from ..config import get_settings
from ..formatters import format_age, short_table
from . import generic

logger = logging.getLogger(__name__)

SECRET_API_VERSION = "v1"


def _dyn():
    return dynamic.DynamicClient(get_api_client())


def _read_only_guard(action: str) -> None:
    if get_settings().read_only:
        raise PermissionError(
            f"Server is in read-only mode (K8S_MCP_READ_ONLY=true). "
            f"{action} is disabled."
        )


def _ensure_ns(namespace: str) -> None:
    if not get_settings().ns_allowed(namespace):
        raise PermissionError(
            f"Write to namespace '{namespace}' is not allowed by "
            "K8S_MCP_NAMESPACE_ALLOWLIST"
        )


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
    # grep `secret_reveal` to see what was exposed (which Secret / key /
    # namespace). The actual bytes are NOT logged. Logged at INFO so it
    # shows up in default-config stdout, not just DEBUG.
    logger.info("secret_reveal name=%s namespace=%s key=%s",
                name, namespace, key)

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


def create_secret(
    name: str,
    namespace: str,
    data: dict[str, str] | None = None,
    string_data: dict[str, str] | None = None,
    secret_type: str = "Opaque",
    labels: dict[str, str] | None = None,
) -> str:
    """⚠️ WRITE / ⚠️ EXPOSES SECRETS — create a Secret — pick THIS when you
    need a fresh Secret and want the tool to handle base64 encoding for
    you. Avoids the `kubectl create secret --from-literal=...` shell-escape
    dance or the manual base64 step on `apply_yaml`.

    Two input modes:

      - **`string_data=`** — plaintext values the apiserver will encode
        itself (recommended; clearer in audit logs, doesn't leak the base64
        representation in the manifest). Use for UTF-8 strings.
      - **`data=`** — already-base64 values; you must base64-encode before
        passing (or use `string_data`). Use when ingesting values from
        another base64 source or when the value isn't valid UTF-8.

    Pick exactly one of `data` / `string_data`. Empty values are rejected
    so a typo (e.g. `password="")` doesn't ship a Secret with empty bytes.

    Args:
        name: Secret name.
        namespace: target namespace.
        data: optional `{key: base64_value}` mapping.
        string_data: optional `{key: plaintext_value}` mapping.
        secret_type: defaults to "Opaque". Use `kubernetes.io/tls`,
            `kubernetes.io/dockerconfigjson`, `kubernetes.io/basic-auth`,
            etc. as needed.
        labels: optional labels applied to the Secret.

    Returns the apply result (kind/name: action).

    Raises:
        ValueError: neither / both of data / string_data set, or a value
            is empty.
        PermissionError: read-only mode or namespace allowlist denies write.
    """
    _read_only_guard("create_secret")
    _ensure_ns(namespace)

    if (data is None) == (string_data is None):
        raise ValueError(
            "Provide exactly one of `data` (base64 dict) or "
            "`string_data` (plaintext dict)"
        )

    if data is not None:
        if not data:
            raise ValueError("`data` must be a non-empty dict")
        encoded: dict[str, str] = {}
        for k, v in data.items():
            if v is None or v == "":
                raise ValueError(
                    f"empty value for key '{k}' — pass non-empty base64"
                )
            encoded[k] = v
    else:
        if not string_data:
            raise ValueError("`string_data` must be a non-empty dict")
        encoded = {}
        for k, v in string_data.items():
            if v is None or v == "":
                raise ValueError(
                    f"empty value for key '{k}' — refuse empty secrets"
                )
            encoded[k] = base64.b64encode(v.encode("utf-8")).decode("ascii")

    md: dict = {"name": name, "namespace": namespace}
    if labels:
        md["labels"] = labels

    manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": md,
        "type": secret_type,
        "data": encoded,
    }
    return generic.apply_yaml(yaml.safe_dump(manifest))


def register(mcp) -> None:
    mcp.tool()(list_secrets)
    mcp.tool()(get_secret_value)
    mcp.tool()(create_secret)
