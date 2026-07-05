"""Certificate-expiry diagnostics: read CA + client certs from all visible sources.

The Kubernetes apiserver's *serving certificate* expiration can't be queried
through the K8s API — the apiserver won't reveal its own cert. What an MCP
client CAN see is:

  1. **K8S_MCP_API_CA_CERT** (mode A: apiserver URL+token) — the CA bundle
     the MCP server uses to verify the apiserver.
  2. **In-cluster CA bundle** at
     `/var/run/secrets/kubernetes.io/serviceaccount/ca.crt` (mode C).
  3. **kubeconfig CA bundle** (mode B) — base64-decoded from
     `clusters[].cluster.certificate-authority-data`.
  4. **kubeconfig client certificate** (mode B, optional) — base64-decoded
     from `users[].user.client-certificate-data`. Many clusters use token
     auth (no client cert), so this row appears only when the user's
     kubeconfig embeds a cert/key.

Reading those in one shot gives an LLM agent the data it needs to answer
"is my cluster about to fall over from cert expiry?" without an interactive
chat — and it points the user at what's actually about to expire (CA vs.
client cert) so the answer isn't a vague "your cluster expires soon".

中文说明：
本工具聚合所有 MCP server 看得见的证书源，输出每个证书的 Subject /
Issuer / NotBefore / NotAfter / 剩余天数 / 状态，快过期 / 已过期会
直接高亮。

注意：K8s apiserver 自己的 serving cert **不能** 通过 K8s API 查
（apiserver 不会透露自己的证书）。能查的是 MCP server 自己用来验证
apiserver 的那一份 CA / client cert。
"""
from __future__ import annotations

import base64
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

import yaml
from cryptography import x509
from cryptography.hazmat.backends import default_backend

from ..config import Settings, get_settings
from ..formatters import short_table

logger = logging.getLogger(__name__)


# Tunables. <30 days = "expires soon"; <0 = "EXPIRED".
_EXPIRY_WARN_DAYS = 30
_EXPIRY_CRIT_DAYS = 7


# =============================================================================
# Public tool
# =============================================================================


def get_certificate_expiry() -> str:
    """Report expiration time of every cluster certificate the MCP server can see.

    Reads certs from up to four sources (whichever exist):
      1. **K8S_MCP_API_CA_CERT** — mode A (apiserver URL+token) CA path
      2. **In-cluster SA bundle** — mode C, at
         `/var/run/secrets/kubernetes.io/serviceaccount/ca.crt`
      3. **kubeconfig CA bundle** — mode B, parsed from
         `clusters[].cluster.certificate-authority-data` in the current
         context
      4. **kubeconfig client cert** — mode B, parsed from
         `users[].user.client-certificate-data` (when present; many
         kubeconfigs use token auth and skip this)

    Each cert is reported with Subject / Issuer / NotBefore / NotAfter /
    Days Left / Status (✅ valid / ⚠️ expires soon / ❌ EXPIRED).

    Returns:
        A formatted table. Empty list when no certs are visible — then a
        hint about why (in-cluster auth might just be a token, no
        kubeconfig, etc.).
    """
    settings = get_settings()
    sources = _gather_sources(settings)
    rows: list[dict[str, str]] = []
    notes: list[str] = []

    for label, blob in sources:
        if blob is None:
            continue
        certs_in_blob = _all_certs_from_pem(blob)
        if not certs_in_blob:
            notes.append(f"{label}: could not parse X509 PEM")
            continue
        # When a bundle has multiple certs, label each with index so the
        # user can tell root from intermediate.
        for i, cert in enumerate(certs_in_blob):
            row_label = (
                f"{label} [{i + 1}/{len(certs_in_blob)}]"
                if len(certs_in_blob) > 1
                else label
            )
            rows.append(_cert_row(row_label, cert))

    if not rows:
        explanation = (
            "No cluster certificates visible to this MCP server.\n"
            "Possible reasons:\n"
            "  - Mode A (apiserver URL+token) without K8S_MCP_API_CA_CERT set.\n"
            "  - Mode C (in-cluster) without /var/run/secrets/kubernetes.io/\n"
            "    serviceaccount/ca.crt (token-projection only, no CA).\n"
            "  - Mode B (kubeconfig) without 'certificate-authority-data'\n"
            "    or 'client-certificate-data' on the current context.\n"
            "  - The kubeconfig uses token auth only — no embedded client cert.\n"
        )
        if notes:
            explanation += "\nParse errors:\n" + "\n".join(f"  - {n}" for n in notes)
        return explanation

    # Sort by days-left ascending so the soonest-to-expire surfaces first.
    rows.sort(key=lambda r: int(r["DAYS_LEFT"]) if r["DAYS_LEFT"].lstrip("-").isdigit() else 99999)
    out = short_table(
        rows,
        ["SOURCE", "SUBJECT", "ISSUER", "NOT_BEFORE", "NOT_AFTER", "DAYS_LEFT", "STATUS"],
    )

    # Highlight bad rows in a single line so the agent surfaces them.
    bad = [r for r in rows if r["STATUS"] != "✅ valid"]
    if bad:
        out += "\nAction needed:\n"
        for r in bad:
            out += f"  - {r['SOURCE']}: {r['STATUS']} (expires {r['NOT_AFTER']}, {r['DAYS_LEFT']}d)\n"

    if notes:
        out += "\nParse notes:\n" + "\n".join(f"  - {n}" for n in notes) + "\n"

    return out


