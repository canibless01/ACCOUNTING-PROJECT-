"""
AES-256-CBC encryption for Gmail OAuth tokens stored in Supabase.
The encryption key comes from the TOKEN_ENCRYPTION_KEY environment variable.
"""
from __future__ import annotations

import base64
import json
import os

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from config import Config


def _key() -> bytes:
    """Return the 32-byte encryption key derived from the hex config value."""
    raw = Config.TOKEN_ENCRYPTION_KEY
    if len(raw) == 64:          # hex string
        return bytes.fromhex(raw)
    return raw.encode()[:32]    # fallback: first 32 bytes of the string


def encrypt_token(token_dict: dict) -> str:
    """Encrypt a token dict → base64-encoded 'IV:ciphertext' string."""
    plaintext = json.dumps(token_dict).encode("utf-8")
    iv = os.urandom(16)
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    cipher = Cipher(algorithms.AES(_key()), modes.CBC(iv), backend=default_backend())
    enc = cipher.encryptor()
    ciphertext = enc.update(padded) + enc.finalize()
    return base64.b64encode(iv + ciphertext).decode("utf-8")


def decrypt_token(encrypted: str) -> dict:
    """Decrypt a base64 'IV:ciphertext' string → token dict."""
    raw = base64.b64decode(encrypted.encode("utf-8"))
    iv, ciphertext = raw[:16], raw[16:]
    cipher = Cipher(algorithms.AES(_key()), modes.CBC(iv), backend=default_backend())
    dec = cipher.decryptor()
    padded = dec.update(ciphertext) + dec.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    plaintext = unpadder.update(padded) + unpadder.finalize()
    return json.loads(plaintext.decode("utf-8"))
