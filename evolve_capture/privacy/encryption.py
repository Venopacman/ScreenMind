"""
Encryption at Rest
Encrypts screenshots using Fernet (AES-128-CBC) with OS keyring-stored key.
Transparent layer: encrypted files have a magic header, unencrypted pass through.

Usage:
    from evolve_capture.privacy.encryption import encrypt_image, decrypt_image, open_image, serve_image

    # Write: save screenshot then encrypt in-place
    img.save(filepath, "JPEG", quality=80)
    encrypt_image(filepath)

    # Read: transparent — handles both encrypted and unencrypted
    pil_img = open_image(filepath)

    # Serve: returns Response (handles decrypt + streaming)
    return serve_image(filepath)
"""

import logging
import io
import os
import sys
from pathlib import Path
from typing import Optional

from evolve_capture.config import settings

logger = logging.getLogger("evolve_capture.privacy.encryption")

# Magic header to identify encrypted files (16 bytes)
MAGIC = b"OPENRECALL_ENC\x01\x00"
MAGIC_LEN = len(MAGIC)

# ── Key Management ───────────────────────────────────────────────────

_fernet_instance = None


def _get_keyring():
    """Import keyring lazily to avoid hard dependency."""
    try:
        import keyring
        return keyring
    except ImportError:
        return None


KEYRING_SERVICE = "evolve-capture"
KEYRING_ENTRY = "encryption_key"
# ScreenMind, the old name of this app (before 0.3.0), kept its key here
OLD_KEYRING_SERVICE = "screenmind"


def _keyring_key(keyring) -> Optional[bytes]:
    """The key from the OS keyring, copied over from the old service if needed.

    1. Our entry exists: use it. If the old entry holds the same key, delete
       the old one now: it was copied and checked on an earlier start.
    2. Only the old entry exists: write it under our service, read it back,
       and use it. The old entry stays until a later start (1).
    3. Neither: None. The caller then tries the key file.

    Never returns a new key while an old one exists: that key decrypts the
    screenshots already on disk.
    """
    try:
        key = keyring.get_password(KEYRING_SERVICE, KEYRING_ENTRY)
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not read the key from the OS keyring: %s", e)
        key = None
    try:
        old = keyring.get_password(OLD_KEYRING_SERVICE, KEYRING_ENTRY)
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not read ScreenMind's key from the OS keyring: %s", e)
        old = None

    if key:
        if old == key:
            try:
                keyring.delete_password(OLD_KEYRING_SERVICE, KEYRING_ENTRY)
                logger.info("Deleted ScreenMind's copy of the key from the OS keyring")
            except Exception as e:  # noqa: BLE001
                logger.warning("Could not delete ScreenMind's key from the OS keyring: %s", e)
        elif old:
            logger.warning("The OS keyring holds two different keys (%s and %s). Using %s; "
                           "the other one is kept.", KEYRING_SERVICE, OLD_KEYRING_SERVICE,
                           KEYRING_SERVICE)
        return key.encode()
    if not old:
        return None

    try:
        keyring.set_password(KEYRING_SERVICE, KEYRING_ENTRY, old)
        copied = keyring.get_password(KEYRING_SERVICE, KEYRING_ENTRY) == old
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not copy ScreenMind's key in the OS keyring: %s", e)
        copied = False
    if copied:
        logger.info("Copied the key from keyring service %s to %s",
                    OLD_KEYRING_SERVICE, KEYRING_SERVICE)
    else:
        logger.warning("Using ScreenMind's key; it is not copied to %s yet", KEYRING_SERVICE)
    return old.encode()


def _get_or_create_key() -> bytes:
    """Get encryption key from OS keyring, or create one on first use."""
    keyring = _get_keyring()

    if keyring:
        # Windows Credential Manager / macOS Keychain first
        key = _keyring_key(keyring)
        if key:
            return key

    # Fallback: file-based key in data directory
    key_file = settings.data_path / ".encryption_key"
    if key_file.exists():
        return key_file.read_bytes().strip()

    # Generate new key
    from cryptography.fernet import Fernet
    key = Fernet.generate_key()

    # Try to store in OS keyring
    if keyring:
        try:
            keyring.set_password(KEYRING_SERVICE, KEYRING_ENTRY, key.decode())
            logger.info("Key stored in OS keyring")
        except Exception:
            pass

    # Always write file-based backup
    key_file.write_bytes(key)
    # Restrict permissions (Windows: best effort)
    if sys.platform != "win32":
        os.chmod(str(key_file), 0o600)
    logger.info("Generated new encryption key")

    return key


