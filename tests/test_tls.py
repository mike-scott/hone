"""Unit tests for core/tls.py — hone-core's self-generated TLS material."""
import os

import pytest
from cryptography import x509
from cryptography.x509.oid import ExtensionOID

from core import tls


@pytest.fixture
def certs(tmp_path):
    cert_dir = str(tmp_path / "tls")
    paths = tls.ensure_certs(cert_dir, ["core.example", "127.0.0.1"])
    return cert_dir, paths


def test_generates_ca_and_server_cert(certs):
    cert_dir, (ca, srv, key) = certs
    for p in (ca, srv, key, os.path.join(cert_dir, "ca.key")):
        assert os.path.exists(p)


def test_idempotent(certs):
    cert_dir, _ = certs
    srv = os.path.join(cert_dir, "server.crt")
    before = open(srv, "rb").read()
    tls.ensure_certs(cert_dir, ["core.example"])      # a second call
    assert open(srv, "rb").read() == before           # regenerates nothing


def test_server_cert_chains_to_the_ca(certs):
    _, (ca, srv, _key) = certs
    server = x509.load_pem_x509_certificate(open(srv, "rb").read())
    cacert = x509.load_pem_x509_certificate(open(ca, "rb").read())
    assert server.issuer == cacert.subject


def test_server_cert_carries_the_san(certs):
    _, (_ca, srv, _key) = certs
    server = x509.load_pem_x509_certificate(open(srv, "rb").read())
    san = server.extensions.get_extension_for_oid(
        ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
    assert "core.example" in san.get_values_for_type(x509.DNSName)


def test_certs_have_key_identifiers(certs):
    """Strict TLS verification needs the server cert's Authority Key Id."""
    _, (_ca, srv, _key) = certs
    server = x509.load_pem_x509_certificate(open(srv, "rb").read())
    server.extensions.get_extension_for_oid(
        ExtensionOID.AUTHORITY_KEY_IDENTIFIER)
    server.extensions.get_extension_for_oid(
        ExtensionOID.SUBJECT_KEY_IDENTIFIER)


def test_ca_cert_pem(certs):
    cert_dir, _ = certs
    pem = tls.ca_cert_pem(cert_dir)
    assert pem.startswith("-----BEGIN CERTIFICATE-----")
