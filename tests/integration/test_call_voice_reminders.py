# ruff: noqa: F811
"""Sprint 19, Dev B: voz IA de las llamadas agendadas y recordatorios.

Requiere base de datos y Redis: `pytest tests/ --run-db`. Lo que un doble no prueba: que dos
peticiones a la vez no inicien dos llamadas, que el enlace con `call_records` ocurra de verdad al
guardar la llamada, y que dos workers no manden el mismo recordatorio.
"""

import asyncio
import uuid
from collections.abc import AsyncGenerator, Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import pytest_asyncio

from app.core.config import get_settings
from app.services.call_notifications import procesar_recordatorios
from app.services.dedup import get_redis
from app.tasks import call_tasks
from app.tasks.voice_tasks import guardar_llamada
from tests.integration.call_helpers import (
    VozFalsa,
    crear_llamada_db,
    instalar_voz_falsa,
    quitar_voz_falsa,
)
from tests.integration.lead_helpers import activar_modulo, crear_lead, limpiar_leads
from tests.integration.sequence_helpers import (
    ProveedorFalso,
    instalar_proveedor_falso,
    sql,
    uno,
)
from tests.integration.test_crm_api import Escenario, _cliente, dos_tenants, escenario  # noqa: F401

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

CALLS = "/api/v1/scheduled-calls"
CONFIG = "/api/v1/clients/lead-automation"


@pytest_asyncio.fixture
async def crm(escenario: Escenario) -> AsyncGenerator[Escenario, None]:
    await activar_modulo(escenario.client_id)
    yield escenario
    await limpiar_leads(escenario.client_id)
    await sql(
        escenario.client_id,
        "DELETE FROM call_records WHERE client_id = :c",
        c=str(escenario.client_id),
    )


@pytest.fixture
def voz() -> Iterator[type[VozFalsa]]:
    yield instalar_voz_falsa()
    quitar_voz_falsa()


@pytest.fixture
def correo(monkeypatch: pytest.MonkeyPatch) -> ProveedorFalso:
    return instalar_proveedor_falso(monkeypatch)


@pytest.fixture
def con_voz_del_tenant(escenario: Escenario, monkeypatch: pytest.MonkeyPatch) -> None:
    """El canal de voz es de un solo tenant (`DEFAULT_CLIENT_ID`): este."""
    monkeypatch.setattr(get_settings(), "DEFAULT_CLIENT_ID", str(escenario.client_id))


def _en(horas: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=horas)


async def _lead(crm: Escenario, **campos: Any) -> uuid.UUID:
    return await crear_lead(
        crm.client_id,
        **{
            "first_name": "Ana",
            "email": f"{uuid.uuid4().hex[:8]}@x.com",
            "phone": "+57300" + str(uuid.uuid4().int)[:7],
            **campos,
        },
    )


async def _ia(crm: Escenario, lead: uuid.UUID, **campos: Any) -> uuid.UUID:
    return await crear_llamada_db(
        crm.client_id,
        lead,
        call_type="ai_voice",
        ai_voice_provider="fakevoice",
        scheduled_at=campos.pop("scheduled_at", _en(1)),
        **campos,
    )


async def _llamada(crm: Escenario, call_id: uuid.UUID) -> Any:
    return await uno(
        crm.client_id,
        "SELECT status, outcome, call_record_id, ai_voice_config FROM scheduled_calls WHERE id = :i",
        i=str(call_id),
    )


# ── Iniciar ──────────────────────────────────────────────────────────────────────────────────


