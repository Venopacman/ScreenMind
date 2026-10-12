"""Comprehensive tests for encryption at rest."""
import pytest
import io
from pathlib import Path
from unittest.mock import patch, MagicMock
from PIL import Image

from evolve_capture.privacy.encryption import (
    is_encrypted, encrypt_image, decrypt_image_bytes,
    open_image, serve_image, MAGIC, MAGIC_LEN,
)


class TestIsEncrypted:
    """Tests for encrypted file detection."""

    def test_normal_jpeg_not_encrypted(self, tmp_path):
        f = tmp_path / "test.jpg"
        f.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
        assert is_encrypted(f) is False

    def test_magic_header_detected(self, tmp_path):
        f = tmp_path / "test.jpg"
        f.write_bytes(MAGIC + b"\x00" * 100)
        assert is_encrypted(f) is True

    def test_nonexistent_file(self):
        assert is_encrypted(Path("/nonexistent/file.jpg")) is False

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.jpg"
        f.write_bytes(b"")
        assert is_encrypted(f) is False

    def test_short_file(self, tmp_path):
        """File shorter than magic header length."""
        f = tmp_path / "short.jpg"
        f.write_bytes(b"short")
        assert is_encrypted(f) is False

    def test_partial_magic_not_detected(self, tmp_path):
        """Partial magic header is not a match."""
        f = tmp_path / "partial.jpg"
        f.write_bytes(MAGIC[:8] + b"\x00" * 100)
        assert is_encrypted(f) is False


class TestEncryptDecrypt:
    """Tests for encrypt/decrypt roundtrip."""

    @patch("evolve_capture.privacy.encryption.settings")
    def test_encrypt_disabled_returns_false(self, mock_settings, tmp_path):
        """Encryption does nothing when disabled."""
        mock_settings.encryption_enabled = False
        f = tmp_path / "test.jpg"
        f.write_bytes(b"original data")
        assert encrypt_image(f) is False
        assert f.read_bytes() == b"original data"

    @patch("evolve_capture.privacy.encryption._get_fernet")
    @patch("evolve_capture.privacy.encryption.settings")
    def test_encrypt_then_decrypt_roundtrip(self, mock_settings, mock_fernet, tmp_path):
        """Encrypted file decrypts back to original."""
        from cryptography.fernet import Fernet
        key = Fernet.generate_key()
        real_fernet = Fernet(key)
        mock_fernet.return_value = real_fernet
        mock_settings.encryption_enabled = True

        f = tmp_path / "test.jpg"
        original = b"\xff\xd8\xff\xe0" + b"image data here" * 20
        f.write_bytes(original)

        result = encrypt_image(f)
        assert result is True
        assert is_encrypted(f) is True
        assert f.read_bytes() != original  # file changed

        decrypted = decrypt_image_bytes(f)
        assert decrypted == original

    @patch("evolve_capture.privacy.encryption._get_fernet")
    @patch("evolve_capture.privacy.encryption.settings")
    def test_double_encrypt_is_noop(self, mock_settings, mock_fernet, tmp_path):
        """Encrypting an already-encrypted file doesn't double-encrypt."""
        from cryptography.fernet import Fernet
        key = Fernet.generate_key()
        mock_fernet.return_value = Fernet(key)
        mock_settings.encryption_enabled = True

        f = tmp_path / "test.jpg"
        f.write_bytes(b"\xff\xd8\xff\xe0" + b"data" * 10)

        encrypt_image(f)
        size_after_first = f.stat().st_size
        encrypt_image(f)
        size_after_second = f.stat().st_size
        assert size_after_first == size_after_second

    def test_decrypt_unencrypted_returns_raw(self, tmp_path):
        """Decrypting an unencrypted file returns raw bytes (passthrough)."""
        f = tmp_path / "test.jpg"
        data = b"\xff\xd8\xff\xe0plaintext"
        f.write_bytes(data)
        assert decrypt_image_bytes(f) == data

    def test_decrypt_nonexistent_returns_none(self):
        """Decrypting non-existent file returns None."""
        assert decrypt_image_bytes(Path("/no/such/file.jpg")) is None

    @patch("evolve_capture.privacy.encryption._get_fernet")
    def test_decrypt_with_no_key_returns_none(self, mock_fernet, tmp_path):
        """Encrypted file with unavailable key returns None."""
        mock_fernet.return_value = None
        f = tmp_path / "test.jpg"
        f.write_bytes(MAGIC + b"encrypted stuff")
        assert decrypt_image_bytes(f) is None


class TestOpenImage:
    """Tests for open_image() PIL wrapper."""

    def test_open_unencrypted_jpeg(self, tmp_path):
        """Opens a normal JPEG file."""
        f = tmp_path / "test.jpg"
        img = Image.new("RGB", (100, 50), color="red")
        img.save(str(f), "JPEG")

        result = open_image(f)
        assert result.size == (100, 50)

    @patch("evolve_capture.privacy.encryption._get_fernet")
    @patch("evolve_capture.privacy.encryption.settings")
    def test_open_encrypted_jpeg(self, mock_settings, mock_fernet, tmp_path):
        """Opens an encrypted JPEG transparently."""
        from cryptography.fernet import Fernet
        key = Fernet.generate_key()
        real_fernet = Fernet(key)
        mock_fernet.return_value = real_fernet
        mock_settings.encryption_enabled = True

        f = tmp_path / "test.jpg"
        img = Image.new("RGB", (100, 50), color="blue")
        img.save(str(f), "JPEG")
        encrypt_image(f)

        result = open_image(f)
        assert result.size == (100, 50)


class TestServeImage:
    """Tests for serve_image() FastAPI response."""

    def test_serve_unencrypted_returns_file_response(self, tmp_path):
        """Unencrypted file returns FileResponse."""
        from fastapi.responses import FileResponse
        f = tmp_path / "test.jpg"
        img = Image.new("RGB", (50, 50), color="green")
        img.save(str(f), "JPEG")

        response = serve_image(f)
        assert isinstance(response, FileResponse)

    def test_serve_encrypted_returns_response(self, tmp_path):
        """Encrypted file returns Response with decrypted bytes."""
        from fastapi.responses import Response
        from cryptography.fernet import Fernet

        f = tmp_path / "test.jpg"
        img = Image.new("RGB", (50, 50), color="green")
        buf = io.BytesIO()
        img.save(buf, "JPEG")
        original_bytes = buf.getvalue()

        # Manually encrypt
        key = Fernet.generate_key()
        fernet = Fernet(key)
        encrypted = fernet.encrypt(original_bytes)
        f.write_bytes(MAGIC + encrypted)

        with patch("evolve_capture.privacy.encryption._get_fernet", return_value=fernet):
            response = serve_image(f)
        assert isinstance(response, Response)
        assert response.body == original_bytes
