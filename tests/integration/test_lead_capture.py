# ruff: noqa: F811
"""Captura publica de leads contra PostgreSQL y Redis reales (Sprint 16, ADR-083).

Requiere base de datos: `pytest tests/ --run-db`. Es el unico endpoint de leads sin JWT, asi que
aqui se prueba lo que lo hace seguro: que la resolucion del token no cruce tenants, que todas las
fuentes inutilizables respondan igual, que un duplicado o un honeypot sean indistinguibles de un
alta, los limites por IP y por fuente (y que fallen cerrados), el consentimiento y el CORS acotado.
"""

import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import tenant_session
from app.main import create_app
from app.services.lead_pipeline import generate_capture_token
from tests.integration.lead_helpers import (
    activar_modulo,
    crear_etapa,
    crear_fuente,
    crear_lead,
    limpiar_leads,
)
from tests.integration.test_crm_api import Escenario, dos_tenants, escenario  # noqa: F401

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

CAPTURA = "/api/v1/capture"


def _ip() -> dict[str, str]:
    """IP distinta por llamada: los limites por IP no deben cruzarse entre tests."""
    n = uuid.uuid4().int
    return {"x-forwarded-for": f"10.{n % 250}.{(n >> 8) % 250}.{(n >> 16) % 250}"}


@pytest_asyncio.fixture
async def api() -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def crm(escenario: Escenario) -> AsyncGenerator[Escenario, None]:
    await activar_modulo(escenario.client_id)
    yield escenario
    await limpiar_leads(escenario.client_id)


@pytest_asyncio.fixture
async def crm_dos(
    dos_tenants: tuple[Escenario, Escenario],
) -> AsyncGenerator[tuple[Escenario, Escenario], None]:
    for e in dos_tenants:
        await activar_modulo(e.client_id)
    yield dos_tenants
    for e in dos_tenants:
        await limpiar_leads(e.client_id)


async def _fuente_con_token(esc: Escenario, **campos: Any) -> tuple[uuid.UUID, str]:
    token, hash_ = generate_capture_token()
    campos.setdefault("name", "Formulario")
    campos.setdefault("source_type", "web_form")
    fuente = await crear_fuente(esc.client_id, capture_token_hash=hash_, **campos)
    return fuente, token


async def _leads(esc: Escenario) -> list[Any]:
    async with tenant_session(esc.client_id) as s:
        return list(
            (
                await s.execute(
                    text(
                        "SELECT id, source_id, pipeline_stage_id, enrichment_data, "
                        "last_activity_at, email::text AS email_crudo FROM leads "
                        "WHERE deleted_at IS NULL ORDER BY created_at"
                    )
                )
            ).all()
        )


FORM = {"first_name": "Ana", "email": "ana@example.com", "consent": True}


class TestAlta:
    async def test_crea_el_lead_en_new_con_fuente_consentimiento_y_utm(
        self, crm: Escenario, api: AsyncClient
    ) -> None:
        etapa = await crear_etapa(crm.client_id, "new", 0)
        fuente, token = await _fuente_con_token(
            crm, utm_tracking={"utm_source": "landing", "utm_campaign": "otono"}
        )
        r = await api.post(
            f"{CAPTURA}/{token}", json={**FORM, "utm_campaign": "primavera"}, headers=_ip()
        )
        assert r.status_code == 202, r.text
        assert r.json() == {"status": "received"}
        (lead,) = await _leads(crm)
        assert (lead.source_id, lead.pipeline_stage_id) == (fuente, etapa)
        captura = lead.enrichment_data["capture"]
        assert captura["consent"] is True
        # El UTM de la visita gana al de la fuente; el de la fuente rellena lo que falta.
        assert captura["utm"] == {"utm_source": "landing", "utm_campaign": "primavera"}
        assert "ana@example.com" not in lead.email_crudo  # cifrado en la base

    async def test_no_hace_falta_jwt(self, crm: Escenario, api: AsyncClient) -> None:
        _, token = await _fuente_con_token(crm)
        r = await api.post(f"{CAPTURA}/{token}", json=FORM, headers=_ip())
        assert r.status_code == 202  # sin cabecera Authorization

    async def test_sin_pipeline_el_lead_se_crea_sin_etapa_y_no_se_pierde(
        self, crm: Escenario, api: AsyncClient
    ) -> None:
        _, token = await _fuente_con_token(crm)
        assert (await api.post(f"{CAPTURA}/{token}", json=FORM, headers=_ip())).status_code == 202
        (lead,) = await _leads(crm)
        assert lead.pipeline_stage_id is None

    async def test_el_token_de_un_tenant_solo_crea_leads_en_ese_tenant(
        self, crm_dos: tuple[Escenario, Escenario], api: AsyncClient
    ) -> None:
        a, b = crm_dos
        _, token_a = await _fuente_con_token(a)
        await api.post(f"{CAPTURA}/{token_a}", json=FORM, headers=_ip())
        assert len(await _leads(a)) == 1
        assert await _leads(b) == []


