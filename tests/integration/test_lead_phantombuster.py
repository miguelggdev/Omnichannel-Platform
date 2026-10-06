# ruff: noqa: F811
"""Webhook de Phantombuster contra PostgreSQL y Redis reales (Sprint 16, ADR-084).

Requiere base de datos: `pytest tests/ --run-db`. Es publico (sin JWT), asi que se prueba lo que lo
acota: solo fuentes `linkedin`, respuesta uniforme ante lo inutilizable, que los reintentos de
Phantombuster no dupliquen, que un resultado con solo enlaces no siga ninguno y que los perfiles
queden marcados como recogidos sin consentimiento.
"""

import json
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

URL = "/api/v1/capture/{token}/phantombuster"
PERFILES = [
    {"profileUrl": "https://www.linkedin.com/in/ana/", "fullName": "Ana Gomez", "company": "ACME"},
    {"profileUrl": "https://www.linkedin.com/in/beto", "firstName": "Beto", "title": "CTO"},
    {"profileUrl": "https://www.linkedin.com/in/cata", "email": "cata@example.com"},
]


def _ip() -> dict[str, str]:
    n = uuid.uuid4().int
    return {"x-forwarded-for": f"10.{n % 250}.{(n >> 8) % 250}.{(n >> 16) % 250}"}


def _webhook(perfiles: Any = None, **extra: Any) -> dict[str, Any]:
    return {
        "agentId": "1",
        "containerId": "2",
        "exitCode": 0,
        "exitMessage": "finished",
        "resultObject": json.dumps(PERFILES if perfiles is None else perfiles),
        **extra,
    }


@pytest_asyncio.fixture
async def api() -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://t") as c:
        yield c


@pytest_asyncio.fixture
async def crm(escenario: Escenario) -> AsyncGenerator[Escenario, None]:
    await activar_modulo(escenario.client_id)
    await crear_etapa(escenario.client_id, "new", 0)
    yield escenario
    await limpiar_leads(escenario.client_id)


@pytest_asyncio.fixture
async def crm_dos(
    dos_tenants: tuple[Escenario, Escenario],
) -> AsyncGenerator[tuple[Escenario, Escenario], None]:
    for e in dos_tenants:
        await activar_modulo(e.client_id)
        await crear_etapa(e.client_id, "new", 0)
    yield dos_tenants
    for e in dos_tenants:
        await limpiar_leads(e.client_id)


async def _fuente(esc: Escenario, tipo: str = "linkedin", **campos: Any) -> tuple[uuid.UUID, str]:
    token, hash_ = generate_capture_token()
    fuente = await crear_fuente(
        esc.client_id, name="PB", source_type=tipo, capture_token_hash=hash_, **campos
    )
    return fuente, token


async def _leads(esc: Escenario) -> list[Any]:
    async with tenant_session(esc.client_id) as s:
        return list(
            (
                await s.execute(
                    text(
                        "SELECT first_name, last_name, linkedin_url, company_name, job_title, "
                        "enrichment_data, source_id, pipeline_stage_id FROM leads ORDER BY linkedin_url"
                    )
                )
            ).all()
        )


