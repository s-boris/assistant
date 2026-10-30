"""Private CA (EC P-256, 10 years), server cert (2 years, IP SANs) and device certs for the app's Keystore keys."""
import base64
import datetime as dt
import hashlib
import ipaddress
import os
import ssl

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .util import write_private


class PairError(Exception):
    pass


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _key_pem(key):
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def _name(cn):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def load_ca(paths):
    with open(paths.ca_key, "rb") as fh:
        key = serialization.load_pem_private_key(fh.read(), None)
    with open(paths.ca_pem, "rb") as fh:
        cert = x509.load_pem_x509_certificate(fh.read())
    return key, cert


def init_ca(paths, cn="phonegw private CA"):
    """-> True if created. Never overwrites an existing CA (the app trusts exactly this one)."""
    if os.path.exists(paths.ca_key) and os.path.exists(paths.ca_pem):
        return False
    key = ec.generate_private_key(ec.SECP256R1())
    ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
    cert = (x509.CertificateBuilder().subject_name(_name(cn)).issuer_name(_name(cn)).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(_now() - dt.timedelta(minutes=5))
            .not_valid_after(_now() + dt.timedelta(days=3652))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
                                         encipher_only=False, decipher_only=False), critical=True)
            .add_extension(ski, critical=False)
            .sign(key, hashes.SHA256()))
    write_private(paths.ca_key, _key_pem(key))
    write_private(paths.ca_pem, cert.public_bytes(serialization.Encoding.PEM), 0o600)
    return True


def server_sans(paths):
    try:
        with open(paths.server_pem, "rb") as fh:
            cert = x509.load_pem_x509_certificate(fh.read())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        return sorted(str(i) for i in san.get_values_for_type(x509.IPAddress)), sorted(san.get_values_for_type(x509.DNSName))
    except Exception:
        return None


def issue_server(paths, ips, dns, days=730, force=False):
    """-> True if (re)issued: missing, SANs changed, < 30 days left, or force."""
    ips = sorted({str(ipaddress.ip_address(i)) for i in list(ips) + ["127.0.0.1"]})
    dns = sorted(set(dns))
    if not force and os.path.exists(paths.server_key) and server_sans(paths) == (ips, dns):
        with open(paths.server_pem, "rb") as fh:
            c = x509.load_pem_x509_certificate(fh.read())
        if c.not_valid_after_utc - _now() > dt.timedelta(days=30):
            return False
    ca_key, ca = load_ca(paths)
    key = ec.generate_private_key(ec.SECP256R1())
    san = [x509.IPAddress(ipaddress.ip_address(i)) for i in ips] + [x509.DNSName(d) for d in dns]
    cert = (x509.CertificateBuilder().subject_name(_name(dns[0] if dns else "phonegw")).issuer_name(ca.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(_now() - dt.timedelta(minutes=5)).not_valid_after(_now() + dt.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    write_private(paths.server_key, _key_pem(key))
    write_private(paths.server_pem, cert.public_bytes(serialization.Encoding.PEM))
    return True


def public_info(paths):
    """-> (DNS SANs, IP SANs, days left) of the public cert, or None."""
    try:
        with open(paths.public_pem, "rb") as fh:
            c = x509.load_pem_x509_certificate(fh.read())
        san = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        return (sorted(san.get_values_for_type(x509.DNSName)), sorted(str(i) for i in san.get_values_for_type(x509.IPAddress)),
                (c.not_valid_after_utc - _now()).days)
    except Exception:
        return None


def issue_public(paths, host, days=730, force=False):
    """Internet-facing server cert: ONE DNS SAN (the public host), no IP SANs (probers learn no LAN/WG address), same CA
    as the LAN cert (the app already trusts it). -> True if (re)issued: missing, SAN changed, < 30 days left, or force."""
    info = public_info(paths)
    if not force and info and os.path.exists(paths.public_key) and info[0] == [host] and not info[1] and info[2] > 30:
        return False
    ca_key, ca = load_ca(paths)
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder().subject_name(_name(host)).issuer_name(ca.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(_now() - dt.timedelta(minutes=5)).not_valid_after(_now() + dt.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    write_private(paths.public_key, _key_pem(key))
    write_private(paths.public_pem, cert.public_bytes(serialization.Encoding.PEM))
    return True


def public_context(paths):
    """TLS 1.3 only, device certificate REQUIRED in the handshake (no cert, or one not from the phonegw CA: TLS alert)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_cert_chain(paths.public_pem, paths.public_key)
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(paths.ca_pem)
    return ctx


def parse_spki(b64):
    """base64 DER SubjectPublicKeyInfo -> EC P-256 public key (anything else refused)."""
    try:
        key = serialization.load_der_public_key(base64.b64decode(b64, validate=True))
    except Exception:
        raise PairError("bad_key")
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise PairError("key_type")
    return key


def issue_device(paths, pub_b64, device_id, days=730):
    """-> (cert PEM str, sha256 hex of the DER cert)."""
    pub = parse_spki(pub_b64)
    ca_key, ca = load_ca(paths)
    cert = (x509.CertificateBuilder().subject_name(_name(device_id)).issuer_name(ca.subject).public_key(pub)
            .serial_number(x509.random_serial_number()).not_valid_before(_now() - dt.timedelta(minutes=5))
            .not_valid_after(_now() + dt.timedelta(days=days))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False, crl_sign=False,
                                         encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    der = cert.public_bytes(serialization.Encoding.DER)
    return cert.public_bytes(serialization.Encoding.PEM).decode(), hashlib.sha256(der).hexdigest()


def fingerprint(der):
    return hashlib.sha256(der).hexdigest()


def server_context(paths):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_cert_chain(paths.server_pem, paths.server_key)
    ctx.verify_mode = ssl.CERT_OPTIONAL
    ctx.load_verify_locations(paths.ca_pem)
    return ctx


def ca_fingerprint(paths):
    with open(paths.ca_pem, "rb") as fh:
        c = x509.load_pem_x509_certificate(fh.read())
    return fingerprint(c.public_bytes(serialization.Encoding.DER))