class TestRespuestaUniforme:
    async def test_un_duplicado_responde_igual_y_solo_anota_la_actividad(
        self, crm: Escenario, api: AsyncClient
    ) -> None:
        _, token = await _fuente_con_token(crm)
        primera = await api.post(f"{CAPTURA}/{token}", json=FORM, headers=_ip())
        (antes,) = await _leads(crm)
        por_email = await api.post(
            f"{CAPTURA}/{token}", json={**FORM, "email": "ANA@Example.com"}, headers=_ip()
        )
        por_telefono = await api.post(
            f"{CAPTURA}/{token}",
            json={"phone": "+57 300 111-2233", "consent": True},
            headers=_ip(),
        )
        repetido_tel = await api.post(
            f"{CAPTURA}/{token}", json={"phone": "573001112233", "consent": True}, headers=_ip()
        )
        for r in (primera, por_email, por_telefono, repetido_tel):
            assert (r.status_code, r.json()) == (202, {"status": "received"})
        leads = await _leads(crm)
        assert len(leads) == 2  # el email y el telefono son dos personas distintas
        assert leads[0].last_activity_at >= antes.last_activity_at

    async def test_un_duplicado_de_un_lead_borrado_crea_uno_nuevo(
        self, crm: Escenario, api: AsyncClient
    ) -> None:
        from datetime import datetime, timezone

        _, token = await _fuente_con_token(crm)
        await crear_lead(
            crm.client_id, email="ana@example.com", deleted_at=datetime.now(timezone.utc)
        )
        await api.post(f"{CAPTURA}/{token}", json=FORM, headers=_ip())
        assert len(await _leads(crm)) == 1

    async def test_el_honeypot_relleno_responde_202_y_no_crea_nada(
        self, crm: Escenario, api: AsyncClient
    ) -> None:
        _, token = await _fuente_con_token(crm)
        r = await api.post(
            f"{CAPTURA}/{token}", json={**FORM, "website": "http://spam.example"}, headers=_ip()
        )
        assert (r.status_code, r.json()) == (202, {"status": "received"})
        assert await _leads(crm) == []

    async def test_toda_fuente_inutilizable_responde_el_mismo_404(
        self, crm: Escenario, api: AsyncClient
    ) -> None:
        _, apagada = await _fuente_con_token(crm, is_active=False)
        casos = {"desconocido": "token-que-no-existe", "apagada": apagada}
        respuestas = {}
        for nombre, token in casos.items():
            respuestas[nombre] = await api.post(f"{CAPTURA}/{token}", json=FORM, headers=_ip())
        # Tenant suspendido y modulo apagado, con fuentes validas.
        _, token_ok = await _fuente_con_token(crm)
        await activar_modulo(crm.client_id, False)
        respuestas["modulo_apagado"] = await api.post(
            f"{CAPTURA}/{token_ok}", json=FORM, headers=_ip()
        )
        await activar_modulo(crm.client_id, True)
        async with tenant_session(crm.client_id) as s:
            await s.execute(
                text("UPDATE clients SET is_active = false WHERE id = :c"),
                {"c": str(crm.client_id)},
            )
        respuestas["tenant_suspendido"] = await api.post(
            f"{CAPTURA}/{token_ok}", json=FORM, headers=_ip()
        )
        cuerpos = {r.text for r in respuestas.values()}
        assert {r.status_code for r in respuestas.values()} == {404}
        assert len(cuerpos) == 1, cuerpos  # indistinguibles
        async with tenant_session(crm.client_id) as s:
            await s.execute(
                text("UPDATE clients SET is_active = true WHERE id = :c"), {"c": str(crm.client_id)}
            )
        assert await _leads(crm) == []


