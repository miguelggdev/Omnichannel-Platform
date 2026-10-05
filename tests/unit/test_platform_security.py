"""Vision general de seguridad: las reglas de cada comprobacion, sin base de datos.

El recorrido contra PostgreSQL real (catalogo de RLS y rol de la app) esta en
`tests/integration/test_platform_security.py`.
"""

from types import SimpleNamespace

import pytest

from app.api.v1.platform_security import FAIL, OK, WARN, comprobar_configuracion, comprobar_rol
from app.core.config import Settings


def _ajustes(**cambios: object) -> Settings:
    base: dict[str, object] = {
        "DATABASE_URL": "postgresql+asyncpg://u:p@h/d",
        "JWT_SECRET": "x" * 40,
        "ENCRYPTION_KEY": "y" * 40,
        "APP_ENV": "production",
        "CORS_ORIGINS": ["https://app.example.com"],
        "YCLOUD_WEBHOOK_SECRET": "a",
        "META_APP_SECRET": "b",
        "TELEGRAM_WEBHOOK_SECRET": "c",
        "EMAIL_INBOUND_WEBHOOK_SECRET": "d",
        "ONBOARDING_ENABLED": False,
    }
    return Settings(_env_file=None, **{**base, **cambios})  # type: ignore[arg-type]


def _estado(ajustes: Settings) -> dict[str, str]:
    return {c.id: c.status for c in comprobar_configuracion(ajustes)}


def test_una_configuracion_correcta_de_produccion_no_avisa_de_nada() -> None:
    assert set(_estado(_ajustes()).values()) == {OK}


def test_fuera_de_produccion_se_avisa_del_entorno() -> None:
    assert _estado(_ajustes(APP_ENV="development"))["environment"] == WARN


def test_cors_con_comodin_es_un_fallo_y_con_localhost_en_produccion_un_aviso() -> None:
    assert _estado(_ajustes(CORS_ORIGINS=["*"]))["cors"] == FAIL
    assert _estado(_ajustes(CORS_ORIGINS=["http://localhost:3000"]))["cors"] == WARN
    assert (
        _estado(_ajustes(APP_ENV="development", CORS_ORIGINS=["http://localhost:3000"]))["cors"]
        == OK
    )


def test_un_jwt_muy_largo_es_un_aviso() -> None:
    assert _estado(_ajustes(JWT_EXPIRATION_MINUTES=60))["jwt_lifetime"] == OK
    assert _estado(_ajustes(JWT_EXPIRATION_MINUTES=61))["jwt_lifetime"] == WARN


def test_secretos_de_webhook_ausentes_se_nombran_sin_valores() -> None:
    checks = {
        c.id: c
        for c in comprobar_configuracion(_ajustes(META_APP_SECRET="", TELEGRAM_WEBHOOK_SECRET=""))
    }
    c = checks["webhook_secrets"]
    assert c.status == WARN
    assert c.detail == "META_APP_SECRET, TELEGRAM_WEBHOOK_SECRET"


def test_ninguna_comprobacion_filtra_un_secreto() -> None:
    ajustes = _ajustes(
        JWT_SECRET="secreto-jwt-" + "z" * 30, ENCRYPTION_KEY="secreto-enc-" + "w" * 30
    )
    texto = repr([c.model_dump() for c in comprobar_configuracion(ajustes)])
    assert "secreto-jwt" not in texto
    assert "secreto-enc" not in texto


def test_con_onboarding_y_sin_saltos_de_proxy_se_avisa() -> None:
    estado = _estado(
        _ajustes(
            ONBOARDING_ENABLED=True,
            TRUSTED_PROXY_HOPS=0,
            EMAIL_SMTP_HOST="h",
            EMAIL_FROM_ADDRESS="a@b.c",
        )
    )
    assert estado["onboarding_proxy_hops"] == WARN
    assert estado["onboarding_smtp"] == OK
    assert "onboarding_disabled" not in estado


def test_con_onboarding_sin_smtp_se_avisa() -> None:
    assert _estado(_ajustes(ONBOARDING_ENABLED=True))["onboarding_smtp"] == WARN


@pytest.mark.parametrize(
    ("fila", "estado", "detalle"),
    [
        (SimpleNamespace(rolsuper=False, rolbypassrls=False), OK, None),
        (SimpleNamespace(rolsuper=True, rolbypassrls=False), FAIL, "superuser"),
        (SimpleNamespace(rolsuper=False, rolbypassrls=True), FAIL, "BYPASSRLS"),
        (None, WARN, "no se pudo leer el rol"),
    ],
)
def test_el_rol_de_la_app_que_salta_rls_es_un_fallo(
    fila: object, estado: str, detalle: str | None
) -> None:
    c = comprobar_rol(fila)
    assert (c.status, c.detail) == (estado, detalle)