# =============================================================================
# Source gathering
# =============================================================================


def _gather_sources(settings: Settings) -> list[tuple[str, bytes | None]]:
    """Return [(label, PEM bytes or None)] for every cert source we know about.

    Order:
      1. K8S_MCP_API_CA_CERT (mode A)
      2. in-cluster CA bundle (mode C)
      3. kubeconfig CA bundle (mode B)
      4. kubeconfig client cert (mode B, optional)

    The order matters only for fallback — the table output sorts by
    days-left at the end so the soonest-to-expire surfaces first.
    """
    out: list[tuple[str, bytes | None]] = []

    # 1. K8S_MCP_API_CA_CERT (mode A)
    if settings.api_ca_cert:
        path = Path(settings.api_ca_cert).expanduser()
        out.append(("api_ca_cert (K8S_MCP_API_CA_CERT)", _safe_read(path)))
    else:
        out.append(("api_ca_cert (K8S_MCP_API_CA_CERT)", None))

    # 2. In-cluster CA bundle (mode C)
    in_cluster_ca = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    out.append(("in-cluster SA bundle", _safe_read(in_cluster_ca) if in_cluster_ca.exists() else None))

    # 3 + 4. kubeconfig (mode B)
    kubeconfig_path = _kubeconfig_path(settings)
    if kubeconfig_path and kubeconfig_path.exists():
        ctx_name = settings.kube_context  # may be None → load_kube_config default
        try:
            data = yaml.safe_load(kubeconfig_path.read_text())
        except Exception as e:  # noqa: BLE001 — defensive
            out.append((f"kubeconfig ({kubeconfig_path})", None))
            logger.debug("kubeconfig parse failed: %s", e)
            return out

        ctx = _current_context(data, ctx_name)
        if ctx:
            cluster = _cluster_for(data, ctx)
            user = _user_for(data, ctx)
            ca_data, client_data = None, None
            if cluster:
                # kubeconfig nests the actual cluster creds under "cluster: {...}"
                cluster_inner = cluster.get("cluster") or {}
                ca_data = cluster_inner.get("certificate-authority-data")
            if user:
                # Same nesting for users: "user: {...}".
                user_inner = user.get("user") or {}
                client_data = user_inner.get("client-certificate-data")

            if ca_data:
                out.append((
                    f"kubeconfig CA ({_short(cluster.get('name', '?'))})",
                    base64.b64decode(ca_data),
                ))
            else:
                out.append(("kubeconfig CA (no certificate-authority-data)", None))
            if client_data:
                out.append((
                    f"kubeconfig client cert ({_short(user.get('name', '?'))})",
                    base64.b64decode(client_data),
                ))
            else:
                # Token-auth kubeconfigs skip the client cert entirely —
                # don't surface as an error.
                out.append(("kubeconfig client cert (not embedded; token auth)", None))
        else:
            out.append((f"kubeconfig ({kubeconfig_path})", None))
    else:
        out.append((f"kubeconfig ({kubeconfig_path or 'no path'})", None))

    return out


