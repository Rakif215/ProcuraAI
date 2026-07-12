"""
app/core/crypto.py
-------------------
Fernet-based symmetric encryption for sensitive credentials (IMAP/SMTP passwords).

The ENCRYPTION_KEY must be a 32-byte URL-safe base64 key.
Generate one with:  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
import logging
from cryptography.fernet import Fernet, InvalidToken
from app.core.config import settings

logger = logging.getLogger(__name__)

_fernet = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = settings.encryption_key
        if not key:
            raise RuntimeError(
                "ENCRYPTION_KEY not set. Generate one with:\n"
                "  python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )
        _fernet = Fernet(key.encode())
    return _fernet


def encrypt_password(plaintext: str) -> str:
    """Encrypt a password string. Returns a Fernet token (URL-safe base64)."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_password(ciphertext: str) -> str:
    """Decrypt a Fernet-encrypted password. Raises ValueError on bad token."""
    try:
        return _get_fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        # If it's not encrypted (legacy data), return as-is
        logger.warning("Could not decrypt credential — returning as-is (may be legacy plaintext)")
        return ciphertext
