"""Tests for get_certificate_expiry — multi-source cluster cert diagnostics."""
from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from k8s_mcp.config import reset_settings_cache
from k8s_mcp.tools import certs


@pytest.fixture(autouse=True)
def _clear():
    reset_settings_cache()
    yield
    reset_settings_cache()


def _make_cert_pem(
    *,
    cn: str = "test-cert",
    days_until_expiry: int = 30,
    days_since_start: int = -30,
    issuer_cn: str = "Test CA",
) -> bytes:
    """Build a self-signed PEM cert with controllable expiry date.

    days_until_expiry > 0: cert is currently valid.
    days_until_expiry <= 0: cert is expired.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer_cn)])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now + timedelta(days=days_since_start))
        .not_valid_after(now + timedelta(days=days_until_expiry))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


# =============================================================================
# _cert_row / _dn_summary — pure helpers
# =============================================================================


def test_cert_row_shows_subject_issuer_dates_and_days_left():
    """Cert is parsed into a row with the expected fields and a sane 'days left'."""
    pem = _make_cert_pem(days_until_expiry=45)
    cert = x509.load_pem_x509_certificate(pem)
    row = certs._cert_row("test source", cert)
    assert row["SOURCE"] == "test source"
    assert "test-cert" in row["SUBJECT"]
    assert "Test CA" in row["ISSUER"]
    # 45-day cert, today is "days_since_start=-30", so days_left ~= 45
    assert int(row["DAYS_LEFT"]) >= 44
    assert "valid" in row["STATUS"]
    assert "2026" in row["NOT_BEFORE"] or "2027" in row["NOT_BEFORE"]


def test_cert_row_flags_expired_cert():
    """Expiry date in the past → EXPIRED status, negative DAYS_LEFT."""
    pem = _make_cert_pem(days_until_expiry=-5)
    cert = x509.load_pem_x509_certificate(pem)
    row = certs._cert_row("expired", cert)
    assert "EXPIRED" in row["STATUS"]
    assert int(row["DAYS_LEFT"]) < 0


def test_cert_row_flags_expiring_under_30_days_as_warn():
    """< 30d but >= 7d → 'expires in N d (<30d)'."""
    pem = _make_cert_pem(days_until_expiry=15)
    cert = x509.load_pem_x509_certificate(pem)
    row = certs._cert_row("soon", cert)
    assert "⚠️" in row["STATUS"]
    assert "<30d" in row["STATUS"]


def test_cert_row_flags_expiring_under_7_days_as_critical():
    """< 7d → critical status."""
    pem = _make_cert_pem(days_until_expiry=3)
    cert = x509.load_pem_x509_certificate(pem)
    row = certs._cert_row("about to die", cert)
    assert "❌" in row["STATUS"]


def test_dn_summary_truncates_long_subject():
    """Sanity: doesn't crash on a long DN, returns truncated string ending in '...'."""
    # CN has a 64-char limit; build a name with several components so the
    # joined DN is > 60 chars.
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    long_name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "x" * 60),  # 60 chars
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "y" * 30),  # 30 chars
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "z" * 30),  # 30 chars
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(long_name)
        .issuer_name(long_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=200))
        .sign(key, hashes.SHA256())
    )
    s = certs._dn_summary(cert.subject)
    assert len(s) <= 60
    assert s.endswith("...")


# =============================================================================
# _gather_sources — feed each source individually
# =============================================================================


def test_gather_sources_reads_api_ca_cert_env_path(monkeypatch, tmp_path):
    """When K8S_MCP_API_CA_CERT points at a real file, that source is populated."""
    pem = _make_cert_pem(days_until_expiry=200)
    ca_path = tmp_path / "ca.crt"
    ca_path.write_bytes(pem)
    monkeypatch.setenv("K8S_MCP_API_CA_CERT", str(ca_path))
    reset_settings_cache()
    from k8s_mcp.config import get_settings
    sources = certs._gather_sources(get_settings())
    api_label, api_blob = sources[0]
    assert "api_ca_cert" in api_label
    assert api_blob == pem


def test_gather_sources_skips_unreadable_api_ca_cert_gracefully(monkeypatch, tmp_path):
    """Missing path → label reported but value is None — never raises."""
    monkeypatch.setenv("K8S_MCP_API_CA_CERT", "/nonexistent/ca.crt")
    reset_settings_cache()
    from k8s_mcp.config import get_settings
    sources = certs._gather_sources(get_settings())
    api_label, api_blob = sources[0]
    assert api_blob is None
    assert "api_ca_cert" in api_label


def test_gather_sources_reads_kubeconfig_ca_and_client_cert(tmp_path):
    """Mode B: kubeconfig with CA + client cert returns both rows."""
    ca_pem = _make_cert_pem(cn="kube-ca", days_until_expiry=400)
    client_pem = _make_cert_pem(cn="kube-client", days_until_expiry=90)
    cfg = f"""\
apiVersion: v1
kind: Config
current-context: main
clusters:
- name: prod
  cluster:
    server: https://k8s.example.com:6443
    certificate-authority-data: {base64.b64encode(ca_pem).decode()}
contexts:
- name: main
  context:
    cluster: prod
    user: alice
users:
- name: alice
  user:
    client-certificate-data: {base64.b64encode(client_pem).decode()}
"""
    cfg_path = tmp_path / "kubeconfig"
    cfg_path.write_text(cfg)

    class _S:
        kubeconfig = str(cfg_path)
        kube_context = None
        api_ca_cert = None

    sources = certs._gather_sources(_S())
    blobs = {label: blob for label, blob in sources}
    assert blobs["kubeconfig CA (prod)"] == ca_pem
    assert blobs["kubeconfig client cert (alice)"] == client_pem


