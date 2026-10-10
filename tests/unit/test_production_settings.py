"""Configuracion de produccion (ADR-088): sin secretos de ejemplo, sin CORS abierto, sin docs."""

from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings

SECRETO = "kX9vR2mQ7pL4tW8zN1cB6dF3hJ5sA0eY"


def _settings(**cambios: Any) -> Settings:
    base: dict[str, Any] = {
        "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/db",
        "JWT_SECRET": SECRETO,
        "ENCRYPTION_KEY": SECRETO[::-1],
        "APP_ENV": "production",
        "CORS_ORIGINS": ["https://app.ejemplo.co"],
    }
    base.update(cambios)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


class TestSecretosDeEjemplo:
    def test_produccion_con_secretos_aleatorios_arranca(self) -> None:
        assert _settings().APP_ENV == "production"

    @pytest.mark.parametrize("nombre", ["JWT_SECRET", "ENCRYPTION_KEY"])
    def test_el_valor_del_env_example_se_rechaza(self, nombre: str) -> None:
        with pytest.raises(ValidationError, match=nombre):
            _settings(**{nombre: "your_encryption_key_min_32_chars_here"})

    def test_en_desarrollo_se_permiten(self) -> None:
        s = _settings(APP_ENV="development", JWT_SECRET="your_supabase_jwt_secret_here_123456")
        assert s.APP_ENV == "development"

    def test_cors_abierto_con_credenciales_se_rechaza(self) -> None:
        with pytest.raises(ValidationError, match="CORS_ORIGINS"):
            _settings(CORS_ORIGINS=["*"])


class TestDocsEnProduccion:
    def _rutas(self, monkeypatch: pytest.MonkeyPatch, entorno: str) -> set[str]:
        from app.main import create_app

        monkeypatch.setattr(get_settings(), "APP_ENV", entorno)
        return {getattr(r, "path", "") for r in create_app().routes}

    def test_en_produccion_no_hay_swagger_ni_openapi(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rutas = self._rutas(monkeypatch, "production")
        assert not {"/api/docs", "/api/redoc", "/api/openapi.json"} & rutas

    def test_en_desarrollo_si(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rutas = self._rutas(monkeypatch, "development")
        assert {"/api/docs", "/api/openapi.json"} <= rutas
