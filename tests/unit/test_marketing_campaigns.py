"""Tests de segmentacion, tools de marketing, envio masivo y los dos nodos nuevos (Sprint 12)."""

import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-testing-only-minimum-32-chars")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key-minimum-32-characters-long")

from app.agents.graph import NODE_FINANCIAL, NODE_MARKETING, route_after_intent
from app.agents.nodes import financial as financial_node_mod
from app.agents.nodes import marketing as marketing_node_mod
from app.agents.nodes.intent_router import available_intents
from app.agents.tools import marketing_tools as mt
from app.agents.tools.marketing_tools import MARKETING_TOOLS
from app.models.campaign import (
    CAMPAIGN_COMPLETED,
    CAMPAIGN_DRAFT,
    CAMPAIGN_SCHEDULED,
    CAMPAIGN_SENDING,
)
from app.services.segmentation import (
    CriterioInvalidoError,
    construir_query,
)
from app.tasks import campaign_tasks as ct
from app.tasks.campaign_tasks import resolver_plantilla
from app.tasks.celery_app import TASK_MODULES
from app.tasks.celery_config import celery_app

CLIENT_ID = str(uuid4())
CONFIG = {"configurable": {"client_id": CLIENT_ID, "contact_id": None, "conversation_id": None}}


class _Resultado:
    def __init__(self, filas: list) -> None:
        self._filas = filas

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._filas))

    def scalar_one(self):
        return self._filas[0]

    def scalar_one_or_none(self):
        return self._filas[0] if self._filas else None


class _SesionFalsa:
    def __init__(self, resultados: list[_Resultado] | None = None) -> None:
        self._resultados = list(resultados or [])
        self.added: list = []
        self.ejecutadas: list = []

    async def execute(self, stmt=None, params=None):
        self.ejecutadas.append(stmt)
        if self._resultados:
            return self._resultados.pop(0)
        return _Resultado([])

    def add(self, obj) -> None:
        self.added.append(obj)

    def expunge_all(self) -> None:
        return None


def _sesion(sesion: _SesionFalsa):
    @asynccontextmanager
    async def _cm(client_id, user_id=None):
        yield sesion

    return _cm