class TestValidacion:
    async def test_consentimiento_obligatorio_salvo_que_la_fuente_lo_desactive(
        self, crm: Escenario, api: AsyncClient
    ) -> None:
        _, exigente = await _fuente_con_token(crm)
        _, laxa = await _fuente_con_token(crm, config={"require_consent": False})
        sin = {"email": "ana@example.com"}
        assert (await api.post(f"{CAPTURA}/{exigente}", json=sin, headers=_ip())).status_code == 422
        assert await _leads(crm) == []
        assert (await api.post(f"{CAPTURA}/{laxa}", json=sin, headers=_ip())).status_code == 202
        assert len(await _leads(crm)) == 1

    @pytest.mark.parametrize(
        "cuerpo",
        [
            {"consent": True},
            {"first_name": "Solo nombre", "consent": True},
            {"email": "no-es-email", "consent": True},
            {"email": "a@b.co", "phone": "abc", "consent": True},
            {"email": "a@b.co", "linkedin_url": "http://linkedin.com/in/a", "consent": True},
        ],
    )
    async def test_cuerpos_invalidos_son_422(
        self, crm: Escenario, api: AsyncClient, cuerpo: dict[str, Any]
    ) -> None:
        _, token = await _fuente_con_token(crm)
        assert (await api.post(f"{CAPTURA}/{token}", json=cuerpo, headers=_ip())).status_code == 422
        assert await _leads(crm) == []

    async def test_un_cuerpo_de_mas_de_16_kb_es_413(self, crm: Escenario, api: AsyncClient) -> None:
        _, token = await _fuente_con_token(crm)
        grande = '{"email": "a@b.co", "consent": true, "relleno": "' + "x" * 17_000 + '"}'
        r = await api.post(
            f"{CAPTURA}/{token}",
            content=grande,
            headers={**_ip(), "content-type": "application/json"},
        )
        assert r.status_code == 413
        assert await _leads(crm) == []

    async def test_los_scores_y_el_estado_enviados_se_ignoran(
        self, crm: Escenario, api: AsyncClient
    ) -> None:
        _, token = await _fuente_con_token(crm)
        await api.post(
            f"{CAPTURA}/{token}",
            json={**FORM, "total_score": 100, "status": "won", "client_id": str(uuid.uuid4())},
            headers=_ip(),
        )
        async with tenant_session(crm.client_id) as s:
            fila = (await s.execute(text("SELECT total_score, status FROM leads"))).one()
        assert (fila.total_score, fila.status) == (0, "active")


