"""Ed25519 signatures for the update feed: the maintainer signs, every install verifies.

The private key lives only on the maintainer's PC (``data/hive/maintainer_key.pem``, under ``data/``, which no build
ever includes); the public key (hex) goes into the public build's settings (``hive.public_key``).
"""

from __future__ import annotations

from pathlib import Path

KEY_FILE = Path("data") / "hive" / "maintainer_key.pem"


class SignatureError(ValueError):
    pass


def generate(path: Path) -> str:
    """A new key pair: the private key written to ``path`` (refusing to replace one), the public key returned (hex)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if path.exists():
        raise FileExistsError(f"{path} already holds a key: losing it would orphan every install, so it's kept")
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pem)
    return public_hex(path)


def _private(path: Path):  # type: ignore[no-untyped-def]
    from cryptography.hazmat.primitives import serialization

    return serialization.load_pem_private_key(path.read_bytes(), password=None)


def public_hex(path: Path) -> str:
    from cryptography.hazmat.primitives import serialization

    raw = _private(path).public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return raw.hex()


def sign(path: Path, data: bytes) -> str:
    """The signature of ``data`` (hex) with the private key at ``path``."""
    return _private(path).sign(data).hex()


def verify(public_key_hex: str, data: bytes, signature_hex: str) -> None:
    """Raises SignatureError unless ``signature_hex`` is the key's signature of ``data``."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex.strip()))
        key.verify(bytes.fromhex(signature_hex.strip()), data)
    except (InvalidSignature, ValueError) as exc:
        raise SignatureError("the signature doesn't match the maintainer's key") from exc