def _get_fernet():
    """Get or create Fernet instance (cached)."""
    global _fernet_instance
    if _fernet_instance is None:
        try:
            from cryptography.fernet import Fernet
            key = _get_or_create_key()
            _fernet_instance = Fernet(key)
        except ImportError:
            logger.warning("'cryptography' package not installed — encryption disabled")
            return None
        except Exception as e:
            logger.error(f"Failed to initialize: {e}")
            return None
    return _fernet_instance


# ── Core Functions ───────────────────────────────────────────────────

def is_encrypted(filepath: Path) -> bool:
    """Check if a file has the encryption magic header."""
    try:
        with open(filepath, "rb") as f:
            header = f.read(MAGIC_LEN)
        return header == MAGIC
    except Exception:
        return False


def encrypt_image(filepath: Path) -> bool:
    """
    Encrypt a screenshot file in-place.
    Only encrypts if encryption is enabled in settings.
    Returns True if encrypted, False if skipped.
    """
    if not getattr(settings, 'encryption_enabled', False):
        return False

    fernet = _get_fernet()
    if fernet is None:
        return False

    filepath = Path(filepath)
    if not filepath.exists():
        return False

    # Don't double-encrypt
    if is_encrypted(filepath):
        return True

    try:
        plaintext = filepath.read_bytes()
        ciphertext = fernet.encrypt(plaintext)

        # Write with magic header
        with open(filepath, "wb") as f:
            f.write(MAGIC)
            f.write(ciphertext)

        return True
    except Exception as e:
        logger.error(f"Failed to encrypt {filepath.name}: {e}")
        return False


def decrypt_image_bytes(filepath: Path) -> Optional[bytes]:
    """
    Read and decrypt a screenshot file.
    Transparent: if file is not encrypted, returns raw bytes.
    """
    filepath = Path(filepath)
    if not filepath.exists():
        return None

    try:
        raw = filepath.read_bytes()

        # Check magic header
        if raw[:MAGIC_LEN] == MAGIC:
            fernet = _get_fernet()
            if fernet is None:
                logger.error(f"Cannot decrypt {filepath.name} — no key available")
                return None
            return fernet.decrypt(raw[MAGIC_LEN:])
        else:
            # Not encrypted — return as-is
            return raw
    except Exception as e:
        logger.error(f"Failed to decrypt {filepath.name}: {e}")
        return None


def open_image(filepath: Path):
    """
    Open an image file as PIL Image. Handles encrypted + unencrypted transparently.
    Drop-in replacement for PIL.Image.open(filepath).
    """
    from PIL import Image

    filepath = Path(filepath)

    if is_encrypted(filepath):
        data = decrypt_image_bytes(filepath)
        if data is None:
            raise IOError(f"Cannot decrypt {filepath}")
        return Image.open(io.BytesIO(data))
    else:
        # Not encrypted — normal PIL open (fastest path)
        return Image.open(filepath)


def serve_image(filepath: Path):
    """
    Create a FastAPI Response for an image file. Handles encrypted + unencrypted.
    Drop-in replacement for FileResponse(filepath).
    """
    from fastapi.responses import FileResponse, Response

    filepath = Path(filepath)

    if is_encrypted(filepath):
        data = decrypt_image_bytes(filepath)
        if data is None:
            from fastapi import HTTPException
            raise HTTPException(status_code=500, detail="Cannot decrypt screenshot")
        return Response(content=data, media_type="image/jpeg")
    else:
        # Not encrypted — use efficient FileResponse (zero-copy sendfile)
        return FileResponse(str(filepath), media_type="image/jpeg")