class TestLimites:
    async def test_el_limite_por_ip_corta_tambien_con_tokens_que_no_existen(
        self, crm: Escenario, api: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(get_settings(), "CAPTURE_MAX_PER_IP_PER_MINUTE", 3)
        ip = _ip()
        codigos = [
            (await api.post(f"{CAPTURA}/falso-{i}", json=FORM, headers=ip)).status_code
            for i in range(5)
        ]
        assert codigos == [404, 404, 404, 429, 429]

    async def test_una_ip_falsificada_a_la_izquierda_no_evade_el_limite(
        self, crm: Escenario, api: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(get_settings(), "CAPTURE_MAX_PER_IP_PER_MINUTE", 1)
        real = _ip()["x-forwarded-for"]
        uno = await api.post(
            f"{CAPTURA}/x", json=FORM, headers={"x-forwarded-for": f"1.1.1.1, {real}"}
        )
        dos = await api.post(
            f"{CAPTURA}/x", json=FORM, headers={"x-forwarded-for": f"2.2.2.2, {real}"}
        )
        assert (uno.status_code, dos.status_code) == (404, 429)

    async def test_el_limite_por_fuente_no_afecta_a_otra_fuente(
        self, crm: Escenario, api: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(get_settings(), "CAPTURE_MAX_PER_SOURCE_PER_MINUTE", 2)
        _, token_a = await _fuente_con_token(crm)
        _, token_b = await _fuente_con_token(crm)
        # IPs distintas: lo que se agota es la fuente, no la IP.
        a = [
            (await api.post(f"{CAPTURA}/{token_a}", json=FORM, headers=_ip())).status_code
            for _ in range(3)
        ]
        b = (await api.post(f"{CAPTURA}/{token_b}", json=FORM, headers=_ip())).status_code
        assert a == [202, 202, 429]
        assert b == 202

    async def test_si_redis_no_responde_falla_cerrado_con_503(
        self, crm: Escenario, api: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class RedisCaido:
            async def incr(self, *_a: Any) -> int:
                raise ConnectionError("redis caido")

        monkeypatch.setattr("app.core.rate_limit.get_redis", lambda: RedisCaido())
        _, token = await _fuente_con_token(crm)
        r = await api.post(f"{CAPTURA}/{token}", json=FORM, headers=_ip())
        assert r.status_code == 503
        assert await _leads(crm) == []


class TestCors:
    ORIGEN = "https://www.tu-cliente.com"

    async def test_el_preflight_de_cualquier_origen_se_acepta_sin_credenciales(
        self, api: AsyncClient
    ) -> None:
        r = await api.options(
            f"{CAPTURA}/cualquier-token",
            headers={
                "Origin": self.ORIGEN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert r.status_code == 204
        assert r.headers["access-control-allow-origin"] == "*"
        assert r.headers["access-control-allow-methods"] == "POST, OPTIONS"
        assert "access-control-allow-credentials" not in r.headers

    async def test_la_respuesta_lleva_el_comodin_tambien_en_los_errores(
        self, crm: Escenario, api: AsyncClient
    ) -> None:
        _, token = await _fuente_con_token(crm)
        ok = await api.post(
            f"{CAPTURA}/{token}", json=FORM, headers={**_ip(), "Origin": self.ORIGEN}
        )
        malo = await api.post(
            f"{CAPTURA}/falso", json=FORM, headers={**_ip(), "Origin": self.ORIGEN}
        )
        assert ok.headers["access-control-allow-origin"] == "*"
        assert malo.status_code == 404
        assert malo.headers["access-control-allow-origin"] == "*"
        assert "access-control-allow-credentials" not in ok.headers

    async def test_un_origen_permitido_conserva_su_cabecera_concreta(
        self, crm: Escenario, api: AsyncClient
    ) -> None:
        origen = get_settings().CORS_ORIGINS[0]
        _, token = await _fuente_con_token(crm)
        r = await api.post(f"{CAPTURA}/{token}", json=FORM, headers={**_ip(), "Origin": origen})
        assert r.headers["access-control-allow-origin"] == origen  # no se pisa con `*`

    async def test_el_resto_de_la_api_no_se_abre(self, api: AsyncClient) -> None:
        r = await api.options(
            "/api/v1/leads",
            headers={"Origin": self.ORIGEN, "Access-Control-Request-Method": "GET"},
        )
        assert r.status_code != 204  # no lo atiende la captura
        assert r.headers.get("access-control-allow-origin") != "*"
        g = await api.get("/api/v1/leads", headers={"Origin": self.ORIGEN})
        assert g.status_code == 401
        assert g.headers.get("access-control-allow-origin") != "*"