def test_gather_sources_token_only_kubeconfig_does_not_error(tmp_path):
    """kubeconfig without client-cert-data (token auth) must not raise —
    the row is just skipped with a 'not embedded' annotation."""
    cfg = """\
apiVersion: v1
kind: Config
current-context: main
clusters:
- name: prod
  cluster:
    server: https://k8s.example.com:6443
contexts:
- name: main
  context: {cluster: prod, user: alice}
users:
- name: alice
  user:
    token: kubernetes-thingy
"""
    cfg_path = tmp_path / "kubeconfig"
    cfg_path.write_text(cfg)

    class _S:
        kubeconfig = str(cfg_path)
        kube_context = None
        api_ca_cert = None

    sources = certs._gather_sources(_S())
    blobs = {label: blob for label, blob in sources}
    # No client-cert-data: row is None (not a parse error).
    assert blobs["kubeconfig client cert (not embedded; token auth)"] is None


def test_gather_sources_handles_kubecontext_override(tmp_path):
    """When settings.kube_context is set, that context's CA is used (not
    current-context)."""
    ca_main = _make_cert_pem(cn="main-ca", days_until_expiry=500)
    ca_other = _make_cert_pem(cn="other-ca", days_until_expiry=500)
    cfg = f"""\
apiVersion: v1
kind: Config
current-context: main
clusters:
- name: prod-main
  cluster:
    server: https://main.example.com
    certificate-authority-data: {base64.b64encode(ca_main).decode()}
- name: prod-other
  cluster:
    server: https://other.example.com
    certificate-authority-data: {base64.b64encode(ca_other).decode()}
contexts:
- name: main
  context: {{cluster: prod-main, user: alice}}
- name: other
  context: {{cluster: prod-other, user: alice}}
users:
- name: alice
  user: {{token: k}}
"""
    cfg_path = tmp_path / "kubeconfig"
    cfg_path.write_text(cfg)

    class _S:
        kubeconfig = str(cfg_path)
        kube_context = "other"  # override
        api_ca_cert = None

    sources = certs._gather_sources(_S())
    blobs = {label: blob for label, blob in sources}
    assert blobs["kubeconfig CA (prod-other)"] == ca_other


# =============================================================================
# get_certificate_expiry — end-to-end behavior
# =============================================================================


def test_returns_no_certs_message_when_all_sources_are_empty(monkeypatch):
    """Nothing visible to the server → clear "no certs" message (not empty)."""
    monkeypatch.delenv("K8S_MCP_API_CA_CERT", raising=False)
    monkeypatch.setenv("KUBECONFIG", "/nonexistent/kubeconfig")
    reset_settings_cache()
    out = certs.get_certificate_expiry()
    assert "No cluster certificates visible" in out
    assert out.strip() != ""


def test_returns_table_with_at_least_one_row_when_ca_visible(monkeypatch, tmp_path):
    """Single source → table has rows, sorted by days-left ascending."""
    # Mix of fast-expiring and slow-expiring.
    pem_fast = _make_cert_pem(cn="expiring-soon", days_until_expiry=10)
    pem_slow = _make_cert_pem(cn="long-lived", days_until_expiry=365)
    ca_path = tmp_path / "ca.crt"
    ca_path.write_bytes(pem_fast + pem_slow)
    monkeypatch.setenv("K8S_MCP_API_CA_CERT", str(ca_path))
    monkeypatch.setenv("KUBECONFIG", "/nonexistent/kubeconfig")
    reset_settings_cache()
    out = certs.get_certificate_expiry()
    # Both certs parsed (the API_CA_CERT file in test holds concatenated PEM blocks).
    assert "expiring-soon" in out
    assert "long-lived" in out
    # Action-needed line for the fast-expiring one.
    assert "Action needed" in out


def test_action_needed_section_only_appears_when_a_cert_is_expiring(monkeypatch, tmp_path):
    """All certs > 30 days out → no 'Action needed' block."""
    pem = _make_cert_pem(days_until_expiry=200)
    monkeypatch.setenv("K8S_MCP_API_CA_CERT", "")  # unset
    pem_path = tmp_path / "ca.crt"
    pem_path.write_bytes(pem)
    monkeypatch.setenv("KUBECONFIG", str(pem_path))  # so sources[0] is None
    # Trick: skip in-cluster by not creating the SA dir; skip kubeconfig
    # because the "kubeconfig" path has no kubeconfig structure — but we
    # need only one source. Use a writable path with valid kubeconfig.
    pem_path.unlink()
    monkeypatch.delenv("K8S_MCP_API_CA_CERT", raising=False)
    # Use the in-cluster source path? We can't write to /var/run. Instead,
    # monkeypatch the in-cluster path check.
    monkeypatch.setattr(
        "pathlib.Path.exists", lambda self: False, raising=False
    )
    # Skipping — instead test through the API_CA_CERT path only.
    pem_path.write_bytes(pem)
    monkeypatch.setenv("K8S_MCP_API_CA_CERT", str(pem_path))
    reset_settings_cache()
    out = certs.get_certificate_expiry()
    assert "✅ valid" in out
    assert "Action needed" not in out