def _contacto(**over):
    base = {
        "id": uuid4(),
        "first_name": "Ana",
        "last_name": "Gomez",
        "display_name": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _campana(**over):
    base = {
        "id": uuid4(),
        "name": "Promo septiembre",
        "channel": "whatsapp",
        "segment_criteria": {"tags": ["vip"]},
        "message_template": "Hola {{contact_name}}",
        "status": CAMPAIGN_DRAFT,
        "target_count": 3,
        "delivered_count": 0,
        "failed_count": 0,
        "read_count": 0,
        "replied_count": 0,
        "started_at": None,
        "error_log": [],
    }
    base.update(over)
    return SimpleNamespace(**base)


class TestSegmentacion:
    def test_un_criterio_desconocido_no_se_ignora(self) -> None:
        """Ignorarlo mandaria la campana a mas gente de la que el tenant eligio."""
        with pytest.raises(CriterioInvalidoError, match="sentiment_avg"):
            construir_query(uuid4(), {"tags": ["vip"], "sentiment_avg": 0.5})

    @pytest.mark.parametrize(
        ("criterio", "valor"),
        [("tags", "vip"), ("metadata", ["a"]), ("last_active_days", "treinta")],
    )
    def test_rechaza_tipos_que_no_corresponden(self, criterio: str, valor: object) -> None:
        with pytest.raises(CriterioInvalidoError):
            construir_query(uuid4(), {criterio: valor})

    def test_las_etiquetas_se_exigen_todas(self) -> None:
        """Con un IN saldria la union de ambas etiquetas, que es lo contrario."""
        sql = str(construir_query(uuid4(), {"tags": ["vip", "mayorista"]}).compile())

        assert sql.count("EXISTS") == 2

    def test_nunca_entran_fusionados_ni_borrados_por_rgpd(self) -> None:
        sql = " ".join(str(construir_query(uuid4(), {}).compile()).split())

        assert "contacts.merged_into_id IS NULL" in sql
        assert "contacts.is_gdpr_deleted IS false" in sql

    def test_el_score_solo_se_castea_si_es_numerico(self) -> None:
        """`metadata` la edita el tenant: un score de texto tumbaria la consulta."""
        sql = " ".join(str(construir_query(uuid4(), {"score_min": 70}).compile()).split())

        assert "CASE WHEN" in sql
        assert "AS NUMERIC" in sql


class TestToolsDeMarketing:
    def test_el_llm_no_ve_el_client_id(self) -> None:
        for herramienta in MARKETING_TOOLS:
            assert "config" not in herramienta.args
            assert "client_id" not in herramienta.args

    @pytest.mark.asyncio
    async def test_plantilla_de_whatsapp_no_aprobada_se_rechaza(self, monkeypatch) -> None:
        """Criterio 7: Meta bloquea el numero del tenant si se manda sin aprobar."""
        monkeypatch.setattr(mt, "_plantillas_aprobadas", AsyncMock(return_value=["promo_aprobada"]))
        sesion = _SesionFalsa()
        monkeypatch.setattr(mt, "tenant_session", _sesion(sesion))

        respuesta = await mt.create_campaign.ainvoke(
            {
                "name": "Promo",
                "segment_criteria": {"tags": ["vip"]},
                "message_template": "texto libre sin aprobar",
            },
            config=CONFIG,
        )

        assert "no esta aprobado" in respuesta
        assert sesion.added == []

    @pytest.mark.asyncio
    async def test_plantilla_aprobada_crea_la_campana(self, monkeypatch) -> None:
        monkeypatch.setattr(mt, "_plantillas_aprobadas", AsyncMock(return_value=["promo_ok"]))
        monkeypatch.setattr(mt, "contar_segmento", AsyncMock(return_value=42))
        sesion = _SesionFalsa()
        monkeypatch.setattr(mt, "tenant_session", _sesion(sesion))

        respuesta = await mt.create_campaign.ainvoke(
            {
                "name": "Promo",
                "segment_criteria": {"tags": ["vip"]},
                "message_template": "promo_ok",
            },
            config=CONFIG,
        )

        campana = sesion.added[0]
        assert campana.status == CAMPAIGN_DRAFT
        assert campana.target_count == 42
        assert "42 contacto(s)" in respuesta

    @pytest.mark.asyncio
    async def test_una_campana_programada_queda_en_scheduled(self, monkeypatch) -> None:
        monkeypatch.setattr(mt, "contar_segmento", AsyncMock(return_value=5))
        sesion = _SesionFalsa()
        monkeypatch.setattr(mt, "tenant_session", _sesion(sesion))

        await mt.create_campaign.ainvoke(
            {
                "name": "Promo",
                "segment_criteria": {},
                "message_template": "Hola",
                "channel": "telegram",
                "schedule": "2026-10-01T09:00:00Z",
            },
            config=CONFIG,
        )

        campana = sesion.added[0]
        assert campana.status == CAMPAIGN_SCHEDULED
        assert campana.scheduled_for.year == 2026

    @pytest.mark.asyncio
    async def test_canal_invalido(self, monkeypatch) -> None:
        monkeypatch.setattr(mt, "tenant_session", _sesion(_SesionFalsa()))

        respuesta = await mt.create_campaign.ainvoke(
            {
                "name": "Promo",
                "segment_criteria": {},
                "message_template": "Hola",
                "channel": "paloma_mensajera",
            },
            config=CONFIG,
        )

        assert "Canal invalido" in respuesta

    @pytest.mark.asyncio
    async def test_un_criterio_invalido_no_crea_la_campana(self, monkeypatch) -> None:
        monkeypatch.setattr(
            mt, "contar_segmento", AsyncMock(side_effect=CriterioInvalidoError("sentiment_avg"))
        )
        sesion = _SesionFalsa()
        monkeypatch.setattr(mt, "tenant_session", _sesion(sesion))

        respuesta = await mt.create_campaign.ainvoke(
            {
                "name": "Promo",
                "segment_criteria": {"sentiment_avg": 1},
                "message_template": "Hola",
                "channel": "telegram",
            },
            config=CONFIG,
        )

        assert "No se creo la campana" in respuesta

    @pytest.mark.asyncio
    async def test_enviar_encola_la_task_en_bulk(self, monkeypatch) -> None:
        campana = _campana()
        sesion = _SesionFalsa([_Resultado([campana]), _Resultado([])])
        monkeypatch.setattr(mt, "tenant_session", _sesion(sesion))

        with patch("app.tasks.campaign_tasks.execute_campaign.delay") as mock_delay:
            respuesta = await mt.send_campaign.ainvoke(
                {"campaign_id": str(campana.id)}, config=CONFIG
            )

        mock_delay.assert_called_once_with(CLIENT_ID, str(campana.id))
        assert "encolada" in respuesta

    @pytest.mark.asyncio
    async def test_no_repite_la_misma_campana_en_24_horas(self, monkeypatch) -> None:
        campana = _campana()
        anterior = _campana(name="Promo de ayer", status=CAMPAIGN_COMPLETED)
        sesion = _SesionFalsa([_Resultado([campana]), _Resultado([anterior])])
        monkeypatch.setattr(mt, "tenant_session", _sesion(sesion))

        with patch("app.tasks.campaign_tasks.execute_campaign.delay") as mock_delay:
            respuesta = await mt.send_campaign.ainvoke(
                {"campaign_id": str(campana.id)}, config=CONFIG
            )

        mock_delay.assert_not_called()
        assert "ultimas 24 horas" in respuesta

    @pytest.mark.asyncio
    async def test_una_campana_ya_enviada_no_se_relanza(self, monkeypatch) -> None:
        campana = _campana(status=CAMPAIGN_COMPLETED)
        sesion = _SesionFalsa([_Resultado([campana])])
        monkeypatch.setattr(mt, "tenant_session", _sesion(sesion))

        with patch("app.tasks.campaign_tasks.execute_campaign.delay") as mock_delay:
            respuesta = await mt.send_campaign.ainvoke(
                {"campaign_id": str(campana.id)}, config=CONFIG
            )

        mock_delay.assert_not_called()
        assert "ya no se puede lanzar" in respuesta

    @pytest.mark.asyncio
    async def test_metricas_de_la_campana(self, monkeypatch) -> None:
        campana = _campana(status=CAMPAIGN_SENDING, delivered_count=10, failed_count=2)
        monkeypatch.setattr(mt, "tenant_session", _sesion(_SesionFalsa([_Resultado([campana])])))

        respuesta = await mt.get_campaign_metrics.ainvoke(
            {"campaign_id": str(campana.id)}, config=CONFIG
        )

        assert "enviados 10" in respuesta
        assert "fallidos 2" in respuesta


class TestPlantilla:
    def test_resuelve_las_variables(self) -> None:
        texto = resolver_plantilla("Hola {{contact_name}}, soy {{first_name}}", _contacto())

        assert texto == "Hola Ana Gomez, soy Ana"

    def test_una_variable_sin_dato_no_deja_el_placeholder(self) -> None:
        texto = resolver_plantilla(
            "Hola {{contact_name}}", _contacto(first_name=None, last_name=None)
        )

        assert texto == "Hola "


class TestEnvioMasivo:
    @pytest.mark.asyncio
    async def test_respeta_el_limite_de_mensajes_por_segundo(self, monkeypatch) -> None:
        """Criterio 6: con 100/s, 200 mensajes esperan un segundo entre lotes."""
        campana = _campana(channel="telegram")
        contactos = [_contacto() for _ in range(200)]
        esperas: list[float] = []

        monkeypatch.setattr(ct, "_marcar_en_envio", AsyncMock(return_value=campana))
        monkeypatch.setattr(ct, "_guardar_progreso", AsyncMock())
        monkeypatch.setattr(ct, "tenant_session", _sesion(_SesionFalsa()))
        monkeypatch.setattr(ct, "resolver_segmento", AsyncMock(return_value=contactos))
        monkeypatch.setattr(ct, "get_channel_config", lambda canal: ("telegram", {}))
        monkeypatch.setattr(ct, "get_messaging_provider", lambda nombre, conf: object())
        monkeypatch.setattr(ct, "_enviar_a_contacto", AsyncMock())
        monkeypatch.setattr(ct.asyncio, "sleep", AsyncMock(side_effect=lambda s: esperas.append(s)))
        monkeypatch.setattr(
            ct, "get_settings", lambda: SimpleNamespace(CAMPAIGN_MAX_MESSAGES_PER_SECOND=100)
        )

        resultado = await ct._ejecutar(CLIENT_ID, str(campana.id))

        assert resultado == {"status": CAMPAIGN_COMPLETED, "delivered": 200, "failed": 0}
        # Dos lotes: una sola espera entre ellos. Dormir despues del ultimo
        # solo retrasaria el final de la campana sin frenar nada.
        assert len(esperas) == 1
        assert 0 < esperas[0] <= 1.0

    @pytest.mark.asyncio
    async def test_un_contacto_que_falla_no_detiene_la_campana(self, monkeypatch) -> None:
        campana = _campana(channel="telegram")
        contactos = [_contacto() for _ in range(3)]
        progreso: list[dict] = []

        async def _enviar(client_id, contacto, camp, provider, config):
            if contacto is contactos[1]:
                raise RuntimeError("sin identificador")

        monkeypatch.setattr(ct, "_marcar_en_envio", AsyncMock(return_value=campana))
        monkeypatch.setattr(
            ct,
            "_guardar_progreso",
            AsyncMock(side_effect=lambda client_id, campaign_id, **kw: progreso.append(kw)),
        )
        monkeypatch.setattr(ct, "tenant_session", _sesion(_SesionFalsa()))
        monkeypatch.setattr(ct, "resolver_segmento", AsyncMock(return_value=contactos))
        monkeypatch.setattr(ct, "get_channel_config", lambda canal: ("telegram", {}))
        monkeypatch.setattr(ct, "get_messaging_provider", lambda nombre, conf: object())
        monkeypatch.setattr(ct, "_enviar_a_contacto", _enviar)

        resultado = await ct._ejecutar(CLIENT_ID, str(campana.id))

        assert resultado == {"status": CAMPAIGN_COMPLETED, "delivered": 2, "failed": 1}
        assert progreso[-1]["error_log"][0]["error"] == "sin identificador"

    @pytest.mark.asyncio
    async def test_una_campana_ya_en_envio_no_se_reenvia(self, monkeypatch) -> None:
        """La reentrega del broker no puede mandar la campana dos veces."""
        monkeypatch.setattr(ct, "_marcar_en_envio", AsyncMock(return_value=None))
        enviados = AsyncMock()
        monkeypatch.setattr(ct, "_enviar_a_contacto", enviados)

        resultado = await ct._ejecutar(CLIENT_ID, str(uuid4()))

        assert resultado == {"status": "skipped"}
        enviados.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_un_canal_sin_credenciales_marca_la_campana_fallida(self, monkeypatch) -> None:
        campana = _campana(channel="telegram")
        progreso: list[dict] = []

        monkeypatch.setattr(ct, "_marcar_en_envio", AsyncMock(return_value=campana))
        monkeypatch.setattr(
            ct,
            "_guardar_progreso",
            AsyncMock(side_effect=lambda client_id, campaign_id, **kw: progreso.append(kw)),
        )
        monkeypatch.setattr(ct, "tenant_session", _sesion(_SesionFalsa()))
        monkeypatch.setattr(ct, "resolver_segmento", AsyncMock(return_value=[_contacto()]))

        def _sin_canal(canal):
            raise ct.ChannelNotConfiguredError("telegram sin token")

        monkeypatch.setattr(ct, "get_channel_config", _sin_canal)

        resultado = await ct._ejecutar(CLIENT_ID, str(campana.id))

        assert resultado["status"] == "failed"
        assert progreso[-1]["status"] == "failed"

    def test_la_task_va_en_la_cola_bulk(self) -> None:
        tarea = celery_app.tasks["app.tasks.bulk_execute_campaign"]

        assert tarea.queue == "bulk"

    def test_el_modulo_esta_en_task_modules(self) -> None:
        """Sin esto, el worker de bulk arranca sin conocer la tarea (BUG-014)."""
        assert "app.tasks.campaign_tasks" in TASK_MODULES


class TestNodosNuevos:
    @pytest.mark.asyncio
    async def test_financiero_deshabilitado_responde_sin_llamar_al_llm(self, monkeypatch) -> None:
        """Criterio 4: sin el agente habilitado, no se gasta una llamada al modelo."""
        monkeypatch.setattr(
            financial_node_mod,
            "get_agent_settings",
            AsyncMock(return_value=SimpleNamespace(enabled_agents=("rag",), model="gpt-4o")),
        )
        con_tools = AsyncMock()
        monkeypatch.setattr(financial_node_mod, "responder_con_tools", con_tools)

        resultado = await financial_node_mod.financial_node(
            {"client_id": CLIENT_ID, "message": {"text": "quiero una factura"}}
        )

        assert resultado["response_text"] == financial_node_mod.MENSAJE_NO_HABILITADO
        assert resultado["intent"] == "financial"
        con_tools.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_financiero_habilitado_usa_sus_tools(self, monkeypatch) -> None:
        monkeypatch.setattr(
            financial_node_mod,
            "get_agent_settings",
            AsyncMock(
                return_value=SimpleNamespace(enabled_agents=("rag", "financial"), model="gpt-4o")
            ),
        )
        monkeypatch.setattr(
            financial_node_mod, "construir_contexto", AsyncMock(return_value="Contacto: Ana.")
        )
        con_tools = AsyncMock(return_value="Factura FE-000001 emitida.")
        monkeypatch.setattr(financial_node_mod, "responder_con_tools", con_tools)

        resultado = await financial_node_mod.financial_node(
            {"client_id": CLIENT_ID, "message": {"text": "quiero una factura"}}
        )

        assert resultado["response_text"] == "Factura FE-000001 emitida."
        kwargs = con_tools.await_args.kwargs
        assert kwargs["operacion"] == "financial"
        assert kwargs["temperatura"] == 0.0
        assert "Contacto: Ana." in kwargs["system_prompt"]

    @pytest.mark.asyncio
    async def test_marketing_deshabilitado_responde_sin_llamar_al_llm(self, monkeypatch) -> None:
        monkeypatch.setattr(
            marketing_node_mod,
            "get_agent_settings",
            AsyncMock(return_value=SimpleNamespace(enabled_agents=("rag",), model="gpt-4o")),
        )
        con_tools = AsyncMock()
        monkeypatch.setattr(marketing_node_mod, "responder_con_tools", con_tools)

        resultado = await marketing_node_mod.marketing_node(
            {"client_id": CLIENT_ID, "message": {"text": "crear campana"}}
        )

        assert resultado["response_text"] == marketing_node_mod.MENSAJE_NO_HABILITADO
        con_tools.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_marketing_habilitado_usa_sus_tools(self, monkeypatch) -> None:
        monkeypatch.setattr(
            marketing_node_mod,
            "get_agent_settings",
            AsyncMock(return_value=SimpleNamespace(enabled_agents=("marketing",), model="gpt-4o")),
        )
        con_tools = AsyncMock(return_value="Campana creada.")
        monkeypatch.setattr(marketing_node_mod, "responder_con_tools", con_tools)

        resultado = await marketing_node_mod.marketing_node(
            {"client_id": CLIENT_ID, "message": {"text": "crear campana"}}
        )

        assert resultado["response_text"] == "Campana creada."
        assert con_tools.await_args.kwargs["operacion"] == "marketing"


class TestRoutingDeLosNuevosIntents:
    def test_solo_se_ofrecen_si_el_tenant_habilito_el_agente(self) -> None:
        sin_agentes = available_intents(["rag"])
        con_agentes = available_intents(["rag", "financial", "marketing"])

        assert "financial" not in sin_agentes
        assert "marketing" not in sin_agentes
        assert {"financial", "marketing"} <= set(con_agentes)

    @pytest.mark.parametrize(
        ("intent", "destino"), [("financial", NODE_FINANCIAL), ("marketing", NODE_MARKETING)]
    )
    def test_el_grafo_enruta_al_nodo_del_agente(self, intent: str, destino: str) -> None:
        """Criterio 1: "necesito una factura" -> intent financial -> financial_agent."""
        assert route_after_intent({"intent": intent}) == destino

    def test_los_intents_viejos_siguen_igual(self) -> None:
        assert route_after_intent({"intent": "scheduling"}) == "scheduling"
        assert route_after_intent({"intent": "greeting"}) == "respond"
        assert route_after_intent({"intent": "unknown"}) == "rag_query"


class TestContextoFinanciero:
    @pytest.mark.asyncio
    async def test_incluye_el_nit_registrado_del_contacto(self, monkeypatch) -> None:
        contacto = SimpleNamespace(
            display_name="Ana Gomez",
            first_name="Ana",
            last_name="Gomez",
            metadata_={"nit": "890903938", "razon_social": "ACME SAS"},
        )
        monkeypatch.setattr(
            financial_node_mod, "tenant_session", _sesion(_SesionFalsa([_Resultado([contacto])]))
        )

        contexto = await financial_node_mod.construir_contexto(uuid4(), str(uuid4()))

        assert "Ana Gomez" in contexto
        assert "890903938" in contexto
        assert "ACME SAS" in contexto

    @pytest.mark.asyncio
    async def test_sin_contacto_no_consulta_la_base(self, monkeypatch) -> None:
        sesion = _SesionFalsa()
        monkeypatch.setattr(financial_node_mod, "tenant_session", _sesion(sesion))

        contexto = await financial_node_mod.construir_contexto(uuid4(), None)

        assert "Sin contacto identificado" in contexto
        assert sesion.ejecutadas == []