class TestIniciar:
    async def test_inicia_y_deja_la_llamada_en_curso(
        self, crm: Escenario, voz: type[VozFalsa]
    ) -> None:
        lead = await _lead(crm, phone="+57 300 111-2233")
        call = await _ia(crm, lead, ai_voice_config={"voz": "nova"})
        async with _cliente(crm, "supervisor") as c:
            r = await c.post(f"{CALLS}/{call}/initiate")

        assert r.status_code == 202, r.text
        assert r.json()["status"] == "in_progress"
        peticion = voz.llamadas[0]
        assert peticion.to_phone == "+573001112233"
        assert peticion.scheduled_call_id == call
        assert peticion.config == {"voz": "nova"}  # sin la clave del servidor
        estado, _, _, config = await _llamada(crm, call)
        assert estado == "in_progress"
        assert config["runtime"]["external_id"] == "CA0001"
        assert config["voz"] == "nova"

    async def test_no_se_inicia_dos_veces(self, crm: Escenario, voz: type[VozFalsa]) -> None:
        call = await _ia(crm, await _lead(crm))
        async with _cliente(crm, "admin") as c:
            primera = await c.post(f"{CALLS}/{call}/initiate")
            segunda = await c.post(f"{CALLS}/{call}/initiate")
        assert (primera.status_code, segunda.status_code) == (202, 409)
        assert len(voz.llamadas) == 1

    async def test_dos_peticiones_a_la_vez_inician_una_sola_llamada(
        self, crm: Escenario, voz: type[VozFalsa]
    ) -> None:
        voz.modo = "lento"
        call = await _ia(crm, await _lead(crm))

        async def intentar() -> int:
            async with _cliente(crm, "admin") as c:
                return (await c.post(f"{CALLS}/{call}/initiate")).status_code

        estados = sorted(await asyncio.gather(intentar(), intentar(), intentar()))

        assert estados == [202, 409, 409]
        assert len(voz.llamadas) == 1

    async def test_una_llamada_humana_no_se_inicia_con_voz_ia(self, crm: Escenario) -> None:
        call = await crear_llamada_db(
            crm.client_id, await _lead(crm), assigned_user_id=crm.user_id, scheduled_at=_en(1)
        )
        async with _cliente(crm, "admin") as c:
            r = await c.post(f"{CALLS}/{call}/initiate")
        assert r.status_code == 409

    async def test_solo_pendiente_o_confirmada(self, crm: Escenario, voz: type[VozFalsa]) -> None:
        call = await _ia(
            crm, await _lead(crm), status="cancelled", cancelled_at=datetime.now(timezone.utc)
        )
        async with _cliente(crm, "admin") as c:
            r = await c.post(f"{CALLS}/{call}/initiate")
        assert r.status_code == 409
        assert voz.llamadas == []

    async def test_confirmada_tambien_se_inicia(self, crm: Escenario, voz: type[VozFalsa]) -> None:
        call = await _ia(
            crm, await _lead(crm), status="confirmed", confirmed_at=datetime.now(timezone.utc)
        )
        async with _cliente(crm, "admin") as c:
            r = await c.post(f"{CALLS}/{call}/initiate")
        assert r.status_code == 202

    async def test_sin_telefono_internacional_es_400(
        self, crm: Escenario, voz: type[VozFalsa]
    ) -> None:
        call = await _ia(crm, await _lead(crm, phone="0123456789"))
        async with _cliente(crm, "admin") as c:
            r = await c.post(f"{CALLS}/{call}/initiate")
        assert r.status_code == 400
        assert voz.llamadas == []

    async def test_no_se_llama_a_quien_pidio_no_recibir_contacto(
        self, crm: Escenario, voz: type[VozFalsa]
    ) -> None:
        lead = await _lead(crm)
        await sql(
            crm.client_id,
            "INSERT INTO lead_activities (client_id, lead_id, activity_type, metadata) "
            "VALUES (:c, :l, 'sequence_reply', CAST('{\"reply_kind\": \"unsubscribe\"}' AS jsonb))",
            c=str(crm.client_id),
            l=str(lead),
        )
        call = await _ia(crm, lead)
        async with _cliente(crm, "admin") as c:
            r = await c.post(f"{CALLS}/{call}/initiate")
        assert r.status_code == 409
        assert voz.llamadas == []

    async def test_un_lead_borrado_no_se_llama(self, crm: Escenario, voz: type[VozFalsa]) -> None:
        lead = await _lead(crm)
        call = await _ia(crm, lead)
        await sql(crm.client_id, "UPDATE leads SET deleted_at = now() WHERE id = :l", l=str(lead))
        async with _cliente(crm, "admin") as c:
            r = await c.post(f"{CALLS}/{call}/initiate")
        assert r.status_code == 409
        assert voz.llamadas == []

    async def test_un_fallo_pasajero_deja_la_llamada_como_estaba_y_se_puede_reintentar(
        self, crm: Escenario, voz: type[VozFalsa]
    ) -> None:
        call = await _ia(crm, await _lead(crm))
        voz.modo = "temporal"
        async with _cliente(crm, "admin") as c:
            fallo = await c.post(f"{CALLS}/{call}/initiate")
            estado, _, _, config = await _llamada(crm, call)
            voz.modo = "ok"
            reintento = await c.post(f"{CALLS}/{call}/initiate")

        assert fallo.status_code == 503
        assert (estado, "runtime" in config) == ("pending", False)  # sin marca colgada
        assert reintento.status_code == 202

    async def test_un_rechazo_es_502_y_deja_la_llamada_pendiente(
        self, crm: Escenario, voz: type[VozFalsa]
    ) -> None:
        call = await _ia(crm, await _lead(crm))
        voz.modo = "rechazo"
        async with _cliente(crm, "admin") as c:
            r = await c.post(f"{CALLS}/{call}/initiate")
        assert r.status_code == 502
        assert (await _llamada(crm, call))[0] == "pending"

    async def test_el_error_no_filtra_el_telefono(
        self, crm: Escenario, voz: type[VozFalsa]
    ) -> None:
        call = await _ia(crm, await _lead(crm, phone="+57 300 111-2233"))
        voz.modo = "rechazo"
        async with _cliente(crm, "admin") as c:
            r = await c.post(f"{CALLS}/{call}/initiate")
        assert "573001112233" not in r.text

    async def test_una_marca_reciente_bloquea_y_una_abandonada_no(
        self, crm: Escenario, voz: type[VozFalsa]
    ) -> None:
        ahora = datetime.now(timezone.utc)
        reciente = await _ia(
            crm,
            await _lead(crm),
            ai_voice_config={"runtime": {"state": "starting", "at": ahora.isoformat()}},
        )
        abandonada = await _ia(
            crm,
            await _lead(crm),
            ai_voice_config={
                "runtime": {"state": "starting", "at": (ahora - timedelta(minutes=10)).isoformat()}
            },
        )
        async with _cliente(crm, "admin") as c:
            a = await c.post(f"{CALLS}/{reciente}/initiate")
            b = await c.post(f"{CALLS}/{abandonada}/initiate")
        assert (a.status_code, b.status_code) == (409, 202)

    async def test_permisos_y_limite(
        self, crm: Escenario, voz: type[VozFalsa], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.api.v1 import scheduled_calls

        monkeypatch.setattr(scheduled_calls, "INICIAR_POR_USUARIO_Y_HORA", 2)
        call = await _ia(crm, await _lead(crm))
        async with _cliente(crm, "agent") as c:
            agente = await c.post(f"{CALLS}/{call}/initiate")
        async with _cliente(crm, "supervisor") as c:
            estados = [(await c.post(f"{CALLS}/{call}/initiate")).status_code for _ in range(3)]

        assert agente.status_code == 403
        assert estados == [202, 409, 429]

    async def test_otro_tenant_no_la_inicia(
        self, dos_tenants: tuple[Escenario, Escenario], voz: type[VozFalsa]
    ) -> None:
        a, b = dos_tenants
        for e in (a, b):
            await activar_modulo(e.client_id)
        call = await _ia(a, await _lead(a))
        async with _cliente(b, "admin") as c:
            r = await c.post(f"{CALLS}/{call}/initiate")
        assert r.status_code == 404
        assert voz.llamadas == []
        for e in (a, b):
            await limpiar_leads(e.client_id)


# ── Enlazar con la grabacion ─────────────────────────────────────────────────────────────────


class TestEnlazar:
    async def _en_curso(self, crm: Escenario, voz: type[VozFalsa]) -> tuple[uuid.UUID, str]:
        call = await _ia(crm, await _lead(crm))
        async with _cliente(crm, "admin") as c:
            await c.post(f"{CALLS}/{call}/initiate")
        return call, (voz.llamadas and "CA0001") or ""

    async def test_al_terminar_se_enlaza_y_se_completa(
        self, crm: Escenario, voz: type[VozFalsa]
    ) -> None:
        call, sid = await self._en_curso(crm, voz)

        await guardar_llamada(
            crm.client_id,
            sid,
            {"status": "completed", "direction": "outbound", "duration_seconds": 42},
        )

        estado, resultado, registro, _ = await _llamada(crm, call)
        assert estado == "completed"
        assert registro is not None
        assert resultado is None  # lo decide una persona o el agente despues
        fila = await uno(
            crm.client_id, "SELECT call_sid FROM call_records WHERE id = :i", i=str(registro)
        )
        assert fila[0] == sid

    @pytest.mark.parametrize("estado_twilio", ["no-answer", "busy", "failed", "canceled"])
    async def test_si_no_hablaron_queda_no_show_sin_respuesta(
        self, crm: Escenario, voz: type[VozFalsa], estado_twilio: str
    ) -> None:
        call, sid = await self._en_curso(crm, voz)

        await guardar_llamada(
            crm.client_id, sid, {"status": estado_twilio, "direction": "outbound"}
        )

        estado, resultado, registro, _ = await _llamada(crm, call)
        assert (estado, resultado) == ("no_show", "no_answer")
        assert registro is not None

    async def test_un_estado_intermedio_solo_enlaza(
        self, crm: Escenario, voz: type[VozFalsa]
    ) -> None:
        call, sid = await self._en_curso(crm, voz)

        await guardar_llamada(
            crm.client_id, sid, {"status": "in-progress", "direction": "outbound"}
        )

        estado, _, registro, _ = await _llamada(crm, call)
        assert estado == "in_progress"
        assert registro is not None

    async def test_una_llamada_ajena_no_toca_nada(
        self, crm: Escenario, voz: type[VozFalsa]
    ) -> None:
        call, _ = await self._en_curso(crm, voz)

        await guardar_llamada(
            crm.client_id, "CA-de-otro", {"status": "completed", "direction": "inbound"}
        )

        assert (await _llamada(crm, call))[0] == "in_progress"

    async def test_el_sid_de_otro_tenant_no_cierra_mi_llamada(
        self, dos_tenants: tuple[Escenario, Escenario], voz: type[VozFalsa]
    ) -> None:
        a, b = dos_tenants
        for e in (a, b):
            await activar_modulo(e.client_id)
        call = await _ia(a, await _lead(a))
        async with _cliente(a, "admin") as c:
            await c.post(f"{CALLS}/{call}/initiate")

        await guardar_llamada(
            b.client_id, "CA0001", {"status": "completed", "direction": "outbound"}
        )

        assert (await _llamada(a, call))[0] == "in_progress"
        for e in (a, b):
            await limpiar_leads(e.client_id)
            await sql(
                e.client_id, "DELETE FROM call_records WHERE client_id = :c", c=str(e.client_id)
            )

    async def test_un_fallo_al_enlazar_no_pierde_la_grabacion(
        self, crm: Escenario, voz: type[VozFalsa], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.services import scheduled_call_voice

        async def roto(*a: Any, **k: Any) -> bool:
            raise RuntimeError("fallo de prueba")

        monkeypatch.setattr(scheduled_call_voice, "enlazar_llamada", roto)
        call, sid = await self._en_curso(crm, voz)

        await guardar_llamada(crm.client_id, sid, {"status": "completed", "direction": "outbound"})

        assert await uno(
            crm.client_id, "SELECT count(*) FROM call_records WHERE call_sid = :s", s=sid
        ) == (1,)
        assert (await _llamada(crm, call))[0] == "in_progress"  # sin enlazar, pero nada se perdio


# ── Inicio automatico ────────────────────────────────────────────────────────────────────────


class TestInicioAutomatico:
    async def test_apagado_por_defecto(self, crm: Escenario, voz: type[VozFalsa]) -> None:
        assert get_settings().VOICE_AI_AUTO_START is False
        await _ia(crm, await _lead(crm), scheduled_at=_en(-0.05))

        assert await call_tasks.iniciar_llamadas_ia_todos() == {"disabled": True}
        assert voz.llamadas == []

    async def test_inicia_las_vencidas_dentro_del_margen(
        self,
        crm: Escenario,
        voz: type[VozFalsa],
        con_voz_del_tenant: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(get_settings(), "VOICE_AI_AUTO_START", True)
        a_tiempo = await _ia(crm, await _lead(crm), scheduled_at=_en(-0.05))
        vieja = await _ia(crm, await _lead(crm), scheduled_at=_en(-2))
        futura = await _ia(crm, await _lead(crm), scheduled_at=_en(1))
        humana = await crear_llamada_db(
            crm.client_id, await _lead(crm), assigned_user_id=crm.user_id, scheduled_at=_en(-0.05)
        )

        resultado = await call_tasks.iniciar_llamadas_ia_todos()

        assert resultado == {"started": 1, "failed": 0}
        assert (await _llamada(crm, a_tiempo))[0] == "in_progress"
        assert (await _llamada(crm, vieja))[0] == "pending"  # una llamada de hace 2 h ya no se hace
        assert (await _llamada(crm, futura))[0] == "pending"
        assert (await _llamada(crm, humana))[0] == "pending"

    async def test_un_fallo_no_detiene_a_las_demas(
        self,
        crm: Escenario,
        voz: type[VozFalsa],
        con_voz_del_tenant: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(get_settings(), "VOICE_AI_AUTO_START", True)
        mala = await _ia(crm, await _lead(crm, phone="0000"), scheduled_at=_en(-0.1))
        buena = await _ia(crm, await _lead(crm), scheduled_at=_en(-0.05))

        resultado = await call_tasks.iniciar_llamadas_ia_todos()

        assert resultado == {"started": 1, "failed": 1}
        assert (await _llamada(crm, mala))[0] == "pending"
        assert (await _llamada(crm, buena))[0] == "in_progress"

    async def test_dos_pasadas_a_la_vez_no_duplican(
        self,
        crm: Escenario,
        voz: type[VozFalsa],
        con_voz_del_tenant: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(get_settings(), "VOICE_AI_AUTO_START", True)
        voz.modo = "lento"
        await _ia(crm, await _lead(crm), scheduled_at=_en(-0.05))

        await asyncio.gather(
            call_tasks.iniciar_llamadas_ia_todos(), call_tasks.iniciar_llamadas_ia_todos()
        )

        assert len(voz.llamadas) == 1

    async def test_registro_en_beat(self) -> None:
        from app.tasks.celery_app import celery_app

        for nombre, tarea, cada in (
            ("start-due-ai-calls", "app.tasks.bulk_start_due_ai_calls", 60.0),
            ("send-call-reminders", "app.tasks.bulk_send_call_reminders", 300.0),
        ):
            entrada = celery_app.conf.beat_schedule[nombre]
            assert (entrada["task"], entrada["schedule"], entrada["options"]["queue"]) == (
                tarea,
                cada,
                "bulk",
            )
            assert tarea in celery_app.tasks
            assert celery_app.amqp.router.route({}, tarea)["queue"].name == "bulk"


# ── Recordatorios ────────────────────────────────────────────────────────────────────────────

AHORA = datetime.now(timezone.utc)


async def _con_recordatorio(
    crm: Escenario, lead: uuid.UUID, *, faltan: timedelta, agendada_hace: timedelta, **campos: Any
) -> uuid.UUID:
    return await crear_llamada_db(
        crm.client_id,
        lead,
        assigned_user_id=crm.user_id,
        scheduled_at=AHORA + faltan,
        created_at=AHORA - agendada_hace,
        timezone="America/Bogota",
        **campos,
    )


async def _marcas(crm: Escenario, call: uuid.UUID) -> tuple[Any, Any]:
    return await uno(
        crm.client_id,
        "SELECT reminder_24h_sent_at, reminder_1h_sent_at FROM scheduled_calls WHERE id = :i",
        i=str(call),
    )


class TestRecordatorios:
    async def test_recordatorio_de_24_horas_por_email_en_la_zona_de_la_llamada(
        self, crm: Escenario, correo: ProveedorFalso
    ) -> None:
        lead = await _lead(crm)
        call = await _con_recordatorio(
            crm, lead, faltan=timedelta(hours=20), agendada_hace=timedelta(days=3)
        )

        resumen = await procesar_recordatorios(crm.client_id, AHORA)

        assert resumen.por_resultado == {"sent": 1}
        canal, _, texto, meta = correo.enviados[0]
        assert canal == "email"
        assert "Ana" in texto
        assert "(America/Bogota)" in texto
        assert meta is not None
        assert "Recordatorio" in meta["subject"]
        assert (await _marcas(crm, call))[0] is not None
        assert (await _marcas(crm, call))[1] is None
        assert await uno(
            crm.client_id,
            "SELECT metadata->>'reminder', metadata->>'channel' FROM lead_activities "
            "WHERE lead_id = :l AND activity_type = 'call_reminder_sent'",
            l=str(lead),
        ) == ("24h", "email")

    async def test_no_se_repite(self, crm: Escenario, correo: ProveedorFalso) -> None:
        await _con_recordatorio(
            crm, await _lead(crm), faltan=timedelta(hours=20), agendada_hace=timedelta(days=3)
        )

        await procesar_recordatorios(crm.client_id, AHORA)
        await procesar_recordatorios(crm.client_id, AHORA + timedelta(minutes=5))

        assert len(correo.enviados) == 1

    async def test_el_de_1_hora_avisa_tambien_al_agente(
        self, crm: Escenario, correo: ProveedorFalso
    ) -> None:
        lead = await _lead(crm, company_name="Globex")
        call = await _con_recordatorio(
            crm, lead, faltan=timedelta(minutes=50), agendada_hace=timedelta(days=3)
        )

        await procesar_recordatorios(crm.client_id, AHORA)

        agente = (
            await uno(crm.client_id, "SELECT email FROM users WHERE id = :u", u=str(crm.user_id))
        )[0]
        correo_lead = (
            await uno(crm.client_id, "SELECT 1 FROM leads WHERE id = :l", l=str(lead))
        ) and "@x.com"
        destinos = [e[1] for e in correo.enviados]
        assert destinos[0].endswith(correo_lead)
        assert destinos[1] == agente
        assert (await _marcas(crm, call))[1] is not None
        assert "Globex" in correo.enviados[1][2]

    async def test_una_llamada_agendada_con_menos_de_24h_no_tiene_aviso_de_24h(
        self, crm: Escenario, correo: ProveedorFalso
    ) -> None:
        """La regla es de Dev A: avisar 'manana' de algo agendado hace una hora seria raro."""
        call = await _con_recordatorio(
            crm, await _lead(crm), faltan=timedelta(hours=20), agendada_hace=timedelta(hours=2)
        )

        resumen = await procesar_recordatorios(crm.client_id, AHORA)

        assert resumen.procesados == 0
        assert correo.enviados == []
        assert (await _marcas(crm, call)) == (None, None)

    async def test_sin_recordatorio_lo_lejano_lo_pasado_ni_lo_cancelado(
        self, crm: Escenario, correo: ProveedorFalso
    ) -> None:
        lead = await _lead(crm)
        await _con_recordatorio(
            crm, lead, faltan=timedelta(days=3), agendada_hace=timedelta(days=5)
        )
        await _con_recordatorio(
            crm, lead, faltan=timedelta(minutes=-10), agendada_hace=timedelta(days=5)
        )
        await _con_recordatorio(
            crm,
            lead,
            faltan=timedelta(minutes=30),
            agendada_hace=timedelta(days=5),
            status="cancelled",
            cancelled_at=AHORA,
        )

        resumen = await procesar_recordatorios(crm.client_id, AHORA)

        assert resumen.procesados == 0
        assert correo.enviados == []

    async def test_no_se_avisa_a_quien_pidio_no_recibir_mensajes(
        self, crm: Escenario, correo: ProveedorFalso
    ) -> None:
        lead = await _lead(crm)
        await sql(
            crm.client_id,
            "INSERT INTO lead_activities (client_id, lead_id, activity_type, metadata) "
            "VALUES (:c, :l, 'sequence_reply', CAST('{\"reply_kind\": \"unsubscribe\"}' AS jsonb))",
            c=str(crm.client_id),
            l=str(lead),
        )
        call = await _con_recordatorio(
            crm, lead, faltan=timedelta(minutes=30), agendada_hace=timedelta(days=2)
        )

        resumen = await procesar_recordatorios(crm.client_id, AHORA)

        assert resumen.por_resultado == {"skipped": 1}
        assert correo.enviados == []
        assert (await _marcas(crm, call))[1] is not None  # marcado: no se reintenta eternamente

    async def test_un_fallo_pasajero_se_reintenta_sin_marcar(
        self, crm: Escenario, correo: ProveedorFalso
    ) -> None:
        call = await _con_recordatorio(
            crm, await _lead(crm), faltan=timedelta(minutes=30), agendada_hace=timedelta(days=2)
        )
        correo.falla = RuntimeError("smtp caido")

        primera = await procesar_recordatorios(crm.client_id, AHORA)
        correo.falla = None
        segunda = await procesar_recordatorios(crm.client_id, AHORA + timedelta(minutes=5))

        assert primera.por_resultado == {"retry": 1}
        assert segunda.por_resultado == {"sent": 1}
        assert (await _marcas(crm, call))[1] is not None

    async def test_un_fallo_permanente_se_marca_y_queda_registrado_sin_detalle(
        self, crm: Escenario, correo: ProveedorFalso
    ) -> None:
        lead = await _lead(crm)
        call = await _con_recordatorio(
            crm, lead, faltan=timedelta(minutes=30), agendada_hace=timedelta(days=2)
        )
        correo.falla = ValueError("Destinatario invalido: ana@x.com")

        resumen = await procesar_recordatorios(crm.client_id, AHORA)

        assert resumen.por_resultado == {"failed": 1}
        assert (await _marcas(crm, call))[1] is not None
        meta = (
            await uno(
                crm.client_id,
                "SELECT metadata::text FROM lead_activities WHERE lead_id = :l AND activity_type = 'call_reminder_failed'",
                l=str(lead),
            )
        )[0]
        assert "invalid_destination" in meta
        assert "@" not in meta

    async def test_sin_email_usa_whatsapp_con_la_plantilla_del_tenant(
        self, crm: Escenario, correo: ProveedorFalso
    ) -> None:
        async with _cliente(crm, "admin") as c:
            await c.put(
                CONFIG, json={"call_reminders": {"whatsapp_template": "recordatorio_llamada"}}
            )
        lead = await _lead(crm, email=None, phone="573001112233")
        await _con_recordatorio(
            crm, lead, faltan=timedelta(minutes=30), agendada_hace=timedelta(days=2)
        )

        await procesar_recordatorios(crm.client_id, AHORA)

        assert correo.plantillas == [("whatsapp", "573001112233", "recordatorio_llamada")]
        assert [e for e in correo.enviados if e[0] == "whatsapp"] == []

    async def test_sin_plantilla_whatsapp_manda_texto(
        self, crm: Escenario, correo: ProveedorFalso
    ) -> None:
        lead = await _lead(crm, email=None, phone="573001112233")
        await _con_recordatorio(
            crm, lead, faltan=timedelta(minutes=30), agendada_hace=timedelta(days=2)
        )

        await procesar_recordatorios(crm.client_id, AHORA)

        assert correo.enviados[0][:2] == ("whatsapp", "573001112233")

    async def test_el_email_va_primero_aunque_haya_telefono(
        self, crm: Escenario, correo: ProveedorFalso
    ) -> None:
        lead = await _lead(crm, phone="573001112233")
        await _con_recordatorio(
            crm, lead, faltan=timedelta(minutes=30), agendada_hace=timedelta(days=2)
        )

        await procesar_recordatorios(crm.client_id, AHORA)

        assert correo.enviados[0][0] == "email"
        assert correo.plantillas == []

    async def test_sin_ningun_destino_se_marca_y_se_registra(
        self, crm: Escenario, correo: ProveedorFalso
    ) -> None:
        lead = await _lead(crm, email=None, phone=None)
        call = await _con_recordatorio(
            crm, lead, faltan=timedelta(minutes=30), agendada_hace=timedelta(days=2)
        )

        resumen = await procesar_recordatorios(crm.client_id, AHORA)

        assert resumen.por_resultado == {"failed": 1}
        assert (await _marcas(crm, call))[1] is not None

    async def test_en_el_idioma_del_tenant(self, crm: Escenario, correo: ProveedorFalso) -> None:
        await sql(
            crm.client_id,
            'UPDATE clients SET settings = settings || \'{"default_language": "en"}\'::jsonb WHERE id = :c',
            c=str(crm.client_id),
        )
        await _con_recordatorio(
            crm, await _lead(crm), faltan=timedelta(minutes=30), agendada_hace=timedelta(days=2)
        )

        await procesar_recordatorios(crm.client_id, AHORA)

        assert "starts in one hour" in correo.enviados[0][2]

    async def test_dos_workers_a_la_vez_avisan_una_sola_vez(
        self, crm: Escenario, correo: ProveedorFalso
    ) -> None:
        call = await crear_llamada_db(
            crm.client_id,
            await _lead(crm),
            assigned_user_id=None,
            call_type="ai_voice",
            ai_voice_provider="fakevoice",
            scheduled_at=AHORA + timedelta(minutes=30),
            created_at=AHORA - timedelta(days=2),
        )

        await asyncio.gather(*(procesar_recordatorios(crm.client_id, AHORA) for _ in range(4)))

        assert len(correo.enviados) == 1
        assert (await _marcas(crm, call))[1] is not None

    async def test_un_error_en_una_no_detiene_a_las_demas(
        self, crm: Escenario, correo: ProveedorFalso, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.services import call_notifications

        mala_lead = await _lead(crm)
        mala = await _con_recordatorio(
            crm, mala_lead, faltan=timedelta(minutes=20), agendada_hace=timedelta(days=2)
        )
        buena = await _con_recordatorio(
            crm, await _lead(crm), faltan=timedelta(minutes=40), agendada_hace=timedelta(days=2)
        )
        original = call_notifications._entregar

        async def roto(session: Any, llamada: Any, lead: Any, *a: Any, **k: Any) -> Any:
            if llamada.id == mala:
                raise RuntimeError("fallo de prueba")
            return await original(session, llamada, lead, *a, **k)

        monkeypatch.setattr(call_notifications, "_entregar", roto)

        resumen = await procesar_recordatorios(crm.client_id, AHORA)

        assert resumen.por_resultado == {"error": 1, "sent": 1}
        assert (await _marcas(crm, mala))[1] is None
        assert (await _marcas(crm, buena))[1] is not None

    async def test_otro_tenant_no_se_procesa(
        self, dos_tenants: tuple[Escenario, Escenario], correo: ProveedorFalso
    ) -> None:
        a, b = dos_tenants
        for e in (a, b):
            await activar_modulo(e.client_id)
        await _con_recordatorio(
            a, await _lead(a), faltan=timedelta(minutes=30), agendada_hace=timedelta(days=2)
        )

        resumen = await procesar_recordatorios(b.client_id, AHORA)

        assert resumen.procesados == 0
        assert correo.enviados == []
        for e in (a, b):
            await limpiar_leads(e.client_id)

    async def test_la_tarea_recorre_los_tenants_con_el_modulo(
        self, crm: Escenario, correo: ProveedorFalso, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _con_recordatorio(
            crm, await _lead(crm), faltan=timedelta(minutes=30), agendada_hace=timedelta(days=2)
        )

        async def solo_este() -> list[uuid.UUID]:
            return [crm.client_id]

        monkeypatch.setattr(call_tasks, "_load_active_client_ids", solo_este)

        resultado = await call_tasks.enviar_recordatorios_todos()

        assert resultado["skipped"] is False
        assert resultado["by_result"] == {"sent": 1}

    async def test_una_segunda_pasada_a_la_vez_no_hace_nada(self) -> None:
        redis = get_redis()
        await redis.set(call_tasks.CLAVE_RECORDATORIOS, "1", ex=60)
        try:
            assert await call_tasks.enviar_recordatorios_todos() == {"skipped": True}
        finally:
            await redis.delete(call_tasks.CLAVE_RECORDATORIOS)
