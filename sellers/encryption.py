import os
import base64
from cryptography.fernet import Fernet, InvalidToken

def _get_fernet():
    key = os.environ.get('BANK_ENCRYPTION_KEY', '')
    if not key:
        # Derive a stable key from Django SECRET_KEY so dev works without extra config
        from django.conf import settings
        raw = settings.SECRET_KEY.encode()
        # Pad/truncate to 32 bytes, then base64url-encode to get a valid Fernet key
        raw = (raw * 4)[:32]
        key = base64.urlsafe_b64encode(raw).decode()
    return Fernet(key.encode() if isinstance(key, str) else key)

def encrypt_field(value: str) -> str:
    if not value:
        return value
    return _get_fernet().encrypt(value.encode()).decode()

def decrypt_field(token: str) -> str:
    if not token:
        return token
    try:
        return _get_fernet().decrypt(token.encode()).decode()
    except (InvalidToken, Exception):
        return token  # return as-is if already plaintext (legacy rows)

def mask_account(value: str) -> str:
    if not value or len(value) <= 4:
        return '****'
    return '****' + value[-4:]
