"""hone-core — TLS material.

On first startup hone-core generates its own certificate authority and a
server certificate signed by it (ARCHITECTURE.md -> Auth, enrollment &
transport). The CA is handed to each node during enrollment; the node then
validates every non-OAuth call against it. The material lives on the data
volume and is reused on every later start — hone-core serves HTTPS directly,
with no external TLS-terminating proxy.
"""
import datetime
import ipaddress
import os

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

_CA_CERT, _CA_KEY = "ca.crt", "ca.key"
_SRV_CERT, _SRV_KEY = "server.crt", "server.key"


def _rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _san(hostnames):
    """A SubjectAlternativeName covering each name (IP literal or DNS name)."""
    out = []
    for h in hostnames:
        try:
            out.append(x509.IPAddress(ipaddress.ip_address(h)))
        except ValueError:
            out.append(x509.DNSName(h))
    return x509.SubjectAlternativeName(out)


def _write(path, data, mode):
    with open(path, "wb") as f:
        f.write(data)
    os.chmod(path, mode)


def _key_pem(key):
    return key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def ensure_certs(cert_dir, hostnames):
    """Ensure the CA and server certificate exist under `cert_dir`, generating
       them once on first call (a no-op once present). `hostnames` are the DNS
       names / IPs the server certificate is valid for — the first is also its
       common name. Returns (ca_cert_path, server_cert_path, server_key_path).
       The private keys are written 0600; the certificates 0644."""
    paths = {n: os.path.join(cert_dir, n)
             for n in (_CA_CERT, _CA_KEY, _SRV_CERT, _SRV_KEY)}
    result = (paths[_CA_CERT], paths[_SRV_CERT], paths[_SRV_KEY])
    if all(os.path.exists(p) for p in paths.values()):
        return result

    os.makedirs(cert_dir, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc)
    hostnames = list(hostnames) or ["localhost"]

    # --- the CA: a self-signed root, the trust anchor a node is given ------
    ca_key = _rsa_key()
    ca_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "hone-core CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name).issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
        .add_extension(x509.KeyUsage(
            digital_signature=False, content_commitment=False,
            key_encipherment=False, data_encipherment=False,
            key_agreement=False, key_cert_sign=True, crl_sign=True,
            encipher_only=False, decipher_only=False), True)
        .sign(ca_key, hashes.SHA256()))

    # --- the server certificate, signed by the CA --------------------------
    srv_key = _rsa_key()
    srv_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, hostnames[0])]))
        .issuer_name(ca_name)
        .public_key(srv_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=825))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False,
            key_encipherment=True, data_encipherment=False,
            key_agreement=False, key_cert_sign=False, crl_sign=False,
            encipher_only=False, decipher_only=False), True)
        .add_extension(x509.ExtendedKeyUsage(
            [ExtendedKeyUsageOID.SERVER_AUTH]), False)
        .add_extension(_san(hostnames), False)
        .sign(ca_key, hashes.SHA256()))

    _write(paths[_CA_CERT],
           ca_cert.public_bytes(serialization.Encoding.PEM), 0o644)
    _write(paths[_CA_KEY], _key_pem(ca_key), 0o600)
    _write(paths[_SRV_CERT],
           srv_cert.public_bytes(serialization.Encoding.PEM), 0o644)
    _write(paths[_SRV_KEY], _key_pem(srv_key), 0o600)
    return result


def ca_cert_pem(cert_dir):
    """The CA certificate as PEM text — handed to a node in its enrollment
       token response so it can validate hone-core's main-API TLS."""
    with open(os.path.join(cert_dir, _CA_CERT), encoding="utf-8") as f:
        return f.read()