def _safe_read(path: Path) -> bytes | None:
    """Read bytes from path; log and return None on any failure (don't crash)."""
    try:
        return path.read_bytes()
    except Exception as e:  # noqa: BLE001 — defensive
        logger.debug("cert read failed for %s: %s", path, e)
        return None


def _all_certs_from_pem(blob: bytes) -> list[x509.Certificate]:
    """Parse a possibly-multi-PEM byte blob and return every cert in it.

    A CA bundle often has the root + intermediates concatenated; we want
    all of them so days_left / status can highlight the soonest-to-expire.
    """
    if not blob:
        return []
    try:
        # cryptography ≥42 prefers the plural loader; it returns a list
        # and accepts both single and multi-PEM blobs.
        return list(x509.load_pem_x509_certificates(blob))
    except (ValueError, TypeError):
        # Older releases or single-cert blobs — fall back to the singular
        # API which reads only the first cert.
        try:
            return [x509.load_pem_x509_certificate(blob, default_backend())]
        except Exception:
            return []
    except Exception:  # noqa: BLE001 — defensive
        return []


# Keep the old single-cert loader accessible for callers that only expect one.
def _one_cert_from_pem(blob: bytes) -> x509.Certificate | None:
    out = _all_certs_from_pem(blob)
    return out[0] if out else None


def _kubeconfig_path(settings: Settings) -> Path | None:
    """Resolve the kubeconfig file the MCP server is using, if mode B."""
    if settings.kubeconfig:
        return Path(settings.kubeconfig).expanduser()
    env = os.environ.get("KUBECONFIG")
    if env:
        # Like kubectl: take the first colon-separated entry.
        return Path(env.split(os.pathsep)[0]).expanduser()
    return Path.home() / ".kube" / "config"


def _current_context(data: dict, ctx_name: str | None) -> dict | None:
    """Pick the kubeconfig context dict — by name if given, else current-context."""
    if not isinstance(data, dict):
        return None
    ctxs = data.get("contexts") or []
    current_name = ctx_name or data.get("current-context")
    if not current_name:
        return None
    for c in ctxs:
        if isinstance(c, dict) and c.get("name") == current_name:
            return c.get("context") or {}
    return None


def _cluster_for(data: dict, ctx: dict) -> dict | None:
    cluster_name = ctx.get("cluster")
    if not cluster_name:
        return None
    for c in data.get("clusters") or []:
        if isinstance(c, dict) and c.get("name") == cluster_name:
            return c
    return None


def _user_for(data: dict, ctx: dict) -> dict | None:
    user_name = ctx.get("user")
    if not user_name:
        return None
    for u in data.get("users") or []:
        if isinstance(u, dict) and u.get("name") == user_name:
            return u
    return None


# =============================================================================
# Cert → row
# =============================================================================


def _cert_row(label: str, cert: x509.Certificate) -> dict[str, str]:
    """Build a table row from a parsed X509 certificate."""
    now = datetime.now(UTC)
    not_after = cert.not_valid_after_utc
    days_left = (not_after - now).days

    if now > not_after:
        status = "❌ EXPIRED"
    elif days_left < _EXPIRY_CRIT_DAYS:
        status = f"❌ EXPIRES IN {days_left}d (<7d)"
    elif days_left < _EXPIRY_WARN_DAYS:
        status = f"⚠️ expires in {days_left}d (<30d)"
    else:
        status = "✅ valid"

    return {
        "SOURCE": label,
        "SUBJECT": _dn_summary(cert.subject),
        "ISSUER": _dn_summary(cert.issuer),
        "NOT_BEFORE": cert.not_valid_before_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "NOT_AFTER": not_after.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "DAYS_LEFT": str(days_left),
        "STATUS": status,
    }


def _dn_summary(rdns: x509.Name) -> str:
    """Compact human-readable DN: 'CN=foo, O=bar'.

    Skips empty / garbage components. Truncates to 60 chars.
    """
    try:
        parts = [f"{a.oid._name}={a.value}" for a in rdns if a.value]
    except Exception:
        parts = []
    s = ", ".join(parts)
    return s if len(s) <= 60 else s[:57] + "..."


def _short(text: str, n: int = 12) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


# =============================================================================
# Registration
# =============================================================================


def register(mcp) -> None:
    """Register all certs tools with the FastMCP instance."""
    mcp.tool()(get_certificate_expiry)
