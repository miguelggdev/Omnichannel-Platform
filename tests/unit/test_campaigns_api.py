"""Tests del CRUD de campanas de marketing (Sprint 12, Dev B).

Mismo patron que `test_webhooks_config.py`: la base se sustituye por
`CrmSession`, corren sin `--run-db`, y el encolado real de Celery se sustituye
por un doble.
"""

import uuid
from typing import Any, ClassVar

import pytest

from app.api.v1 import campaigns as campaigns_module
from app.models.campaign import CAMPAIGN_COMPLETED, CAMPAIGN_DRAFT, CAMPAIGN_SENDING
from tests.unit.agent_doubles import fake_tenant_session
from tests.unit.crm_doubles import AHORA, CrmSession

URL = "/api/v1/campaigns"


class FakeCampaign:
    """Sustituto de Campaign con lo que leen los endpoints y schemas."""

    def __init__(self, **kwargs: Any) -> None:
        """Construye la campana con valores por defecto razonables."""
        self.id = kwargs.get("id") or uuid.uuid4()
        self.client_id = kwargs.get("client_id") or uuid.uuid4()
        self.name = kwargs.get("name", "Promo de septiembre")
        self.channel = kwargs.get("channel", "telegram")
        self.segment_criteria = kwargs.get("segment_criteria", {"tags": ["vip"]})
        self.message_template = kwargs.get("message_template", "Hola {{contact_name}}")
        self.status = kwargs.get("status", CAMPAIGN_DRAFT)
        self.target_count = kwargs.get("target_count", 42)
        self.delivered_count = kwargs.get("delivered_count", 0)
        self.read_count = kwargs.get("read_count", 0)
        self.replied_count = kwargs.get("replied_count", 0)
        self.failed_count = kwargs.get("failed_count", 0)
        self.scheduled_for = kwargs.get("scheduled_for")
        self.started_at = kwargs.get("started_at")
        self.completed_at = kwargs.get("completed_at")
        self.error_log = kwargs.get("error_log", [])
        self.created_at = kwargs.get("created_at", AHORA)


def _usa_sesion(monkeypatch: pytest.MonkeyPatch, session: CrmSession) -> CrmSession:
    """Hace que los endpoints de campanas usen la sesion falsa."""
    monkeypatch.setattr(campaigns_module, "tenant_session", fake_tenant_session(session))
    return session