class TestWebhook:
    async def test_crea_un_lead_por_perfil_marcado_sin_consentimiento(
        self, crm: Escenario, api: AsyncClient
    ) -> None:
        fuente, token = await _fuente(crm, utm_tracking={"utm_source": "pb"})
        r = await api.post(URL.format(token=token), json=_webhook(), headers=_ip())
        assert r.status_code == 202, r.text
        assert r.json() == {
            "status": "received",
            "received": 3,
            "created": 3,
            "duplicates": 0,
            "invalid": 0,
        }
        ana, beto, cata = await _leads(crm)
        assert (ana.first_name, ana.last_name, ana.company_name) == ("Ana", "Gomez", "ACME")
        assert ana.linkedin_url == "https://www.linkedin.com/in/ana"  # canonica
        assert (beto.first_name, beto.job_title) == ("Beto", "CTO")
        assert ana.source_id == fuente
        assert ana.enrichment_data["capture"]["origin"] == "phantombuster"
        assert ana.enrichment_data["capture"]["consent"] is False
        assert ana.enrichment_data["capture"]["utm"] == {"utm_source": "pb"}
        assert cata.linkedin_url == "https://www.linkedin.com/in/cata"
        async with tenant_session(crm.client_id) as s:
            tipos = (
                await s.execute(text("SELECT DISTINCT activity_type, user_id FROM lead_activities"))
            ).all()
        assert [(t.activity_type, t.user_id) for t in tipos] == [("captured", None)]

    async def test_un_reintento_del_mismo_webhook_no_duplica_a_nadie(
        self, crm: Escenario, api: AsyncClient
    ) -> None:
        _, token = await _fuente(crm)
        await api.post(URL.format(token=token), json=_webhook(), headers=_ip())
        otra = await api.post(URL.format(token=token), json=_webhook(), headers=_ip())
        assert (otra.json()["created"], otra.json()["duplicates"]) == (0, 3)
        assert len(await _leads(crm)) == 3

    async def test_deduplica_contra_los_leads_existentes_por_linkedin_o_email(
        self, crm: Escenario, api: AsyncClient
    ) -> None:
        await crear_lead(crm.client_id, linkedin_url="https://www.linkedin.com/in/ana")
        await crear_lead(crm.client_id, email="cata@example.com")
        _, token = await _fuente(crm)
        r = await api.post(URL.format(token=token), json=_webhook(), headers=_ip())
        assert (r.json()["created"], r.json()["duplicates"]) == (1, 2)

    async def test_los_perfiles_invalidos_se_cuentan_y_los_buenos_entran(
        self, crm: Escenario, api: AsyncClient
    ) -> None:
        _, token = await _fuente(crm)
        perfiles = [
            *PERFILES[:1],
            {"fullName": "Sin contacto"},
            {"email": "no-es-un-email"},
        ]
        r = await api.post(URL.format(token=token), json=_webhook(perfiles), headers=_ip())
        assert (r.json()["created"], r.json()["invalid"]) == (1, 2)

    async def test_si_el_resultado_solo_trae_enlaces_no_se_sigue_ninguno(
        self, crm: Escenario, api: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import httpx

        original = httpx.AsyncClient.send

        async def vigilante(self: httpx.AsyncClient, request: httpx.Request, **kw: Any) -> Any:
            # El cliente de prueba habla con la app en el host "t"; cualquier otro destino seria
            # la plataforma siguiendo un enlace del payload.
            if request.url.host != "t":
                raise AssertionError(f"peticion de red a {request.url.host}")
            return await original(self, request, **kw)

        monkeypatch.setattr(httpx.AsyncClient, "send", vigilante)
        _, token = await _fuente(crm)
        enlaces = {
            "csvUrl": "http://169.254.169.254/latest/meta-data",
            "jsonUrl": "http://10.0.0.1/x",
        }
        r = await api.post(
            URL.format(token=token),
            json={**_webhook(), "resultObject": json.dumps(enlaces)},
            headers=_ip(),
        )
        assert r.status_code == 202
        assert r.json()["note"] == "links_only"
        assert r.json()["created"] == 0
        assert await _leads(crm) == []

    @pytest.mark.parametrize("extra", [{"exitCode": 1}, {"exitMessage": "killed"}])
    async def test_un_agente_que_fallo_no_importa(
        self, crm: Escenario, api: AsyncClient, extra: dict[str, Any]
    ) -> None:
        _, token = await _fuente(crm)
        r = await api.post(URL.format(token=token), json=_webhook(**extra), headers=_ip())
        assert (r.status_code, r.json()["created"]) == (202, 0)
        assert await _leads(crm) == []

    async def test_mas_de_500_perfiles_se_recortan_y_se_avisa(
        self, crm: Escenario, api: AsyncClient
    ) -> None:
        _, token = await _fuente(crm)
        muchos = [{"profileUrl": f"https://linkedin.com/in/p{i}"} for i in range(520)]
        r = await api.post(URL.format(token=token), json=_webhook(muchos), headers=_ip())
        assert (r.json()["received"], r.json()["created"], r.json()["note"]) == (
            520,
            500,
            "truncated",
        )


class TestAcceso:
    async def test_solo_las_fuentes_linkedin_y_todo_lo_demas_es_el_mismo_404(
        self, crm: Escenario, api: AsyncClient
    ) -> None:
        _, otra = await _fuente(crm, tipo="web_form")
        _, apagada = await _fuente(crm, is_active=False)
        casos = [otra, apagada, "token-inexistente"]
        respuestas = [
            await api.post(URL.format(token=t), json=_webhook(), headers=_ip()) for t in casos
        ]
        assert {r.status_code for r in respuestas} == {404}
        assert len({r.text for r in respuestas}) == 1
        assert await _leads(crm) == []

    async def test_un_cuerpo_que_no_es_json_es_422_y_uno_enorme_413(
        self, crm: Escenario, api: AsyncClient
    ) -> None:
        _, token = await _fuente(crm)
        malo = await api.post(
            URL.format(token=token),
            content="esto no es json",
            headers={**_ip(), "content-type": "application/json"},
        )
        enorme = await api.post(
            URL.format(token=token),
            content='{"relleno": "' + "x" * (1024 * 1024 + 10) + '"}',
            headers={**_ip(), "content-type": "application/json"},
        )
        assert (malo.status_code, enorme.status_code) == (422, 413)

    async def test_los_limites_por_ip_siguen_aplicando(
        self, crm: Escenario, api: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(get_settings(), "CAPTURE_MAX_PER_IP_PER_MINUTE", 2)
        ip = _ip()
        codigos = [
            (await api.post(URL.format(token=f"x{i}"), json=_webhook(), headers=ip)).status_code
            for i in range(3)
        ]
        assert codigos == [404, 404, 429]

    async def test_el_token_de_un_tenant_solo_crea_leads_en_ese_tenant(
        self, crm_dos: tuple[Escenario, Escenario], api: AsyncClient
    ) -> None:
        a, b = crm_dos
        _, token_a = await _fuente(a)
        await api.post(URL.format(token=token_a), json=_webhook(), headers=_ip())
        assert len(await _leads(a)) == 3
        assert await _leads(b) == []
