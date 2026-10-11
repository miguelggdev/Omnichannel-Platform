"""Que tipo de token autentica una peticion (TenantContextMiddleware).

Solo un access token. Un refresh token vive 7 dias y `/auth/refresh` es el unico sitio que lo
revalida contra `is_active`; si valiera como Bearer, desactivar a un usuario o suspender a un
tenant no cortaria su acceso hasta que el refresh caducara.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
import pytest

from app.core.config import get_settings
from app.core.security import create_access_token, create_refresh_token

URL = "/api/v1/contacts"  # protegido; la respuesta exacta da igual, lo que cuenta es 401 o no


def _claims(**cambios: Any) -> dict[str, Any]:
    return {
        "user_id": str(uuid.uuid4()),
        "client_id": str(uuid.uuid4()),
        "email": "a@example.com",
        "role": "admin",
        **cambios,
    }


def _firmar(payload: dict[str, Any], **comunes: Any) -> str:
    """Firma con el secreto y los `iss`/`aud` propios: solo falla lo que el test cambia."""
    ajustes = get_settings()
    ahora = datetime.now(timezone.utc)
    cuerpo = {
        "exp": ahora + timedelta(minutes=5),
        "iat": ahora,
        "iss": ajustes.JWT_ISSUER,
        "aud": ajustes.JWT_AUDIENCE,
        **comunes,
        **payload,
    }
    return str(jwt.encode(cuerpo, ajustes.JWT_SECRET, algorithm=ajustes.JWT_ALGORITHM))


async def _get(cliente: Any, token: str) -> Any:
    return await cliente.get(URL, headers={"Authorization": f"Bearer {token}"})


async def test_un_refresh_token_no_autentica_peticiones(api_client: Any) -> None:
    c = _claims()
    r = await _get(api_client, create_refresh_token(c["user_id"], c["client_id"], 0))

    assert r.status_code == 401
    assert r.json()["error_code"] == "INVALID_TOKEN"


@pytest.mark.parametrize("tipo", ["email_verification", "password_reset", "algo", ""])
async def test_un_token_de_otro_tipo_firmado_con_el_mismo_secreto_tampoco(
    api_client: Any, tipo: str
) -> None:
    r = await _get(api_client, _firmar({**_claims(), "type": tipo}))

    assert r.status_code == 401
    assert r.json()["error_code"] == "INVALID_TOKEN"


async def test_un_token_sin_tipo_no_autentica(api_client: Any) -> None:
    r = await _get(api_client, _firmar(_claims()))

    assert r.status_code == 401


@pytest.mark.parametrize("falta", ["user_id", "client_id", "role"])
async def test_un_token_sin_alguna_claim_es_401_y_no_un_500(api_client: Any, falta: str) -> None:
    claims = _claims()
    del claims[falta]

    r = await _get(api_client, _firmar({**claims, "type": "access"}))

    assert r.status_code == 401
    assert r.json()["error_code"] == "INVALID_TOKEN"


@pytest.mark.parametrize(
    "cambio",
    [
        {"user_id": "no-es-uuid"},
        {"client_id": 123},
        {"client_id": None},
        {"role": 5},
        {"role": None},
    ],
)
async def test_claims_con_valores_invalidos_son_401(
    api_client: Any, cambio: dict[str, Any]
) -> None:
    r = await _get(api_client, _firmar({**_claims(**cambio), "type": "access"}))

    assert r.status_code == 401


async def test_un_access_token_valido_sigue_pasando(
    api_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api.v1 import contacts as contacts_module
    from tests.unit.agent_doubles import fake_tenant_session
    from tests.unit.crm_doubles import CrmSession

    sesion = CrmSession(resultados=[[]], escalares=[0])
    monkeypatch.setattr(contacts_module, "tenant_session", fake_tenant_session(sesion))

    r = await _get(api_client, create_access_token(_claims()))

    assert r.status_code == 200


@pytest.mark.parametrize(
    "comunes",
    [
        # Lo que emite Supabase Auth: si alguien reutilizo su secreto JWT, la firma cuadra,
        # pero el emisor y la audiencia no son los nuestros (ADR-089).
        {"iss": "https://proyecto.supabase.co/auth/v1", "aud": "authenticated"},
        {"aud": "authenticated"},
        {"iss": "otro-emisor"},
    ],
)
async def test_un_token_de_otro_emisor_con_el_mismo_secreto_no_autentica(
    api_client: Any, comunes: dict[str, Any]
) -> None:
    r = await _get(api_client, _firmar({**_claims(), "type": "access"}, **comunes))

    assert r.status_code == 401
    assert r.json()["error_code"] == "INVALID_TOKEN"


async def test_un_token_sin_iss_ni_aud_no_autentica(api_client: Any) -> None:
    """Los tokens de antes de ADR-089 (sin `iss`/`aud`) dejan de valer."""
    ajustes = get_settings()
    cuerpo = {
        **_claims(),
        "type": "access",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    token = jwt.encode(cuerpo, ajustes.JWT_SECRET, algorithm=ajustes.JWT_ALGORITHM)

    r = await _get(api_client, token)

    assert r.status_code == 401


async def test_alg_none_no_autentica(api_client: Any) -> None:
    ajustes = get_settings()
    cuerpo = {
        **_claims(),
        "type": "access",
        "iss": ajustes.JWT_ISSUER,
        "aud": ajustes.JWT_AUDIENCE,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    token = jwt.encode(cuerpo, None, algorithm="none")

    r = await _get(api_client, token)

    assert r.status_code == 401