def _sin_encolar(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Sustituye `execute_campaign.delay()` y devuelve lo que se encolo."""
    encolados: list[tuple] = []

    class _Task:
        @staticmethod
        def delay(*args):
            encolados.append(args)

    import app.tasks.campaign_tasks as ct

    monkeypatch.setattr(ct, "execute_campaign", _Task)
    return encolados


# ─── GET /campaigns ──────────────────────────────────────────────────────────


class TestListado:
    async def test_devuelve_las_del_tenant(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _usa_sesion(monkeypatch, CrmSession(resultados=[[FakeCampaign()]], escalares=[1]))

        response = await authenticated_client.get(URL)

        assert response.status_code == 200
        cuerpo = response.json()
        assert cuerpo["total"] == 1
        assert cuerpo["items"][0]["name"] == "Promo de septiembre"

    async def test_el_client_id_va_explicito_en_el_where(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _usa_sesion(monkeypatch, CrmSession(resultados=[[]], escalares=[0]))

        await authenticated_client.get(URL)

        assert "campaigns.client_id =" in str(session.executed[0])

    async def test_filtra_por_estado(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _usa_sesion(monkeypatch, CrmSession(resultados=[[]], escalares=[0]))

        response = await authenticated_client.get(f"{URL}?status={CAMPAIGN_SENDING}")

        assert response.status_code == 200
        assert "campaigns.status =" in str(session.executed[0])

    async def test_estado_inexistente_da_400(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _usa_sesion(monkeypatch, CrmSession())

        response = await authenticated_client.get(f"{URL}?status=enviandose")

        assert response.status_code == 400
        assert "Estado invalido" in response.text


# ─── POST /campaigns ─────────────────────────────────────────────────────────


class TestAlta:
    _BODY: ClassVar[dict[str, Any]] = {
        "name": "Promo",
        "channel": "telegram",
        "segment_criteria": {"tags": ["vip"]},
        "message_template": "Hola {{contact_name}}",
    }

    async def test_crea_en_draft_y_cuenta_el_segmento(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _usa_sesion(monkeypatch, CrmSession())

        async def _contar(*_args, **_kwargs):
            return 17

        monkeypatch.setattr(campaigns_module, "contar_segmento", _contar)

        response = await authenticated_client.post(URL, json=self._BODY)

        assert response.status_code == 201
        cuerpo = response.json()
        assert cuerpo["status"] == CAMPAIGN_DRAFT
        assert cuerpo["target_count"] == 17
        assert session.added[0].name == "Promo"

    async def test_canal_invalido_da_400(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _usa_sesion(monkeypatch, CrmSession())

        response = await authenticated_client.post(URL, json={**self._BODY, "channel": "paloma"})

        assert response.status_code == 400
        assert "Canal invalido" in response.text

    async def test_criterio_desconocido_da_400_y_no_crea_nada(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un criterio que no se entiende no puede pasar en silencio: la
        campana saldria a mas gente de la que el tenant eligio."""
        session = _usa_sesion(monkeypatch, CrmSession())

        from app.services.segmentation import CriterioInvalidoError

        async def _contar(*_args, **_kwargs):
            raise CriterioInvalidoError("Criterios no soportados: sentiment_avg")

        monkeypatch.setattr(campaigns_module, "contar_segmento", _contar)

        response = await authenticated_client.post(
            URL, json={**self._BODY, "segment_criteria": {"sentiment_avg": 0.8}}
        )

        assert response.status_code == 400
        assert "sentiment_avg" in response.text
        assert session.added == []

    async def test_plantilla_vacia_la_rechaza_el_schema(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _usa_sesion(monkeypatch, CrmSession())

        response = await authenticated_client.post(URL, json={**self._BODY, "message_template": ""})

        assert response.status_code == 422


# ─── GET /campaigns/{id} ─────────────────────────────────────────────────────


class TestDetalle:
    async def test_devuelve_los_contadores(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        campana = FakeCampaign(delivered_count=30, failed_count=2, status=CAMPAIGN_COMPLETED)
        _usa_sesion(monkeypatch, CrmSession(resultados=[campana]))

        response = await authenticated_client.get(f"{URL}/{campana.id}")

        assert response.status_code == 200
        assert response.json()["delivered_count"] == 30
        assert response.json()["failed_count"] == 2

    async def test_inexistente_da_404(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _usa_sesion(monkeypatch, CrmSession(resultados=[None]))

        response = await authenticated_client.get(f"{URL}/{uuid.uuid4()}")

        assert response.status_code == 404


# ─── POST /campaigns/{id}/send ───────────────────────────────────────────────


class TestEnvio:
    async def test_encola_con_client_id_y_campaign_id(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La task es `(client_id, campaign_id)`; el spec encolaba solo el id."""
        campana = FakeCampaign(channel="telegram")
        _usa_sesion(monkeypatch, CrmSession(resultados=[campana, None]))
        encolados = _sin_encolar(monkeypatch)

        response = await authenticated_client.post(f"{URL}/{campana.id}/send")

        assert response.status_code == 202
        assert len(encolados) == 1
        assert len(encolados[0]) == 2, "la task recibe client_id y campaign_id"
        assert encolados[0][1] == str(campana.id)

    async def test_deja_la_campana_en_scheduled(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        campana = FakeCampaign(channel="telegram")
        _usa_sesion(monkeypatch, CrmSession(resultados=[campana, None]))
        _sin_encolar(monkeypatch)

        response = await authenticated_client.post(f"{URL}/{campana.id}/send")

        assert response.json()["status"] == "scheduled"
        assert campana.status == "scheduled"

    async def test_no_se_puede_reenviar_una_ya_enviada(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        campana = FakeCampaign(status=CAMPAIGN_COMPLETED)
        _usa_sesion(monkeypatch, CrmSession(resultados=[campana]))
        encolados = _sin_encolar(monkeypatch)

        response = await authenticated_client.post(f"{URL}/{campana.id}/send")

        assert response.status_code == 400
        assert encolados == []

    async def test_whatsapp_sin_plantilla_aprobada_no_sale(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mandar plantillas sin aprobar hace que Meta bloquee el numero."""
        campana = FakeCampaign(channel="whatsapp")
        _usa_sesion(monkeypatch, CrmSession(resultados=[campana]))
        encolados = _sin_encolar(monkeypatch)

        async def _no_aprobada(*_args, **_kwargs):
            return False

        monkeypatch.setattr(campaigns_module, "plantilla_aprobada", _no_aprobada)

        response = await authenticated_client.post(f"{URL}/{campana.id}/send")

        assert response.status_code == 400
        assert "aprobadas" in response.text
        assert encolados == []

    async def test_mismo_segmento_dentro_de_24h_no_sale(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        campana = FakeCampaign(channel="telegram")
        previa = FakeCampaign(name="La de ayer", status=CAMPAIGN_COMPLETED)
        _usa_sesion(monkeypatch, CrmSession(resultados=[campana]))
        encolados = _sin_encolar(monkeypatch)

        async def _duplicada(*_args, **_kwargs):
            return previa

        monkeypatch.setattr(campaigns_module, "campana_duplicada", _duplicada)

        response = await authenticated_client.post(f"{URL}/{campana.id}/send")

        assert response.status_code == 400
        assert "La de ayer" in response.text
        assert encolados == []

    async def test_inexistente_da_404(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _usa_sesion(monkeypatch, CrmSession(resultados=[None]))
        encolados = _sin_encolar(monkeypatch)

        response = await authenticated_client.post(f"{URL}/{uuid.uuid4()}/send")

        assert response.status_code == 404
        assert encolados == []


# ─── DELETE /campaigns/{id} ──────────────────────────────────────────────────


class TestBorrado:
    async def test_borra_un_draft(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        campana = FakeCampaign(status=CAMPAIGN_DRAFT)
        session = _usa_sesion(monkeypatch, CrmSession(resultados=[campana]))

        response = await authenticated_client.delete(f"{URL}/{campana.id}")

        assert response.status_code == 204
        assert session.deleted == [campana]

    async def test_no_borra_una_ya_enviada(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La campana enviada es el unico rastro de a quien se le mando que."""
        campana = FakeCampaign(status=CAMPAIGN_COMPLETED)
        session = _usa_sesion(monkeypatch, CrmSession(resultados=[campana]))

        response = await authenticated_client.delete(f"{URL}/{campana.id}")

        assert response.status_code == 400
        assert session.deleted == []

    async def test_inexistente_da_404(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _usa_sesion(monkeypatch, CrmSession(resultados=[None]))

        response = await authenticated_client.delete(f"{URL}/{uuid.uuid4()}")

        assert response.status_code == 404
