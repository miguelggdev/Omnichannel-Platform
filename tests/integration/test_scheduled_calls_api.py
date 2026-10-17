# ruff: noqa: F811
"""Sprint 19, Dev B: llamadas agendadas, huecos, Google Calendar y avisos.

Requiere base de datos y Redis: `pytest tests/ --run-db`. Lo que un doble no prueba: la doble reserva
bajo concurrencia, RLS real entre tenants, el SAVEPOINT de los avisos y que lo externo (calendario,
email) vaya despues del commit y no deshaga el agendamiento.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import pytest_asyncio

from app.core.database import tenant_session
from app.models.call_record import CallRecord
from tests.integration.call_helpers import (
    CalendarioFalso,
    EventoFalso,
    crear_deal_db,
    instalar_calendario_falso,
)
from tests.integration.lead_helpers import activar_modulo, crear_lead, limpiar_leads
from tests.integration.sequence_helpers import (
    ProveedorFalso,
    instalar_proveedor_falso,
    sql,
    uno,
    usuario,
)
from tests.integration.test_crm_api import Escenario, _cliente, dos_tenants, escenario  # noqa: F401

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

CALLS = "/api/v1/scheduled-calls"


@pytest_asyncio.fixture
async def crm(escenario: Escenario) -> AsyncGenerator[Escenario, None]:
    await activar_modulo(escenario.client_id)
    yield escenario
    await limpiar_leads(escenario.client_id)
    await _limpiar_llamadas(escenario.client_id)


@pytest_asyncio.fixture
async def crm_dos(
    dos_tenants: tuple[Escenario, Escenario],
) -> AsyncGenerator[tuple[Escenario, Escenario], None]:
    for e in dos_tenants:
        await activar_modulo(e.client_id)
    yield dos_tenants
    for e in dos_tenants:
        await limpiar_leads(e.client_id)
        await _limpiar_llamadas(e.client_id)


async def _limpiar_llamadas(client_id: uuid.UUID) -> None:
    """Las grabaciones impiden borrar el tenant (y `limpiar_leads` no las toca)."""
    await sql(client_id, "DELETE FROM call_records WHERE client_id = :c", c=str(client_id))


@pytest.fixture
def correo(monkeypatch: pytest.MonkeyPatch) -> ProveedorFalso:
    return instalar_proveedor_falso(monkeypatch)


@pytest.fixture
def calendario(monkeypatch: pytest.MonkeyPatch) -> CalendarioFalso:
    return instalar_calendario_falso(monkeypatch)


@pytest.fixture(autouse=True)
def _sin_calendario_real(monkeypatch: pytest.MonkeyPatch) -> None:
    """Por defecto no hay calendario de Google configurado (los tests que lo usan lo instalan)."""
    from app.services import call_calendar

    async def ninguno(client_id: Any) -> None:
        return None

    monkeypatch.setattr(call_calendar, "_servicio", ninguno)


def _en(dias: float = 2, hora: int = 15) -> datetime:
    """Un instante futuro, en punto."""
    base = datetime.now(timezone.utc) + timedelta(days=dias)
    return base.replace(hour=hora, minute=0, second=0, microsecond=0)


async def _lead(crm: Escenario, **campos: Any) -> uuid.UUID:
    return await crear_lead(
        crm.client_id,
        **{
            "first_name": "Ana",
            "company_name": "Globex",
            "email": f"{uuid.uuid4().hex[:8]}@x.com",
            **campos,
        },
    )


def _cuerpo(
    lead: uuid.UUID, cuando: datetime, user: uuid.UUID | None, **extra: Any
) -> dict[str, Any]:
    cuerpo: dict[str, Any] = {
        "lead_id": str(lead),
        "scheduled_at": cuando.isoformat(),
        "duration_minutes": 30,
        "call_type": "human",
        "assigned_user_id": str(user) if user else None,
    }
    cuerpo.update(extra)
    return {k: v for k, v in cuerpo.items() if v is not None}


async def _agendar(
    crm: Escenario,
    lead: uuid.UUID,
    cuando: datetime | None = None,
    rol: str = "admin",
    **extra: Any,
) -> dict[str, Any]:
    async with _cliente(crm, rol) as c:
        r = await c.post(CALLS, json=_cuerpo(lead, cuando or _en(), crm.user_id, **extra))
    assert r.status_code == 201, r.text
    return dict(r.json())


# ── Agendar ──────────────────────────────────────────────────────────────────────────────────


class TestAgendar:
    async def test_agenda_y_queda_en_el_historial(self, crm: Escenario) -> None:
        lead = await _lead(crm)
        llamada = await _agendar(crm, lead)

        assert llamada["status"] == "pending"
        assert llamada["call_type"] == "human"
        assert llamada["duration_minutes"] == 30
        assert llamada["timezone"] == "America/Bogota"  # la del negocio por defecto
        assert llamada["calendar_event_id"] is None  # sin calendario
        assert await uno(
            crm.client_id,
            "SELECT count(*) FROM lead_activities WHERE lead_id = :l AND activity_type = 'call_scheduled'",
            l=str(lead),
        ) == (1,)

    async def test_con_la_zona_del_lead(self, crm: Escenario) -> None:
        lead = await _lead(crm)
        llamada = await _agendar(crm, lead, timezone="Europe/Madrid")
        assert llamada["timezone"] == "Europe/Madrid"

    async def test_el_agente_asignado_recibe_un_email_sin_telefono_ni_email_del_lead(
        self, crm: Escenario, correo: ProveedorFalso
    ) -> None:
        lead = await _lead(crm, phone="573001112233")
        await _agendar(crm, lead)

        correo_agente = (
            await uno(crm.client_id, "SELECT email FROM users WHERE id = :u", u=str(crm.user_id))
        )[0]
        canal, destino, texto, meta = correo.enviados[0]
        assert (canal, destino) == ("email", correo_agente)
        assert "Ana" in texto
        assert "Globex" in texto
        assert "573001112233" not in texto
        assert meta is not None
        assert "Nueva llamada agendada" in meta["subject"]

    async def test_una_llamada_de_voz_ia_no_avisa_a_ningun_agente(
        self, crm: Escenario, correo: ProveedorFalso
    ) -> None:
        from tests.integration.call_helpers import instalar_voz_falsa, quitar_voz_falsa

        instalar_voz_falsa()
        try:
            lead = await _lead(crm, phone="573001112233")
            async with _cliente(crm, "admin") as c:
                r = await c.post(
                    CALLS,
                    json={
                        "lead_id": str(lead),
                        "scheduled_at": _en().isoformat(),
                        "call_type": "ai_voice",
                        "ai_voice_provider": "fakevoice",
                    },
                )
        finally:
            quitar_voz_falsa()
        assert r.status_code == 201, r.text
        assert correo.enviados == []

    async def test_un_fallo_del_email_no_deshace_el_agendamiento(
        self, crm: Escenario, correo: ProveedorFalso
    ) -> None:
        correo.falla = RuntimeError("smtp caido")
        lead = await _lead(crm)
        llamada = await _agendar(crm, lead)
        assert llamada["status"] == "pending"

    async def test_doble_reserva_de_la_misma_persona_es_409(self, crm: Escenario) -> None:
        lead = await _lead(crm)
        cuando = _en()
        await _agendar(crm, lead, cuando)
        async with _cliente(crm, "admin") as c:
            solapada = await c.post(
                CALLS, json=_cuerpo(lead, cuando + timedelta(minutes=15), crm.user_id)
            )
            pegada = await c.post(
                CALLS, json=_cuerpo(lead, cuando + timedelta(minutes=30), crm.user_id)
            )
            otra = await usuario(crm.client_id)
            otra_persona = await c.post(CALLS, json=_cuerpo(lead, cuando, otra))

        assert solapada.status_code == 409
        assert pegada.status_code == 201  # tocarse en el borde no es solaparse
        assert otra_persona.status_code == 201

    async def test_dos_peticiones_a_la_vez_para_el_mismo_hueco_solo_dejan_pasar_una(
        self, crm: Escenario
    ) -> None:
        import asyncio

        lead = await _lead(crm)
        cuando = _en()

        async def intentar() -> int:
            async with _cliente(crm, "admin") as c:
                return (await c.post(CALLS, json=_cuerpo(lead, cuando, crm.user_id))).status_code

        estados = sorted(await asyncio.gather(*(intentar() for _ in range(4))))
        assert estados == [201, 409, 409, 409]

    async def test_en_el_pasado_es_400(self, crm: Escenario) -> None:
        lead = await _lead(crm)
        async with _cliente(crm, "admin") as c:
            r = await c.post(
                CALLS,
                json=_cuerpo(lead, datetime.now(timezone.utc) - timedelta(hours=1), crm.user_id),
            )
        assert r.status_code == 400

    @pytest.mark.parametrize(
        "malo",
        [
            {"scheduled_at": "2030-01-01T10:00:00"},  # sin zona horaria
            {"duration_minutes": 4},
            {"duration_minutes": 481},
            {"timezone": "Marte/Olimpo"},
            {"call_type": "robot"},
            {"call_type": "ai_voice", "assigned_user_id": None},  # sin proveedor
            {"call_type": "human", "assigned_user_id": None},  # sin persona
            {"extra": 1},
        ],
    )
    async def test_cuerpos_invalidos(self, crm: Escenario, malo: dict[str, Any]) -> None:
        lead = await _lead(crm)
        cuerpo = _cuerpo(lead, _en(), crm.user_id)
        cuerpo.update(malo)
        cuerpo = {k: v for k, v in cuerpo.items() if v is not None}
        async with _cliente(crm, "admin") as c:
            r = await c.post(CALLS, json=cuerpo)
        assert r.status_code == 422

    async def test_la_clave_runtime_es_del_servidor(self, crm: Escenario) -> None:
        """Si el cliente pudiera escribir `runtime`, falsificaria el id de la llamada del proveedor."""
        from tests.integration.call_helpers import instalar_voz_falsa, quitar_voz_falsa

        instalar_voz_falsa()
        try:
            lead = await _lead(crm, phone="573001112233")
            async with _cliente(crm, "admin") as c:
                r = await c.post(
                    CALLS,
                    json={
                        "lead_id": str(lead),
                        "scheduled_at": _en().isoformat(),
                        "call_type": "ai_voice",
                        "ai_voice_provider": "fakevoice",
                        "ai_voice_config": {"runtime": {"external_id": "CA-robado"}},
                    },
                )
        finally:
            quitar_voz_falsa()
        assert r.status_code == 422

    async def test_proveedor_de_voz_desconocido_es_400(self, crm: Escenario) -> None:
        lead = await _lead(crm)
        async with _cliente(crm, "admin") as c:
            r = await c.post(
                CALLS,
                json={
                    "lead_id": str(lead),
                    "scheduled_at": _en().isoformat(),
                    "call_type": "ai_voice",
                    "ai_voice_provider": "no_existe",
                },
            )
        assert r.status_code == 400

    async def test_lead_inexistente_o_borrado_es_404(self, crm: Escenario) -> None:
        borrado = await _lead(crm)
        await sql(
            crm.client_id, "UPDATE leads SET deleted_at = now() WHERE id = :l", l=str(borrado)
        )
        async with _cliente(crm, "admin") as c:
            a = await c.post(CALLS, json=_cuerpo(uuid.uuid4(), _en(), crm.user_id))
            b = await c.post(CALLS, json=_cuerpo(borrado, _en(), crm.user_id))
        assert (a.status_code, b.status_code) == (404, 404)

    async def test_el_deal_debe_ser_del_lead(self, crm: Escenario) -> None:
        lead, otro = await _lead(crm), await _lead(crm)
        deal_ajeno = await crear_deal_db(crm.client_id, otro)
        deal_propio = await crear_deal_db(crm.client_id, lead)
        async with _cliente(crm, "admin") as c:
            mal = await c.post(
                CALLS, json=_cuerpo(lead, _en(), crm.user_id, deal_id=str(deal_ajeno))
            )
            bien = await c.post(
                CALLS, json=_cuerpo(lead, _en(3), crm.user_id, deal_id=str(deal_propio))
            )
        assert (mal.status_code, bien.status_code) == (400, 201)

    async def test_persona_de_otro_tenant_es_400(
        self, crm_dos: tuple[Escenario, Escenario]
    ) -> None:
        a, b = crm_dos
        lead = await _lead(a)
        async with _cliente(a, "admin") as c:
            r = await c.post(CALLS, json=_cuerpo(lead, _en(), b.user_id))
        assert r.status_code == 400

    async def test_permisos_del_agente(self, crm: Escenario) -> None:
        otro = await usuario(crm.client_id)
        libre = await _lead(crm)
        ajeno = await _lead(crm, assigned_user_id=otro)
        async with _cliente(crm, "agent") as c:
            para_otro = await c.post(CALLS, json=_cuerpo(libre, _en(), otro))
            lead_ajeno = await c.post(CALLS, json=_cuerpo(ajeno, _en(), crm.user_id))
            propia = await c.post(CALLS, json=_cuerpo(libre, _en(), crm.user_id))
        assert (para_otro.status_code, lead_ajeno.status_code, propia.status_code) == (
            403,
            403,
            201,
        )

    async def test_modulo_apagado(self, crm: Escenario) -> None:
        await activar_modulo(crm.client_id, False)
        async with _cliente(crm, "admin") as c:
            assert (await c.get(CALLS)).status_code == 403


# ── Huecos ───────────────────────────────────────────────────────────────────────────────────


class TestHuecos:
    async def _huecos(self, crm: Escenario, **params: Any) -> dict[str, Any]:
        async with _cliente(crm, "agent") as c:
            r = await c.get(f"{CALLS}/slots", params=params)
        assert r.status_code == 200, r.text
        return dict(r.json())

    async def test_sin_calendario_solo_descuenta_la_base(self, crm: Escenario) -> None:
        lead = await _lead(crm)
        cuando = _en(3, 15)  # 10:00 en Bogota
        await _agendar(crm, lead, cuando)
        desde = cuando.replace(hour=0)

        r = await self._huecos(
            crm,
            user_id=str(crm.user_id),
            date_from=desde.isoformat(),
            date_to=(desde + timedelta(days=1)).isoformat(),
        )

        slots = [datetime.fromisoformat(s) for s in r["slots"]]
        assert r["calendar_checked"] is False
        assert r["timezone"] == "America/Bogota"
        assert cuando not in slots  # ocupado por la llamada de esa persona
        assert cuando + timedelta(minutes=30) in slots  # el siguiente hueco esta libre
        assert slots == sorted(slots)
        assert all(8 <= s.astimezone(timezone(timedelta(hours=-5))).hour < 18 for s in slots)

    async def test_otra_persona_si_tiene_ese_hueco(self, crm: Escenario) -> None:
        lead = await _lead(crm)
        cuando = _en(3, 15)
        await _agendar(crm, lead, cuando)
        otra = await usuario(crm.client_id)
        desde = cuando.replace(hour=0)

        r = await self._huecos(
            crm,
            user_id=str(otra),
            date_from=desde.isoformat(),
            date_to=(desde + timedelta(days=1)).isoformat(),
        )

        assert cuando in [datetime.fromisoformat(s) for s in r["slots"]]

    async def test_los_eventos_ajenos_del_calendario_bloquean(
        self, crm: Escenario, calendario: CalendarioFalso
    ) -> None:
        cuando = _en(3, 15)
        calendario.ajenos.append(EventoFalso("reunion", cuando, 60))
        desde = cuando.replace(hour=0)

        r = await self._huecos(
            crm, date_from=desde.isoformat(), date_to=(desde + timedelta(days=1)).isoformat()
        )

        slots = [datetime.fromisoformat(s) for s in r["slots"]]
        assert r["calendar_checked"] is True
        assert cuando not in slots
        assert cuando + timedelta(minutes=30) not in slots
        assert cuando + timedelta(minutes=60) in slots

    async def test_nuestros_eventos_no_bloquean_a_las_demas_personas(
        self, crm: Escenario, calendario: CalendarioFalso
    ) -> None:
        lead = await _lead(crm)
        cuando = _en(3, 15)
        await _agendar(crm, lead, cuando)  # crea su evento en el calendario
        assert len(calendario.creados) == 1
        otra = await usuario(crm.client_id)
        desde = cuando.replace(hour=0)

        r = await self._huecos(
            crm,
            user_id=str(otra),
            date_from=desde.isoformat(),
            date_to=(desde + timedelta(days=1)).isoformat(),
        )

        assert cuando in [datetime.fromisoformat(s) for s in r["slots"]]

    async def test_si_google_falla_los_huecos_salen_sin_calendario(
        self, crm: Escenario, calendario: CalendarioFalso
    ) -> None:
        calendario.falla = RuntimeError("google caido")
        r = await self._huecos(crm)
        assert r["calendar_checked"] is False

    @pytest.mark.parametrize(
        "params",
        [
            {"date_from": "2026-10-20T00:00:00", "date_to": "2026-10-21T00:00:00"},  # sin zona
            {"date_from": "2026-10-21T00:00:00Z", "date_to": "2026-10-20T00:00:00Z"},
            {"date_from": "2026-10-01T00:00:00Z", "date_to": "2026-12-01T00:00:00Z"},
        ],
    )
    async def test_rangos_invalidos_son_400(self, crm: Escenario, params: dict[str, str]) -> None:
        async with _cliente(crm, "agent") as c:
            r = await c.get(f"{CALLS}/slots", params=params)
        assert r.status_code == 400

    async def test_duracion_fuera_de_rango_es_422(self, crm: Escenario) -> None:
        async with _cliente(crm, "agent") as c:
            malos = [
                await c.get(f"{CALLS}/slots", params={"duration_minutes": 4}),
                await c.get(f"{CALLS}/slots", params={"duration_minutes": 481}),
            ]
        assert [r.status_code for r in malos] == [422, 422]


# ── Google Calendar al agendar ───────────────────────────────────────────────────────────────


class TestCalendario:
    async def test_crea_el_evento_y_lo_guarda(
        self, crm: Escenario, calendario: CalendarioFalso
    ) -> None:
        lead = await _lead(crm)
        cuando = _en()
        llamada = await _agendar(crm, lead, cuando)

        assert llamada["calendar_event_id"] == "evt-1"
        evento = calendario.creados["evt-1"]
        assert evento["start"] == cuando
        assert evento["end"] == cuando + timedelta(minutes=30)
        assert evento["summary"] == "Llamada con Ana (Globex)"

    async def test_un_evento_ajeno_en_ese_horario_es_409(
        self, crm: Escenario, calendario: CalendarioFalso
    ) -> None:
        cuando = _en()
        calendario.ajenos.append(EventoFalso("reunion", cuando, 60))
        lead = await _lead(crm)
        async with _cliente(crm, "admin") as c:
            r = await c.post(CALLS, json=_cuerpo(lead, cuando, crm.user_id))
        assert r.status_code == 409
        assert await uno(crm.client_id, "SELECT count(*) FROM scheduled_calls") == (0,)

    async def test_si_google_falla_la_llamada_se_agenda_sin_evento(
        self, crm: Escenario, calendario: CalendarioFalso
    ) -> None:
        calendario.falla = RuntimeError("google caido")
        lead = await _lead(crm)
        llamada = await _agendar(crm, lead)
        assert llamada["status"] == "pending"
        assert llamada["calendar_event_id"] is None

    async def test_el_titulo_del_evento_no_lleva_telefono_ni_email(
        self, crm: Escenario, calendario: CalendarioFalso
    ) -> None:
        lead = await _lead(crm, phone="573001112233", email="secreto@cliente.com")
        await _agendar(crm, lead)
        assert "573001112233" not in str(calendario.creados)
        assert "secreto@cliente.com" not in str(calendario.creados)

    async def test_cancelar_borra_el_evento(
        self, crm: Escenario, calendario: CalendarioFalso
    ) -> None:
        lead = await _lead(crm)
        llamada = await _agendar(crm, lead)
        async with _cliente(crm, "admin") as c:
            r = await c.post(f"{CALLS}/{llamada['id']}/cancel")

        assert r.status_code == 200
        assert r.json()["status"] == "cancelled"
        assert r.json()["calendar_event_id"] is None
        assert calendario.borrados == ["evt-1"]

    async def test_reprogramar_mueve_el_evento_a_la_llamada_nueva(
        self, crm: Escenario, calendario: CalendarioFalso
    ) -> None:
        lead = await _lead(crm)
        llamada = await _agendar(crm, lead, _en(2))
        nueva_hora = _en(4)
        async with _cliente(crm, "admin") as c:
            r = await c.post(
                f"{CALLS}/{llamada['id']}/reschedule", json={"scheduled_at": nueva_hora.isoformat()}
            )
            vieja = (await c.get(f"{CALLS}/{llamada['id']}")).json()

        assert r.status_code == 201, r.text
        assert r.json()["id"] != llamada["id"]
        assert r.json()["calendar_event_id"] == "evt-1"  # el mismo evento, movido
        assert calendario.movidos[0][0] == "evt-1"
        assert calendario.movidos[0][1] == nueva_hora
        assert vieja["status"] == "rescheduled"
        assert vieja["calendar_event_id"] is None

    async def test_si_no_se_puede_mover_se_crea_uno_nuevo(
        self, crm: Escenario, calendario: CalendarioFalso
    ) -> None:
        lead = await _lead(crm)
        llamada = await _agendar(crm, lead, _en(2))
        original = calendario.modify_event

        async def roto(*a: Any, **k: Any) -> Any:
            raise RuntimeError("evento ya no existe")

        calendario.modify_event = roto  # type: ignore[method-assign]
        async with _cliente(crm, "admin") as c:
            r = await c.post(
                f"{CALLS}/{llamada['id']}/reschedule", json={"scheduled_at": _en(4).isoformat()}
            )
        calendario.modify_event = original  # type: ignore[method-assign]

        assert r.status_code == 201
        assert r.json()["calendar_event_id"] == "evt-2"
        assert "evt-1" in calendario.borrados

    async def test_no_hay_calendario_para_la_voz_ia(
        self, crm: Escenario, calendario: CalendarioFalso
    ) -> None:
        from tests.integration.call_helpers import instalar_voz_falsa, quitar_voz_falsa

        instalar_voz_falsa()
        try:
            lead = await _lead(crm, phone="573001112233")
            async with _cliente(crm, "admin") as c:
                r = await c.post(
                    CALLS,
                    json={
                        "lead_id": str(lead),
                        "scheduled_at": _en().isoformat(),
                        "call_type": "ai_voice",
                        "ai_voice_provider": "fakevoice",
                    },
                )
        finally:
            quitar_voz_falsa()
        assert r.status_code == 201
        assert calendario.creados == {}


# ── Estados ──────────────────────────────────────────────────────────────────────────────────


class TestEstados:
    async def test_confirmar_y_completar(self, crm: Escenario) -> None:
        lead = await _lead(crm)
        llamada = await _agendar(crm, lead)
        async with _cliente(crm, "admin") as c:
            confirmada = await c.post(f"{CALLS}/{llamada['id']}/confirm")
            otra_vez = await c.post(f"{CALLS}/{llamada['id']}/confirm")
            completada = await c.post(
                f"{CALLS}/{llamada['id']}/complete",
                json={"outcome": "meeting_booked", "notes": "Quiere una demo"},
            )

        assert confirmada.json()["status"] == "confirmed"
        assert confirmada.json()["confirmed_at"] is not None
        assert otra_vez.status_code == 409
        assert completada.json()["status"] == "completed"
        assert completada.json()["outcome"] == "meeting_booked"
        assert completada.json()["notes"] == "Quiere una demo"

    async def test_no_se_completa_una_llamada_pendiente(self, crm: Escenario) -> None:
        lead = await _lead(crm)
        llamada = await _agendar(crm, lead)
        async with _cliente(crm, "admin") as c:
            r = await c.post(f"{CALLS}/{llamada['id']}/complete", json={"outcome": "interested"})
        assert r.status_code == 409

    async def test_resultado_desconocido_es_422(self, crm: Escenario) -> None:
        lead = await _lead(crm)
        llamada = await _agendar(crm, lead)
        async with _cliente(crm, "admin") as c:
            await c.post(f"{CALLS}/{llamada['id']}/confirm")
            r = await c.post(f"{CALLS}/{llamada['id']}/complete", json={"outcome": "me_cayo_bien"})
        assert r.status_code == 422

    async def test_completar_con_una_grabacion_de_otro_tenant_es_400(
        self, crm_dos: tuple[Escenario, Escenario]
    ) -> None:
        a, b = crm_dos
        lead = await _lead(a)
        llamada = await _agendar(a, lead)
        registro = uuid.uuid4()
        async with tenant_session(b.client_id) as s:
            s.add(
                CallRecord(
                    id=registro,
                    client_id=b.client_id,
                    call_sid="CA-de-b",
                    direction="outbound",
                    status="completed",
                    started_at=datetime.now(timezone.utc),
                    transcript=[],
                )
            )
        async with _cliente(a, "admin") as c:
            await c.post(f"{CALLS}/{llamada['id']}/confirm")
            r = await c.post(
                f"{CALLS}/{llamada['id']}/complete",
                json={"outcome": "interested", "call_record_id": str(registro)},
            )
        assert r.status_code == 400

    async def test_no_show(self, crm: Escenario) -> None:
        lead = await _lead(crm)
        llamada = await _agendar(crm, lead)
        async with _cliente(crm, "admin") as c:
            r = await c.post(f"{CALLS}/{llamada['id']}/no-show")
            otra = await c.post(f"{CALLS}/{llamada['id']}/confirm")
        assert r.json()["status"] == "no_show"
        assert otra.status_code == 409  # un estado final no tiene salida

    async def test_cancelar_libera_el_hueco(self, crm: Escenario) -> None:
        lead = await _lead(crm)
        cuando = _en()
        llamada = await _agendar(crm, lead, cuando)
        async with _cliente(crm, "admin") as c:
            await c.post(f"{CALLS}/{llamada['id']}/cancel")
            r = await c.post(CALLS, json=_cuerpo(lead, cuando, crm.user_id))
            repetida = await c.post(f"{CALLS}/{llamada['id']}/cancel")
        assert r.status_code == 201
        assert repetida.status_code == 409

    async def test_reprogramar_deja_la_vieja_y_crea_otra(self, crm: Escenario) -> None:
        lead = await _lead(crm)
        llamada = await _agendar(crm, lead, _en(2))
        async with _cliente(crm, "admin") as c:
            r = await c.post(
                f"{CALLS}/{llamada['id']}/reschedule",
                json={"scheduled_at": _en(5).isoformat(), "duration_minutes": 45},
            )
            vieja = (await c.get(f"{CALLS}/{llamada['id']}")).json()
            otra_vez = await c.post(
                f"{CALLS}/{llamada['id']}/reschedule", json={"scheduled_at": _en(6).isoformat()}
            )

        assert r.json()["status"] == "pending"
        assert r.json()["duration_minutes"] == 45
        assert vieja["status"] == "rescheduled"
        assert otra_vez.status_code == 409  # la vieja ya no se puede mover

    async def test_reprogramar_a_un_hueco_ocupado_o_al_pasado(self, crm: Escenario) -> None:
        lead = await _lead(crm)
        primera = await _agendar(crm, lead, _en(2))
        segunda = await _agendar(crm, lead, _en(3))
        async with _cliente(crm, "admin") as c:
            ocupado = await c.post(
                f"{CALLS}/{segunda['id']}/reschedule", json={"scheduled_at": _en(2).isoformat()}
            )
            pasado = await c.post(
                f"{CALLS}/{primera['id']}/reschedule",
                json={
                    "scheduled_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
                },
            )
            intacta = (await c.get(f"{CALLS}/{segunda['id']}")).json()
        assert (ocupado.status_code, pasado.status_code) == (409, 400)
        assert intacta["status"] == "pending"

    async def test_editar_solo_las_notas(self, crm: Escenario) -> None:
        lead = await _lead(crm)
        llamada = await _agendar(crm, lead)
        async with _cliente(crm, "agent") as c:
            r = await c.patch(f"{CALLS}/{llamada['id']}", json={"notes": "Llamar con el CTO"})
            hora = await c.patch(
                f"{CALLS}/{llamada['id']}", json={"scheduled_at": _en(9).isoformat()}
            )
        assert r.json()["notes"] == "Llamar con el CTO"
        assert r.json()["scheduled_at"] == llamada["scheduled_at"]
        assert hora.status_code == 422  # la hora se cambia reprogramando

    async def test_un_agente_no_toca_las_llamadas_de_otro(self, crm: Escenario) -> None:
        otro = await usuario(crm.client_id)
        lead = await _lead(crm)
        async with _cliente(crm, "admin") as c:
            llamada = (await c.post(CALLS, json=_cuerpo(lead, _en(), otro))).json()
        async with _cliente(crm, "agent") as c:
            estados = [
                (await c.post(f"{CALLS}/{llamada['id']}/confirm")).status_code,
                (await c.post(f"{CALLS}/{llamada['id']}/cancel")).status_code,
                (await c.post(f"{CALLS}/{llamada['id']}/no-show")).status_code,
                (await c.patch(f"{CALLS}/{llamada['id']}", json={"notes": "x"})).status_code,
            ]
        assert estados == [403, 403, 403, 403]

    async def test_inexistente_es_404(self, crm: Escenario) -> None:
        falsa = uuid.uuid4()
        async with _cliente(crm, "admin") as c:
            respuestas = [
                await c.get(f"{CALLS}/{falsa}"),
                await c.post(f"{CALLS}/{falsa}/confirm"),
                await c.post(f"{CALLS}/{falsa}/cancel"),
                await c.post(
                    f"{CALLS}/{falsa}/reschedule", json={"scheduled_at": _en().isoformat()}
                ),
                await c.post(f"{CALLS}/{falsa}/complete", json={"outcome": "interested"}),
                await c.post(f"{CALLS}/{falsa}/initiate"),
            ]
        assert [r.status_code for r in respuestas] == [404] * 6


# ── Listado y aislamiento ────────────────────────────────────────────────────────────────────


class TestListado:
    async def test_filtra_y_pagina(self, crm: Escenario) -> None:
        lead, otro_lead = await _lead(crm), await _lead(crm)
        otra = await usuario(crm.client_id)
        a = await _agendar(crm, lead, _en(2))
        await _agendar(crm, lead, _en(3))
        async with _cliente(crm, "admin") as c:
            await c.post(CALLS, json=_cuerpo(otro_lead, _en(4), otra))
            await c.post(f"{CALLS}/{a['id']}/confirm")
            todas = (await c.get(CALLS)).json()
            pag = (await c.get(CALLS, params={"page_size": 2, "page": 2})).json()
            confirmadas = (await c.get(CALLS, params={"status": "confirmed"})).json()
            de_otra = (await c.get(CALLS, params={"assigned_user_id": str(otra)})).json()
            del_lead = (await c.get(CALLS, params={"lead_id": str(otro_lead)})).json()
            rango = (
                await c.get(
                    CALLS,
                    params={
                        "scheduled_from": (_en(3) - timedelta(hours=1)).isoformat(),
                        "scheduled_to": (_en(3) + timedelta(hours=1)).isoformat(),
                    },
                )
            ).json()

        assert todas["total"] == 3
        assert [x["scheduled_at"] for x in todas["items"]] == sorted(
            x["scheduled_at"] for x in todas["items"]
        )
        assert (pag["total_pages"], len(pag["items"])) == (2, 1)
        assert confirmadas["total"] == 1
        assert de_otra["total"] == 1
        assert del_lead["total"] == 1
        assert rango["total"] == 1

    async def test_un_tenant_no_ve_ni_toca_las_llamadas_de_otro(
        self, crm_dos: tuple[Escenario, Escenario]
    ) -> None:
        a, b = crm_dos
        lead = await _lead(a)
        llamada = await _agendar(a, lead)
        async with _cliente(b, "admin") as c:
            lista = (await c.get(CALLS)).json()
            respuestas = [
                await c.get(f"{CALLS}/{llamada['id']}"),
                await c.post(f"{CALLS}/{llamada['id']}/confirm"),
                await c.post(f"{CALLS}/{llamada['id']}/cancel"),
                await c.patch(f"{CALLS}/{llamada['id']}", json={"notes": "x"}),
                await c.post(CALLS, json=_cuerpo(lead, _en(5), b.user_id)),
            ]
        assert lista["total"] == 0
        assert [r.status_code for r in respuestas] == [404] * 5
        assert (
            await uno(
                a.client_id, "SELECT status FROM scheduled_calls WHERE id = :i", i=llamada["id"]
            )
        )[0] == "pending"
