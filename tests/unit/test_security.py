"""Tests unitarios para app.core.security.

Verifica hash/verify de passwords y create/decode de JWT.
NO requieren base de datos.
"""

import os
import time

import pytest

# Configurar variables de entorno ANTES de importar módulos de la app
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-testing-only-minimum-32-chars")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key-minimum-32-characters-long")

from app.core.exceptions import JWTExpiredError, JWTInvalidError
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_jwt,
    hash_password,
    verify_password,
)


class TestPasswordHashing:
    """Tests de hash y verificación de passwords con bcrypt."""

    def test_hash_password_returns_hash(self) -> None:
        """hash_password retorna un hash bcrypt válido."""
        hashed = hash_password("mi_password_seguro")
        assert hashed != "mi_password_seguro"
        assert hashed.startswith("$2b$")

    def test_verify_password_correct(self) -> None:
        """verify_password retorna True con password correcto."""
        hashed = hash_password("mi_password_seguro")
        assert verify_password("mi_password_seguro", hashed) is True

    def test_verify_password_incorrect(self) -> None:
        """verify_password retorna False con password incorrecto."""
        hashed = hash_password("mi_password_seguro")
        assert verify_password("password_incorrecto", hashed) is False

    def test_hash_is_unique(self) -> None:
        """Dos hashes del mismo password son diferentes (salt)."""
        hash1 = hash_password("mismo_password")
        hash2 = hash_password("mismo_password")
        assert hash1 != hash2  # bcrypt usa salt aleatorio

    def test_both_hashes_verify(self) -> None:
        """Ambos hashes del mismo password verifican correctamente."""
        hash1 = hash_password("mismo_password")
        hash2 = hash_password("mismo_password")
        assert verify_password("mismo_password", hash1) is True
        assert verify_password("mismo_password", hash2) is True


class TestJWT:
    """Tests de creación y decodificación de JWT."""

    SAMPLE_DATA = {
        "user_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "client_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "email": "test@example.com",
        "role": "admin",
    }

    def test_create_access_token(self) -> None:
        """create_access_token genera un token JWT válido."""
        token = create_access_token(self.SAMPLE_DATA)
        assert isinstance(token, str)
        assert len(token) > 0

    def test_decode_access_token(self) -> None:
        """decode_jwt decodifica correctamente un access token."""
        token = create_access_token(self.SAMPLE_DATA)
        payload = decode_jwt(token)
        assert payload["user_id"] == self.SAMPLE_DATA["user_id"]
        assert payload["client_id"] == self.SAMPLE_DATA["client_id"]
        assert payload["email"] == self.SAMPLE_DATA["email"]
        assert payload["role"] == self.SAMPLE_DATA["role"]
        assert payload["type"] == "access"

    def test_create_refresh_token(self) -> None:
        """create_refresh_token genera un token con type=refresh."""
        token = create_refresh_token(self.SAMPLE_DATA)
        payload = decode_jwt(token)
        assert payload["type"] == "refresh"
        assert payload["user_id"] == self.SAMPLE_DATA["user_id"]

    def test_access_and_refresh_are_different(self) -> None:
        """Access y refresh tokens son diferentes."""
        access = create_access_token(self.SAMPLE_DATA)
        refresh = create_refresh_token(self.SAMPLE_DATA)
        assert access != refresh

    def test_decode_invalid_token_raises(self) -> None:
        """decode_jwt lanza JWTInvalidError con token corrupto."""
        with pytest.raises(JWTInvalidError):
            decode_jwt("token.invalido.corrupto")

    def test_decode_expired_token_raises(self) -> None:
        """decode_jwt lanza JWTExpiredError con token expirado."""
        from datetime import timedelta

        token = create_access_token(
            self.SAMPLE_DATA,
            expires_delta=timedelta(seconds=-1),
        )
        # Esperar un instante para asegurar expiración
        time.sleep(0.1)
        with pytest.raises(JWTExpiredError):
            decode_jwt(token)

    def test_token_contains_exp(self) -> None:
        """El payload del token contiene campo 'exp'."""
        token = create_access_token(self.SAMPLE_DATA)
        payload = decode_jwt(token)
        assert "exp" in payload
        assert isinstance(payload["exp"], int)
