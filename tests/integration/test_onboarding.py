"""Onboarding publico contra PostgreSQL real con RLS (Sprint 15, fase 2).

Requiere base de datos: `pytest tests/ --run-db`. Prueba lo que un doble no puede: que el alta
cree de verdad las cuatro filas bajo RLS del tenant nuevo, que un email repetido no deje un
tenant huerfano, que la sesion devuelta sirva y que el token de verificacion no valga de Bearer.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from jose import jwt
from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import tenant_session
from app.core.security import create_email_verification_token
from app.main import create_app

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

REGISTER = "/api/v1/onboarding/register"
VERIFY = "/api/v1/onboarding/verify-email"


def _cuerpo(email: str, **extra: Any) -> dict[str, Any]:
    return {
        "business_name": "Panadería La Espiga",
        "business_type": "restaurant",
        "admin_full_name": "Ana Gómez Ruiz",
        "admin_email": email,
        "admin_password": "ClaveSegura1",
        "country": "CO",
        "language": "pt",
        "terms_accepted": True,
        **extra,
    }


def _ip() -> dict[str, str]:
    """IP distinta por llamada: el limite por IP no debe cruzarse entre tests."""
    return {"x-forwarded-for": f"10.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}.7"}


@pytest_asyncio.fixture
async def api(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[AsyncClient, None]:
    monkeypatch.setattr(get_settings(), "ONBOARDING_ENABLED", True)
    encolados: list[tuple[uuid.UUID, uuid.UUID, str]] = []

    async def _falso(client_id: uuid.UUID, user_id: uuid.UUID, email: str) -> None:
        encolados.append((client_id, user_id, email))

    monkeypatch.setattr("app.api.v1.onboarding._encolar_verificacion", _falso)
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.encolados = encolados  # type: ignore[attr-defined]
        yield c


async def _limpiar(client_id: uuid.UUID) -> None:
    async with tenant_session(client_id) as s:
        for tabla in ("token_budgets", "agent_configs", "users"):
            await s.execute(
                text(f"DELETE FROM {tabla} WHERE client_id = :c"),  # noqa: S608
                {"c": str(client_id)},
            )
        await s.execute(text("DELETE FROM clients WHERE id = :c"), {"c": str(client_id)})


async def _registrar(api: AsyncClient, email: str | None = None) -> dict[str, Any]:
    email = email or f"ana-{uuid.uuid4().hex[:10]}@example.com"
    r = await api.post(REGISTER, json=_cuerpo(email), headers=_ip())
    assert r.status_code == 201, r.text
    return {**r.json(), "email": email}


class TestRegistro:
    async def test_crea_tenant_admin_agente_y_presupuesto(self, api: AsyncClient) -> None:
        datos = await _registrar(api)
        cid = uuid.UUID(datos["client_id"])
        try:
            async with tenant_session(cid) as s:
                cliente = (
                    await s.execute(
                        text("SELECT plan, is_active, settings FROM clients WHERE id = :c"),
                        {"c": str(cid)},
                    )
                ).one()
                usuario = (
                    await s.execute(
                        text(
                            "SELECT role, first_name, last_name, settings, password_hash "
                            "FROM users WHERE client_id = :c"
                        ),
                        {"c": str(cid)},
                    )
                ).one()
                agentes = (
                    await s.execute(
                        text("SELECT count(*) FROM agent_configs WHERE client_id = :c"),
                        {"c": str(cid)},
                    )
                ).scalar_one()
                presupuesto = (
                    await s.execute(
                        text("SELECT total_budget FROM token_budgets WHERE client_id = :c"),
                        {"c": str(cid)},
                    )
                ).scalar_one()
            assert (cliente.plan, cliente.is_active) == ("free", True)
            assert cliente.settings["business_type"] == "restaurant"
            assert usuario.role == "admin"
            assert (usuario.first_name, usuario.last_name) == ("Ana", "Gómez Ruiz")
            assert usuario.settings == {"ui_language": "pt", "email_verified": False}
            assert usuario.password_hash.startswith("$2")
            assert agentes == 1
            assert presupuesto == get_settings().ONBOARDING_FREE_TOKEN_BUDGET
            assert api.encolados == [(cid, uuid.UUID(datos["user_id"]), datos["email"])]  # type: ignore[attr-defined]
        finally:
            await _limpiar(cid)

    async def test_la_sesion_devuelta_sirve_y_solo_ve_su_tenant(self, api: AsyncClient) -> None:
        a = await _registrar(api)
        b = await _registrar(api)
        try:
            r = await api.get(
                "/api/v1/admin/users", headers={"Authorization": f"Bearer {a['access_token']}"}
            )
            assert r.status_code == 200, r.text
            emails = [u["email"] for u in r.json()["items"]]
            assert emails == [a["email"]]
            assert b["email"] not in emails
        finally:
            await _limpiar(uuid.UUID(a["client_id"]))
            await _limpiar(uuid.UUID(b["client_id"]))

    async def test_el_login_funciona_con_la_contrasena_elegida(self, api: AsyncClient) -> None:
        datos = await _registrar(api)
        try:
            r = await api.post(
                "/api/v1/auth/login",
                json={"email": datos["email"], "password": "ClaveSegura1"},
            )
            assert r.status_code == 200, r.text
        finally:
            await _limpiar(uuid.UUID(datos["client_id"]))

    async def test_email_repetido_es_409_y_no_deja_un_tenant_huerfano(
        self, api: AsyncClient
    ) -> None:
        datos = await _registrar(api)
        try:
            r = await api.post(REGISTER, json=_cuerpo(datos["email"].upper()), headers=_ip())
            assert r.status_code == 409
            assert r.json()["error_code"] == "CONFLICT"
            async with tenant_session(uuid.UUID(datos["client_id"])) as s:
                n = (
                    await s.execute(text("SELECT count(*) FROM clients WHERE name LIKE 'Panader%'"))
                ).scalar_one()
            assert n == 1
        finally:
            await _limpiar(uuid.UUID(datos["client_id"]))

    async def test_apagado_responde_404_y_no_crea_nada(
        self, api: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(get_settings(), "ONBOARDING_ENABLED", False)
        email = f"no-{uuid.uuid4().hex[:8]}@example.com"
        r = await api.post(REGISTER, json=_cuerpo(email), headers=_ip())
        assert r.status_code == 404
        r = await api.post(VERIFY, json={"token": "x" * 20})
        assert r.status_code == 404
        login = await api.post(
            "/api/v1/auth/login", json={"email": email, "password": "ClaveSegura1"}
        )
        assert login.status_code == 401

    async def test_el_limite_por_ip_corta_con_429(
        self, api: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(get_settings(), "ONBOARDING_MAX_PER_IP_PER_HOUR", 2)
        cabecera = _ip()
        creados: list[str] = []
        try:
            for _ in range(2):
                r = await api.post(
                    REGISTER,
                    json=_cuerpo(f"l-{uuid.uuid4().hex[:8]}@example.com"),
                    headers=cabecera,
                )
                assert r.status_code == 201
                creados.append(r.json()["client_id"])
            r = await api.post(
                REGISTER, json=_cuerpo(f"l-{uuid.uuid4().hex[:8]}@example.com"), headers=cabecera
            )
            assert r.status_code == 429
            assert r.json()["error_code"] == "RATE_LIMITED"
        finally:
            for c in creados:
                await _limpiar(uuid.UUID(c))

    async def test_una_ip_falsificada_a_la_izquierda_no_evade_el_limite(
        self, api: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(get_settings(), "ONBOARDING_MAX_PER_IP_PER_HOUR", 1)
        real = _ip()["x-forwarded-for"]
        creados: list[str] = []
        try:
            r = await api.post(
                REGISTER,
                json=_cuerpo(f"f-{uuid.uuid4().hex[:8]}@example.com"),
                headers={"x-forwarded-for": f"1.1.1.1, {real}"},
            )
            assert r.status_code == 201
            creados.append(r.json()["client_id"])
            r = await api.post(
                REGISTER,
                json=_cuerpo(f"f-{uuid.uuid4().hex[:8]}@example.com"),
                headers={"x-forwarded-for": f"2.2.2.2, {real}"},
            )
            assert r.status_code == 429
        finally:
            for c in creados:
                await _limpiar(uuid.UUID(c))

    @pytest.mark.parametrize(
        "cambio",
        [
            {"admin_password": "todominusculas1"},
            {"admin_password": "Corta1"},
            {"language": "ja"},
            {"terms_accepted": False},
            {"country": "colombia"},
            {"business_type": "casino"},
        ],
    )
    async def test_datos_invalidos_son_422(self, api: AsyncClient, cambio: dict[str, Any]) -> None:
        r = await api.post(
            REGISTER, json=_cuerpo(f"v-{uuid.uuid4().hex[:8]}@example.com", **cambio), headers=_ip()
        )
        assert r.status_code == 422


class TestVerificacion:
    async def test_el_token_del_enlace_verifica_el_email(self, api: AsyncClient) -> None:
        datos = await _registrar(api)
        cid = uuid.UUID(datos["client_id"])
        try:
            token = create_email_verification_token(
                datos["user_id"], datos["client_id"], datos["email"]
            )
            r = await api.post(VERIFY, json={"token": token})
            assert r.status_code == 200
            assert r.json() == {"verified": True}
            async with tenant_session(cid) as s:
                ajustes = (
                    await s.execute(
                        text("SELECT settings FROM users WHERE client_id = :c"), {"c": str(cid)}
                    )
                ).scalar_one()
            assert ajustes == {"ui_language": "pt", "email_verified": True}
        finally:
            await _limpiar(cid)

    async def test_un_token_de_otro_email_no_verifica(self, api: AsyncClient) -> None:
        datos = await _registrar(api)
        try:
            token = create_email_verification_token(
                datos["user_id"], datos["client_id"], "otro@example.com"
            )
            r = await api.post(VERIFY, json={"token": token})
            assert r.status_code == 400
        finally:
            await _limpiar(uuid.UUID(datos["client_id"]))

    async def test_el_access_token_no_vale_como_enlace_y_viceversa(self, api: AsyncClient) -> None:
        datos = await _registrar(api)
        try:
            r = await api.post(VERIFY, json={"token": datos["access_token"]})
            assert r.status_code == 400
            token = create_email_verification_token(
                datos["user_id"], datos["client_id"], datos["email"]
            )
            r = await api.get("/api/v1/admin/users", headers={"Authorization": f"Bearer {token}"})
            assert r.status_code == 401
        finally:
            await _limpiar(uuid.UUID(datos["client_id"]))

    async def test_un_token_caducado_es_400(self, api: AsyncClient) -> None:
        datos = await _registrar(api)
        try:
            s = get_settings()
            caducado = jwt.encode(
                {
                    "user_id": datos["user_id"],
                    "client_id": datos["client_id"],
                    "email": datos["email"],
                    "type": "email_verification",
                    "exp": datetime.now(timezone.utc) - timedelta(minutes=1),
                },
                s.JWT_SECRET,
                algorithm=s.JWT_ALGORITHM,
            )
            r = await api.post(VERIFY, json={"token": caducado})
            assert r.status_code == 400
        finally:
            await _limpiar(uuid.UUID(datos["client_id"]))
