"""Deals y llamadas agendadas contra PostgreSQL real con RLS (Sprint 19, slice Dev A).

Requiere base de datos: `pytest tests/ --run-db`. Lo que un doble no puede probar: que las dos
tablas aislen por tenant (lectura y `WITH CHECK`), que una referencia a otro tenant no se cuele
por la FK (que no mira la RLS), los CHECKs de coherencia, que ganar un deal convierta al lead en
la misma transaccion, el pipeline agregado en SQL igual que en Python, la doble reserva de una
persona bajo concurrencia, los recordatorios y la supresion RGPD.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.config import get_settings
from app.core.database import tenant_session
from app.models.deal import Deal
from app.models.lead import Lead
from app.models.lead_activity import LeadActivity
from app.models.scheduled_call import ScheduledCall
from app.schemas.deal import (
    DealCreate,
    DealUpdate,
    ScheduledCallCreate,
    ScheduledCallReschedule,
)
from app.services.call_scheduling import RECORDATORIO_1H, RECORDATORIO_24H, EstadoInvalidoError
from app.services.deal_pipeline import resumir_pipeline
from app.services.deals import (
    DealError,
    actualizar_deal,
    cambiar_etapa,
    crear_deal,
    deals_para_export,
    ingresos_por_mes,
    resumen_pipeline,
)
from app.services.lead_privacy import ANONIMIZADO
from app.services.lead_suppression import suprimir_leads
from app.services.scheduled_calls import (
    CallError,
    agendar,
    cambiar_estado,
    completar,
    huecos_para,
    llamadas_para_export,
    recordatorios_pendientes,
    reprogramar,
)
from tests.integration.lead_helpers import limpiar_leads

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

AHORA = datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)  # lunes 10:00 en Bogota


@dataclass
class Tenant:
    """Un tenant de prueba con dos comerciales y una medica."""

    id: uuid.UUID
    ana: uuid.UUID
    beto: uuid.UUID
    medica: uuid.UUID


async def _usuario(client_id: uuid.UUID, rol: str, activo: bool = True) -> uuid.UUID:
    user_id = uuid.uuid4()
    async with tenant_session(client_id) as s:
        await s.execute(
            text(
                "INSERT INTO users (id, client_id, email, password_hash, first_name, last_name, "
                "role, is_active) VALUES (:id, :c, :email, 'x', 'N', 'A', :rol, :activo)"
            ),
            {
                "id": str(user_id),
                "c": str(client_id),
                "email": f"{user_id.hex[:12]}@example.com",
                "rol": rol,
                "activo": activo,
            },
        )
    return user_id


async def _crear_tenant() -> Tenant:
    client_id = uuid.uuid4()
    async with tenant_session(client_id) as s:
        await s.execute(
            text(
                "INSERT INTO clients (id, name, slug, plan, lead_management_enabled, settings) "
                "VALUES (:id, 'T', :slug, 'free', true, CAST(:settings AS jsonb))"
            ),
            {
                "id": str(client_id),
                "slug": f"deal-{client_id.hex[:10]}",
                "settings": json.dumps({"business_profile": {"timezone": "America/Bogota"}}),
            },
        )
    return Tenant(
        id=client_id,
        ana=await _usuario(client_id, "agent"),
        beto=await _usuario(client_id, "agent"),
        medica=await _usuario(client_id, "medical"),
    )


async def _borrar_tenant(client_id: uuid.UUID) -> None:
    await limpiar_leads(client_id)
    async with tenant_session(client_id) as s:
        for tabla in ("call_records", "users"):
            await s.execute(
                text(f"DELETE FROM {tabla} WHERE client_id = :c"),  # noqa: S608
                {"c": str(client_id)},
            )
        await s.execute(text("DELETE FROM clients WHERE id = :c"), {"c": str(client_id)})


@pytest_asyncio.fixture
async def tenant() -> AsyncGenerator[Tenant, None]:
    t = await _crear_tenant()
    yield t
    await _borrar_tenant(t.id)


@pytest_asyncio.fixture
async def otro() -> AsyncGenerator[Tenant, None]:
    t = await _crear_tenant()
    yield t
    await _borrar_tenant(t.id)


async def _lead(client_id: uuid.UUID, **campos: Any) -> uuid.UUID:
    campos.setdefault("first_name", "Ana")
    async with tenant_session(client_id) as s:
        lead = Lead(client_id=client_id, **campos)
        s.add(lead)
        await s.flush()
        return lead.id


async def _cargar(s: Any, modelo: Any, id_: uuid.UUID) -> Any:
    return (await s.execute(select(modelo).where(modelo.id == id_))).scalar_one()


async def _deal(t: Tenant, lead_id: uuid.UUID, **kw: Any) -> uuid.UUID:
    async with tenant_session(t.id) as s:
        lead = await _cargar(s, Lead, lead_id)
        deal = await crear_deal(
            s, lead, DealCreate(lead_id=lead_id, title=kw.pop("title", "Plan"), **kw), ahora=AHORA
        )
        return deal.id


def _cita(lead_id: uuid.UUID, inicio: datetime, **kw: Any) -> ScheduledCallCreate:
    return ScheduledCallCreate(lead_id=lead_id, scheduled_at=inicio, **kw)


async def _agendar(t: Tenant, lead_id: uuid.UUID, inicio: datetime, **kw: Any) -> uuid.UUID:
    kw.setdefault("assigned_user_id", t.ana)
    async with tenant_session(t.id) as s:
        llamada = await agendar(
            s, await _cargar(s, Lead, lead_id), _cita(lead_id, inicio, **kw), ahora=AHORA
        )
        return llamada.id


# ─── RLS ────────────────────────────────────────────────────────────────────────────────────


class TestAislamiento:
    async def test_un_tenant_no_ve_deals_ni_llamadas_de_otro(
        self, tenant: Tenant, otro: Tenant
    ) -> None:
        lead_id = await _lead(tenant.id)
        await _deal(tenant, lead_id)
        await _agendar(tenant, lead_id, AHORA + timedelta(days=1))
        async with tenant_session(otro.id) as s:
            for modelo in (Deal, ScheduledCall):
                assert (await s.execute(select(func.count()).select_from(modelo))).scalar() == 0

    async def test_with_check(self, tenant: Tenant, otro: Tenant) -> None:
        lead_id = await _lead(tenant.id)
        async with tenant_session(otro.id) as s:
            s.add(Deal(client_id=tenant.id, lead_id=lead_id, title="intruso"))
            with pytest.raises(DBAPIError):
                await s.flush()
        async with tenant_session(otro.id) as s:
            s.add(
                ScheduledCall(
                    client_id=tenant.id, lead_id=lead_id, assigned_user_id=tenant.ana,
                    scheduled_at=AHORA + timedelta(days=1),
                )
            )  # fmt: skip
            with pytest.raises(DBAPIError):
                await s.flush()

    async def test_rls_forzada(self, tenant: Tenant) -> None:
        async with tenant_session(tenant.id) as s:
            filas = (
                await s.execute(
                    text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname IN ('deals', 'scheduled_calls')"
                    )
                )
            ).all()
        assert sorted(filas) == [("deals", True, True), ("scheduled_calls", True, True)]

    async def test_no_se_asigna_un_usuario_de_otro_tenant(
        self, tenant: Tenant, otro: Tenant
    ) -> None:
        """La FK a `users` no mira la RLS: sin la comprobacion, esto se guardaria."""
        lead_id = await _lead(tenant.id)
        with pytest.raises(DealError) as exc:
            await _deal(tenant, lead_id, assigned_user_id=otro.ana)
        assert exc.value.code == "user_unavailable"
        with pytest.raises(CallError) as exc_llamada:
            await _agendar(tenant, lead_id, AHORA + timedelta(days=1), assigned_user_id=otro.ana)
        assert exc_llamada.value.code == "user_unavailable"

    async def test_no_se_enlaza_una_grabacion_de_otro_tenant(
        self, tenant: Tenant, otro: Tenant
    ) -> None:
        ajena = uuid.uuid4()
        async with tenant_session(otro.id) as s:
            await s.execute(
                text(
                    "INSERT INTO call_records (id, client_id, call_sid, direction, status, "
                    "transcript, started_at) VALUES (:id, :c, :sid, 'outbound', 'completed', "
                    "pgp_sym_encrypt('[]', :clave), now())"
                ),
                {
                    "id": str(ajena),
                    "c": str(otro.id),
                    "sid": f"CA{uuid.uuid4().hex}",
                    "clave": get_settings().ENCRYPTION_KEY,
                },
            )
        lead_id = await _lead(tenant.id)
        llamada_id = await _agendar(tenant, lead_id, AHORA + timedelta(days=1))
        async with tenant_session(tenant.id) as s:
            llamada = await _cargar(s, ScheduledCall, llamada_id)
            cambiar_estado(s, llamada, "confirmed", ahora=AHORA)
            with pytest.raises(CallError) as exc:
                await completar(s, llamada, outcome="interested", ahora=AHORA, call_record_id=ajena)
            assert exc.value.code == "call_record_unavailable"


# ─── Deals ──────────────────────────────────────────────────────────────────────────────────


class TestDeals:
    async def test_alta_con_probabilidad_y_responsable_del_lead(self, tenant: Tenant) -> None:
        lead_id = await _lead(tenant.id, assigned_user_id=tenant.beto)
        deal_id = await _deal(tenant, lead_id, stage="proposal", value=Decimal("1200.50"))
        async with tenant_session(tenant.id) as s:
            deal = await _cargar(s, Deal, deal_id)
            assert (deal.probability, deal.assigned_user_id) == (50, tenant.beto)
            assert deal.value == Decimal("1200.50")
            [actividad] = (
                await s.execute(select(LeadActivity).where(LeadActivity.lead_id == lead_id))
            ).scalars()
        assert actividad.activity_type == "deal_created"

    @pytest.mark.parametrize("rol", ["medical", "inactivo"])
    async def test_responsable_no_valido(self, tenant: Tenant, rol: str) -> None:
        usuario = (
            tenant.medica if rol == "medical" else await _usuario(tenant.id, "agent", activo=False)
        )
        lead_id = await _lead(tenant.id)
        with pytest.raises(DealError, match="usuario"):
            await _deal(tenant, lead_id, assigned_user_id=usuario)

    async def test_editar_responsable_tambien_se_valida(self, tenant: Tenant, otro: Tenant) -> None:
        lead_id = await _lead(tenant.id)
        deal_id = await _deal(tenant, lead_id)
        async with tenant_session(tenant.id) as s:
            deal = await _cargar(s, Deal, deal_id)
            with pytest.raises(DealError):
                await actualizar_deal(s, deal, DealUpdate(assigned_user_id=otro.ana))
            assert await actualizar_deal(s, deal, DealUpdate(title="Nuevo")) == ["title"]
            assert await actualizar_deal(s, deal, DealUpdate(assigned_user_id=tenant.beto)) == [
                "assigned_user_id"
            ]

    @pytest.mark.parametrize(
        "campos",
        [{"deleted_at": AHORA}, {"first_name": ANONIMIZADO, "email": None, "phone": None}],
    )
    async def test_no_sobre_un_lead_borrado_o_suprimido(
        self, tenant: Tenant, campos: dict[str, Any]
    ) -> None:
        lead_id = await _lead(tenant.id, **campos)
        with pytest.raises(DealError) as exc:
            await _deal(tenant, lead_id)
        assert exc.value.code == "lead_unavailable"

    async def test_ganar_convierte_al_lead_y_reabrir_lo_devuelve(self, tenant: Tenant) -> None:
        lead_id = await _lead(tenant.id)
        deal_id = await _deal(tenant, lead_id)
        noche = datetime(2026, 10, 6, 2, 0, tzinfo=timezone.utc)  # 21:00 del 5 en Bogota
        async with tenant_session(tenant.id) as s:
            deal = await _cargar(s, Deal, deal_id)
            assert await cambiar_etapa(s, deal, "closed_won", ahora=noche)
        async with tenant_session(tenant.id) as s:
            lead = await _cargar(s, Lead, lead_id)
            deal = await _cargar(s, Deal, deal_id)
            assert (lead.status, lead.converted_at) == ("won", noche)
            assert deal.actual_close_date == date(2026, 10, 5)
            assert await cambiar_etapa(s, deal, "negotiation", ahora=AHORA)
        async with tenant_session(tenant.id) as s:
            lead = await _cargar(s, Lead, lead_id)
            assert (lead.status, lead.converted_at) == ("active", None)
            tipos = (
                await s.execute(
                    select(LeadActivity.metadata_)
                    .where(LeadActivity.activity_type == "deal_stage_changed")
                    .order_by(LeadActivity.created_at)
                )
            ).scalars()
            assert [(m["stage_from"], m["stage_to"], m["lead_status"]) for m in tipos] == [
                ("new_contact", "closed_won", "won"),
                ("closed_won", "negotiation", "active"),
            ]

    async def test_reabrir_con_otro_ganado_no_desconvierte(self, tenant: Tenant) -> None:
        lead_id = await _lead(tenant.id)
        a, b = await _deal(tenant, lead_id), await _deal(tenant, lead_id)
        async with tenant_session(tenant.id) as s:
            for deal_id in (a, b):
                await cambiar_etapa(s, await _cargar(s, Deal, deal_id), "closed_won", ahora=AHORA)
            assert not await cambiar_etapa(
                s, await _cargar(s, Deal, a), "proposal", ahora=AHORA
            )  # fmt: skip
        async with tenant_session(tenant.id) as s:
            assert (await _cargar(s, Lead, lead_id)).status == "won"

    async def test_reabrir_a_la_vez_los_dos_ganados_desconvierte(self, tenant: Tenant) -> None:
        """Dos deals ganados de un lead reabiertos a la vez: el lead debe volver a `active`.

        Sin bloquear el lead, cada transaccion veria al otro deal aun ganado y ninguna lo
        devolveria a `active`.
        """
        lead_id = await _lead(tenant.id)
        a, b = await _deal(tenant, lead_id), await _deal(tenant, lead_id)
        async with tenant_session(tenant.id) as s:
            for deal_id in (a, b):
                await cambiar_etapa(s, await _cargar(s, Deal, deal_id), "closed_won", ahora=AHORA)

        async def reabrir(deal_id: uuid.UUID) -> None:
            async with tenant_session(tenant.id) as s:
                await cambiar_etapa(s, await _cargar(s, Deal, deal_id), "proposal", ahora=AHORA)

        async with tenant_session(tenant.id) as primera:
            await cambiar_etapa(
                primera, await _cargar(primera, Deal, a), "proposal", ahora=AHORA
            )  # fmt: skip
            segunda = asyncio.create_task(reabrir(b))
            await asyncio.sleep(0.5)
            assert not segunda.done(), "el segundo cambio no espero al bloqueo del lead"
        await segunda
        async with tenant_session(tenant.id) as s:
            assert (await _cargar(s, Lead, lead_id)).status == "active"

    @pytest.mark.parametrize(
        ("sql", "restriccion"),
        [
            ("UPDATE deals SET stage = 'closed_won' WHERE id = :d", "ck_deals_won_at"),
            ("UPDATE deals SET won_at = now(), lost_at = now() WHERE id = :d", "won_xor_lost"),
            ("UPDATE deals SET currency = 'usd' WHERE id = :d", "ck_deals_currency"),
            ("UPDATE deals SET value = -1 WHERE id = :d", "ck_deals_value"),
            ("UPDATE deals SET probability = 101 WHERE id = :d", "ck_deals_probability"),
        ],
    )
    async def test_checks_de_la_base(self, tenant: Tenant, sql: str, restriccion: str) -> None:
        deal_id = await _deal(tenant, await _lead(tenant.id))
        async with tenant_session(tenant.id) as s:
            with pytest.raises(IntegrityError, match=restriccion):
                await s.execute(text(sql), {"d": str(deal_id)})

    async def test_un_lead_con_deals_no_se_borra_en_cascada(self, tenant: Tenant) -> None:
        lead_id = await _lead(tenant.id)
        await _deal(tenant, lead_id)
        async with tenant_session(tenant.id) as s:
            with pytest.raises(IntegrityError):
                await s.execute(text("DELETE FROM leads WHERE id = :i"), {"i": str(lead_id)})


class TestPipeline:
    async def test_sql_igual_que_python(self, tenant: Tenant) -> None:
        lead_id = await _lead(tenant.id)
        for etapa, valor, moneda in [
            ("new_contact", "100.05", "USD"),
            ("proposal", "333.33", "USD"),
            ("proposal", "0.05", "USD"),
            ("proposal", "1500000", "COP"),
            ("negotiation", "2000", "EUR"),
        ]:
            await _deal(tenant, lead_id, stage=etapa, value=Decimal(valor), currency=moneda)
        perdido = await _deal(tenant, lead_id)
        async with tenant_session(tenant.id) as s:
            await cambiar_etapa(s, await _cargar(s, Deal, perdido), "closed_lost", ahora=AHORA)
        ahora = AHORA + timedelta(days=3)
        async with tenant_session(tenant.id) as s:
            sql = await resumen_pipeline(s, tenant.id, ahora=ahora)
            todos = (await s.execute(select(Deal).where(Deal.stage != "closed_lost"))).scalars()
            python = resumir_pipeline(list(todos), ahora=ahora)
        clave = [(f.stage, f.currency, f.count, f.total_value, f.weighted_value) for f in sql]
        assert clave == [
            (f.stage, f.currency, f.count, f.total_value, f.weighted_value) for f in python
        ]
        assert [f.avg_days_in_stage for f in sql] == [f.avg_days_in_stage for f in python]
        assert ("closed_lost", "USD") not in {(f.stage, f.currency) for f in sql}
        usd_propuesta = next(f for f in sql if (f.stage, f.currency) == ("proposal", "USD"))
        # 333.33 x 50% = 166.665 -> 166.67 y 0.05 x 50% = 0.025 -> 0.03: redondeo por deal.
        assert usd_propuesta.weighted_value == Decimal("166.70")

    async def test_ingresos_por_mes_y_moneda(self, tenant: Tenant) -> None:
        lead_id = await _lead(tenant.id)
        for valor, moneda, cuando in [
            ("100", "USD", datetime(2026, 9, 30, 12, tzinfo=timezone.utc)),
            ("200", "USD", datetime(2026, 10, 2, 12, tzinfo=timezone.utc)),
            ("300", "USD", datetime(2026, 10, 3, 12, tzinfo=timezone.utc)),
            ("5000", "COP", datetime(2026, 10, 3, 12, tzinfo=timezone.utc)),
        ]:
            deal_id = await _deal(tenant, lead_id, value=Decimal(valor), currency=moneda)
            async with tenant_session(tenant.id) as s:
                await cambiar_etapa(s, await _cargar(s, Deal, deal_id), "closed_won", ahora=cuando)
        async with tenant_session(tenant.id) as s:
            filas = await ingresos_por_mes(
                s, tenant.id, desde=date(2026, 10, 1), hasta=date(2026, 10, 31)
            )
        assert filas == [
            {"month": "2026-10", "currency": "COP", "deals": 1, "revenue": Decimal("5000.00")},
            {"month": "2026-10", "currency": "USD", "deals": 2, "revenue": Decimal("500.00")},
        ]


# ─── Llamadas ───────────────────────────────────────────────────────────────────────────────


class TestLlamadas:
    async def test_agendar_con_la_zona_del_negocio(self, tenant: Tenant) -> None:
        lead_id = await _lead(tenant.id)
        llamada_id = await _agendar(tenant, lead_id, AHORA + timedelta(days=1))
        async with tenant_session(tenant.id) as s:
            llamada = await _cargar(s, ScheduledCall, llamada_id)
        assert (llamada.status, llamada.timezone) == ("pending", "America/Bogota")

    async def test_no_en_el_pasado(self, tenant: Tenant) -> None:
        with pytest.raises(CallError, match="futuro"):
            await _agendar(tenant, await _lead(tenant.id), AHORA - timedelta(minutes=1))

    async def test_deal_de_otro_lead(self, tenant: Tenant) -> None:
        deal_ajeno = await _deal(tenant, await _lead(tenant.id))
        with pytest.raises(CallError) as exc:
            await _agendar(
                tenant, await _lead(tenant.id), AHORA + timedelta(days=1), deal_id=deal_ajeno
            )
        assert exc.value.code == "deal_mismatch"

    async def test_sin_doble_reserva(self, tenant: Tenant) -> None:
        lead_id = await _lead(tenant.id)
        inicio = AHORA + timedelta(days=1)
        await _agendar(tenant, lead_id, inicio, duration_minutes=60)
        with pytest.raises(CallError) as exc:
            await _agendar(tenant, lead_id, inicio + timedelta(minutes=30))
        assert exc.value.code == "overlap"
        # Otra persona, o justo al terminar: vale.
        await _agendar(
            tenant, lead_id, inicio + timedelta(minutes=30), assigned_user_id=tenant.beto
        )
        await _agendar(tenant, lead_id, inicio + timedelta(minutes=60))

    async def test_doble_reserva_concurrente(self, tenant: Tenant) -> None:
        """Mientras una reserva no termina, otra de la misma persona espera y luego ve el choque.

        Sin el bloqueo por usuario, la segunda transaccion no veria la primera (aun sin commit)
        y las dos llamadas se guardarian solapadas.
        """
        lead_id = await _lead(tenant.id)
        inicio = AHORA + timedelta(days=2)
        async with tenant_session(tenant.id) as primera:
            await agendar(
                primera, await _cargar(primera, Lead, lead_id),
                _cita(lead_id, inicio, assigned_user_id=tenant.ana), ahora=AHORA,
            )  # fmt: skip
            segunda = asyncio.create_task(_agendar(tenant, lead_id, inicio + timedelta(minutes=10)))
            await asyncio.sleep(0.5)
            assert not segunda.done(), "la segunda reserva no espero al bloqueo"
        with pytest.raises(CallError) as exc:
            await segunda
        assert exc.value.code == "overlap"

    async def test_una_cancelada_libera_el_hueco(self, tenant: Tenant) -> None:
        lead_id = await _lead(tenant.id)
        inicio = AHORA + timedelta(days=1)
        llamada_id = await _agendar(tenant, lead_id, inicio)
        async with tenant_session(tenant.id) as s:
            cambiar_estado(s, await _cargar(s, ScheduledCall, llamada_id), "cancelled", ahora=AHORA)
        await _agendar(tenant, lead_id, inicio)

    async def test_huecos_descuentan_lo_ocupado(self, tenant: Tenant) -> None:
        lead_id = await _lead(tenant.id)
        manana_9 = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc)  # martes 09:00 Bogota
        await _agendar(tenant, lead_id, manana_9)
        async with tenant_session(tenant.id) as s:
            huecos = await huecos_para(
                s, tenant.id, user_id=tenant.ana, desde=manana_9 - timedelta(hours=1),
                hasta=manana_9 + timedelta(hours=2), duracion_minutos=30, ahora=AHORA,
            )  # fmt: skip
            de_beto = await huecos_para(
                s, tenant.id, user_id=tenant.beto, desde=manana_9 - timedelta(hours=1),
                hasta=manana_9 + timedelta(hours=2), duracion_minutos=30, ahora=AHORA,
            )  # fmt: skip
        assert manana_9 not in huecos
        assert manana_9 in de_beto
        assert manana_9 + timedelta(minutes=30) in huecos

    async def test_ciclo_confirmar_completar(self, tenant: Tenant) -> None:
        lead_id = await _lead(tenant.id)
        llamada_id = await _agendar(tenant, lead_id, AHORA + timedelta(days=1))
        async with tenant_session(tenant.id) as s:
            llamada = await _cargar(s, ScheduledCall, llamada_id)
            cambiar_estado(s, llamada, "confirmed", ahora=AHORA)
            await completar(s, llamada, outcome="interested", ahora=AHORA, notes="Quiere demo")
        async with tenant_session(tenant.id) as s:
            llamada = await _cargar(s, ScheduledCall, llamada_id)
            assert (llamada.status, llamada.outcome, llamada.notes) == (
                "completed", "interested", "Quiere demo",
            )  # fmt: skip
            assert llamada.confirmed_at == llamada.completed_at == AHORA
            with pytest.raises(EstadoInvalidoError):
                cambiar_estado(s, llamada, "cancelled", ahora=AHORA)

    async def test_reprogramar_crea_otra_y_conserva_la_vieja(self, tenant: Tenant) -> None:
        lead_id = await _lead(tenant.id)
        inicio = AHORA + timedelta(days=1)
        vieja_id = await _agendar(tenant, lead_id, inicio, notes="Llamar al fijo")
        async with tenant_session(tenant.id) as s:
            vieja = await _cargar(s, ScheduledCall, vieja_id)
            # Moverla 15 minutos no choca consigo misma.
            nueva = await reprogramar(
                s, vieja, ScheduledCallReschedule(scheduled_at=inicio + timedelta(minutes=15)),
                ahora=AHORA,
            )  # fmt: skip
            nueva_id = nueva.id
        async with tenant_session(tenant.id) as s:
            vieja = await _cargar(s, ScheduledCall, vieja_id)
            nueva = await _cargar(s, ScheduledCall, nueva_id)
        assert vieja.status == "rescheduled"
        assert (nueva.status, nueva.notes, nueva.reminder_24h_sent_at) == (
            "pending", "Llamar al fijo", None,
        )  # fmt: skip

    async def test_recordatorios(self, tenant: Tenant) -> None:
        lead_id = await _lead(tenant.id)
        en_20h = await _agendar(tenant, lead_id, AHORA + timedelta(hours=26))
        en_30m = await _agendar(
            tenant, lead_id, AHORA + timedelta(days=3), assigned_user_id=tenant.beto
        )
        lejana = await _agendar(tenant, lead_id, AHORA + timedelta(days=10))
        async with tenant_session(tenant.id) as s:
            # Mover la segunda para que falte media hora "ahora" (sin pasar por agendar).
            await s.execute(
                text("UPDATE scheduled_calls SET scheduled_at = :t WHERE id = :i"),
                {"t": AHORA + timedelta(hours=6, minutes=30), "i": str(en_30m)},
            )
        momento = AHORA + timedelta(hours=6)
        async with tenant_session(tenant.id) as s:
            pendientes = await recordatorios_pendientes(s, tenant.id, momento)
        assert {(llamada.id, cual) for llamada, cual in pendientes} == {
            (en_20h, RECORDATORIO_24H),
            (en_30m, RECORDATORIO_1H),
        }
        assert lejana not in {llamada.id for llamada, _ in pendientes}

    @pytest.mark.parametrize(
        ("sql", "restriccion"),
        [
            ("UPDATE scheduled_calls SET status = 'completed' WHERE id = :i", "completed_at"),
            ("UPDATE scheduled_calls SET status = 'cancelled' WHERE id = :i", "cancelled_at"),
            ("UPDATE scheduled_calls SET duration_minutes = 600 WHERE id = :i", "duration"),
            ("UPDATE scheduled_calls SET call_type = 'ai_voice' WHERE id = :i", "ai_provider"),
        ],
    )
    async def test_checks_de_la_base(self, tenant: Tenant, sql: str, restriccion: str) -> None:
        llamada_id = await _agendar(tenant, await _lead(tenant.id), AHORA + timedelta(days=1))
        async with tenant_session(tenant.id) as s:
            with pytest.raises(IntegrityError, match=restriccion):
                await s.execute(text(sql), {"i": str(llamada_id)})


# ─── RGPD ───────────────────────────────────────────────────────────────────────────────────


class TestRgpd:
    async def test_supresion_cancela_llamadas_y_quita_el_texto_libre(self, tenant: Tenant) -> None:
        lead_id = await _lead(tenant.id, email="ana@example.com")
        ajeno = await _lead(tenant.id, email="otro@example.com")
        deal_id = await _deal(tenant, lead_id, title="Propuesta para Ana Perez", notes="Su hija")
        async with tenant_session(tenant.id) as s:
            await cambiar_etapa(
                s, await _cargar(s, Deal, deal_id), "closed_lost", ahora=AHORA,
                motivo_perdida="Ana se mudo a Lima",
            )  # fmt: skip
        futura = await _agendar(tenant, lead_id, AHORA + timedelta(days=2), notes="Tel. del jefe")
        de_otro = await _agendar(
            tenant, ajeno, AHORA + timedelta(days=4), assigned_user_id=tenant.beto
        )
        async with tenant_session(tenant.id) as s:
            await suprimir_leads(
                s, tenant.id, [await _cargar(s, Lead, lead_id)], user_id=tenant.ana, via="lead",
                ahora=AHORA,
            )  # fmt: skip
        async with tenant_session(tenant.id) as s:
            deal = await _cargar(s, Deal, deal_id)
            llamada = await _cargar(s, ScheduledCall, futura)
            otra = await _cargar(s, ScheduledCall, de_otro)
        assert (deal.title, deal.notes, deal.lost_reason) == (ANONIMIZADO, None, ANONIMIZADO)
        assert (deal.value, deal.stage) == (Decimal("0.00"), "closed_lost")  # negocio intacto
        assert (llamada.status, llamada.notes) == ("cancelled", None)
        assert otra.status == "pending"

    async def test_export(self, tenant: Tenant) -> None:
        lead_id = await _lead(tenant.id)
        await _deal(tenant, lead_id, notes="Le interesa el plan anual")
        await _agendar(tenant, lead_id, AHORA + timedelta(days=1))
        async with tenant_session(tenant.id) as s:
            deals = await deals_para_export(s, tenant.id, [lead_id])
            llamadas = await llamadas_para_export(s, tenant.id, [lead_id])
        assert [d["notes"] for d in deals[lead_id]] == ["Le interesa el plan anual"]
        assert [ll["status"] for ll in llamadas[lead_id]] == ["pending"]
