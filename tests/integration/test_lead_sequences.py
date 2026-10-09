"""Secuencias de follow-up contra PostgreSQL real con RLS (Sprint 18, slice Dev A).

Requiere base de datos: `pytest tests/ --run-db`. Lo que un doble no puede probar: que las tres
tablas aislen por tenant (lectura y `WITH CHECK`), el UNIQUE parcial de inscripciones vivas, el
UNIQUE diferible de posiciones, que `inscripciones_pendientes` bloquee filas, que la foto del
lead salga de sus mensajes, identificadores y actividad reales, y un recorrido completo del motor.
"""

import json
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.database import tenant_session
from app.models.contact import Contact
from app.models.contact_identifier import ContactIdentifier
from app.models.conversation import Conversation
from app.models.lead import Lead
from app.models.lead_activity import ACTIVITY_EMAIL_REPLIED, ACTIVITY_LINK_CLICKED, LeadActivity
from app.models.lead_sequence import (
    ENROLLMENT_COMPLETED,
    ENROLLMENT_EXITED,
    EXIT_NEGATIVE_REPLY,
    EXIT_REPLIED,
    EXIT_UNSUBSCRIBED,
    LeadSequence,
    LeadSequenceEnrollment,
    LeadSequenceStep,
)
from app.models.message import Message
from app.schemas.lead_sequence import (
    Branch,
    ConditionStep,
    MessageStep,
    SequenceDefinition,
    TaskStep,
    TriggerConditions,
    WaitStep,
)
from app.services.lead_activity import registrar_actividad
from app.services.lead_sequence_engine import (
    Complete,
    CreateTask,
    Exit,
    SendMessage,
    Wait,
    decidir,
)
from app.services.lead_sequence_templates import PLANTILLAS
from app.services.lead_sequence_timing import programar_siguiente, ventana_del_tenant
from app.services.lead_sequences import (
    PasosInvalidosError,
    SecuenciaError,
    YaInscritoError,
    cargar_pasos,
    crear_secuencia,
    horas_de_respuesta,
    inscribir,
    inscribir_por_evento,
    inscripciones_pendientes,
    reemplazar_pasos,
    registrar_avance,
    salir_de_secuencias,
    snapshot_del_lead,
)
from tests.integration.lead_helpers import crear_etapa, crear_fuente, limpiar_leads

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

PERFIL = {"timezone": "America/Bogota", "business_name": "Clinica Sol"}


async def _crear_tenant() -> uuid.UUID:
    client_id = uuid.uuid4()
    async with tenant_session(client_id) as s:
        await s.execute(
            text(
                "INSERT INTO clients (id, name, slug, plan, lead_management_enabled, settings) "
                "VALUES (:id, 'T', :slug, 'free', true, CAST(:settings AS jsonb))"
            ),
            {
                "id": str(client_id),
                "slug": f"seq-{client_id.hex[:10]}",
                "settings": json.dumps({"business_profile": PERFIL}),
            },
        )
    return client_id


async def _borrar_tenant(client_id: uuid.UUID) -> None:
    await limpiar_leads(client_id)
    async with tenant_session(client_id) as s:
        for tabla in ("messages", "conversations", "contact_identifiers", "contacts"):
            await s.execute(
                text(f"DELETE FROM {tabla} WHERE client_id = :c"),  # noqa: S608
                {"c": str(client_id)},
            )
        await s.execute(text("DELETE FROM clients WHERE id = :c"), {"c": str(client_id)})


@pytest_asyncio.fixture
async def tenant() -> AsyncGenerator[uuid.UUID, None]:
    client_id = await _crear_tenant()
    yield client_id
    await _borrar_tenant(client_id)


@pytest_asyncio.fixture
async def otro_tenant() -> AsyncGenerator[uuid.UUID, None]:
    client_id = await _crear_tenant()
    yield client_id
    await _borrar_tenant(client_id)


def _definicion(nombre: str = "Seguimiento", **kw: Any) -> SequenceDefinition:
    kw.setdefault(
        "steps",
        [
            MessageStep(body="Hola {{first_name}}"),
            WaitStep(amount=2, unit="days"),
            ConditionStep(check="replied", if_true=Branch(action="exit")),
            TaskStep(title="Llamar"),
        ],
    )
    return SequenceDefinition(name=nombre, **kw)


async def _secuencia(client_id: uuid.UUID, **kw: Any) -> uuid.UUID:
    async with tenant_session(client_id) as s:
        return (await crear_secuencia(s, client_id, _definicion(**kw))).id


async def _lead(client_id: uuid.UUID, **campos: Any) -> uuid.UUID:
    campos.setdefault("first_name", "Ana")
    campos.setdefault("phone", f"+57300{uuid.uuid4().int % 10_000_000:07d}")
    async with tenant_session(client_id) as s:
        lead = Lead(client_id=client_id, **campos)
        s.add(lead)
        await s.flush()
        return lead.id


async def _cargar(s: Any, modelo: Any, id_: uuid.UUID) -> Any:
    return (await s.execute(select(modelo).where(modelo.id == id_))).scalar_one()


async def _inscribir(
    client_id: uuid.UUID, lead_id: uuid.UUID, sequence_id: uuid.UUID, **kw: Any
) -> uuid.UUID:
    """Inscribe con el primer paso para ya (salvo que se pida otra cosa): los tests no pueden
    depender de la hora a la que corren. El valor por defecto real se prueba aparte."""
    ahora = kw.pop("ahora", datetime.now(timezone.utc))
    kw.setdefault("next_step_at", ahora)
    async with tenant_session(client_id) as s:
        inscripcion = await inscribir(
            s,
            await _cargar(s, Lead, lead_id),
            await _cargar(s, LeadSequence, sequence_id),
            ahora=ahora,
            **kw,
        )
        return inscripcion.id


# ─── RLS ────────────────────────────────────────────────────────────────────────────────────


class TestAislamiento:
    async def test_un_tenant_no_ve_las_secuencias_de_otro(
        self, tenant: uuid.UUID, otro_tenant: uuid.UUID
    ) -> None:
        secuencia = await _secuencia(tenant)
        await _inscribir(tenant, await _lead(tenant), secuencia)
        async with tenant_session(otro_tenant) as s:
            for modelo in (LeadSequence, LeadSequenceStep, LeadSequenceEnrollment):
                total = (await s.execute(select(func.count()).select_from(modelo))).scalar()
                assert total == 0, modelo.__tablename__
            # Ni siquiera sabiendo el id: los pasos de otro tenant no existen para el.
            with pytest.raises(PasosInvalidosError):
                await cargar_pasos(s, otro_tenant, secuencia)

    @pytest.mark.parametrize("tabla", ["lead_sequences", "lead_sequence_steps"])
    async def test_with_check_impide_escribir_en_otro_tenant(
        self, tenant: uuid.UUID, otro_tenant: uuid.UUID, tabla: str
    ) -> None:
        secuencia = await _secuencia(tenant)
        async with tenant_session(otro_tenant) as s:
            if tabla == "lead_sequences":
                s.add(LeadSequence(client_id=tenant, name="Intrusa"))
            else:
                s.add(
                    LeadSequenceStep(
                        client_id=tenant, sequence_id=secuencia, position=9, step_type="task",
                        config={"title": "x"},
                    )
                )  # fmt: skip
            with pytest.raises(DBAPIError):
                await s.flush()

    async def test_with_check_en_inscripciones(
        self, tenant: uuid.UUID, otro_tenant: uuid.UUID
    ) -> None:
        secuencia = await _secuencia(tenant)
        lead = await _lead(tenant)
        async with tenant_session(otro_tenant) as s:
            s.add(LeadSequenceEnrollment(client_id=tenant, lead_id=lead, sequence_id=secuencia))
            with pytest.raises(DBAPIError):
                await s.flush()

    async def test_rls_forzada_en_las_tres_tablas(self, tenant: uuid.UUID) -> None:
        async with tenant_session(tenant) as s:
            filas = (
                await s.execute(
                    text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname IN ('lead_sequences', 'lead_sequence_steps', "
                        "'lead_sequence_enrollments')"
                    )
                )
            ).all()
        assert sorted(filas) == [
            ("lead_sequence_enrollments", True, True),
            ("lead_sequence_steps", True, True),
            ("lead_sequences", True, True),
        ]


# ─── Secuencias y pasos ─────────────────────────────────────────────────────────────────────


class TestSecuencias:
    async def test_crear_y_cargar_pasos(self, tenant: uuid.UUID) -> None:
        definicion = _definicion()
        async with tenant_session(tenant) as s:
            secuencia = await crear_secuencia(s, tenant, definicion)
        async with tenant_session(tenant) as s:
            assert await cargar_pasos(s, tenant, secuencia.id) == definicion.steps
            guardada = await _cargar(s, LeadSequence, secuencia.id)
            assert guardada.channel_priority == ["whatsapp", "email", "instagram"]
            assert guardada.trigger_conditions["events"] == []

    async def test_nombre_unico_por_tenant(self, tenant: uuid.UUID, otro_tenant: uuid.UUID) -> None:
        await _secuencia(tenant)
        await _secuencia(otro_tenant)  # otro tenant, mismo nombre: vale
        async with tenant_session(tenant) as s:
            with pytest.raises(SecuenciaError) as exc:
                await crear_secuencia(s, tenant, _definicion())
            assert exc.value.code == "duplicate_name"

    async def test_plantillas_predefinidas_se_guardan(self, tenant: uuid.UUID) -> None:
        async with tenant_session(tenant) as s:
            for definicion in PLANTILLAS.values():
                secuencia = await crear_secuencia(s, tenant, definicion)
                assert await cargar_pasos(s, tenant, secuencia.id) == definicion.steps

    async def test_reemplazar_pasos(self, tenant: uuid.UUID) -> None:
        secuencia_id = await _secuencia(tenant)
        nueva = _definicion(steps=[TaskStep(title="Solo llamar")])
        async with tenant_session(tenant) as s:
            await reemplazar_pasos(s, tenant, await _cargar(s, LeadSequence, secuencia_id), nueva)
        async with tenant_session(tenant) as s:
            assert await cargar_pasos(s, tenant, secuencia_id) == nueva.steps

    async def test_posiciones_intercambiables_en_una_transaccion(self, tenant: uuid.UUID) -> None:
        secuencia_id = await _secuencia(tenant)
        async with tenant_session(tenant) as s:
            # UNIQUE diferible: el estado intermedio (dos pasos en la posicion 2) no rompe.
            await s.execute(
                text(
                    "UPDATE lead_sequence_steps SET position = CASE position WHEN 1 THEN 2 "
                    "ELSE 1 END WHERE sequence_id = :s AND position IN (1, 2)"
                ),
                {"s": str(secuencia_id)},
            )
        async with tenant_session(tenant) as s:
            pasos = await cargar_pasos(s, tenant, secuencia_id)
        assert isinstance(pasos[0], WaitStep)

    async def test_pasos_rotos_en_la_base(self, tenant: uuid.UUID) -> None:
        secuencia_id = await _secuencia(tenant)
        async with tenant_session(tenant) as s:
            await s.execute(
                text(
                    "UPDATE lead_sequence_steps SET config = '{\"amount\": 0}' "
                    "WHERE sequence_id = :s AND position = 2"
                ),
                {"s": str(secuencia_id)},
            )
        async with tenant_session(tenant) as s:
            with pytest.raises(PasosInvalidosError, match="paso 2"):
                await cargar_pasos(s, tenant, secuencia_id)

    async def test_hueco_en_las_posiciones(self, tenant: uuid.UUID) -> None:
        secuencia_id = await _secuencia(tenant)
        async with tenant_session(tenant) as s:
            await s.execute(
                text("DELETE FROM lead_sequence_steps WHERE sequence_id = :s AND position = 2"),
                {"s": str(secuencia_id)},
            )
        async with tenant_session(tenant) as s:
            with pytest.raises(PasosInvalidosError, match="falta el paso 2"):
                await cargar_pasos(s, tenant, secuencia_id)

    async def test_secuencia_con_inscripciones_no_se_borra(self, tenant: uuid.UUID) -> None:
        secuencia_id = await _secuencia(tenant)
        await _inscribir(tenant, await _lead(tenant), secuencia_id)
        async with tenant_session(tenant) as s:
            with pytest.raises(IntegrityError):
                await s.execute(
                    text("DELETE FROM lead_sequences WHERE id = :s"), {"s": str(secuencia_id)}
                )


# ─── Inscripcion ────────────────────────────────────────────────────────────────────────────


class TestInscripcion:
    async def test_inscribe_y_anota_actividad(self, tenant: uuid.UUID) -> None:
        secuencia_id, lead_id = await _secuencia(tenant), await _lead(tenant)
        inscripcion_id = await _inscribir(tenant, lead_id, secuencia_id)
        async with tenant_session(tenant) as s:
            inscripcion = await _cargar(s, LeadSequenceEnrollment, inscripcion_id)
            assert (inscripcion.status, inscripcion.current_step) == ("active", 1)
            assert inscripcion.next_step_at == inscripcion.created_at
            actividad = (
                await s.execute(select(LeadActivity).where(LeadActivity.lead_id == lead_id))
            ).scalar_one()
        assert actividad.activity_type == "sequence_enrolled"
        assert actividad.metadata_["sequence_id"] == str(secuencia_id)

    async def test_no_dos_veces_mientras_este_viva(self, tenant: uuid.UUID) -> None:
        secuencia_id, lead_id = await _secuencia(tenant), await _lead(tenant)
        await _inscribir(tenant, lead_id, secuencia_id)
        with pytest.raises(YaInscritoError):
            await _inscribir(tenant, lead_id, secuencia_id)
        # Una vez fuera, puede volver a entrar (una reactivacion meses despues).
        async with tenant_session(tenant) as s:
            assert await salir_de_secuencias(
                s, tenant, [lead_id], "manual", ahora=datetime.now(timezone.utc)
            ) == 1  # fmt: skip
        await _inscribir(tenant, lead_id, secuencia_id)

    async def test_el_unique_parcial_es_de_la_base(self, tenant: uuid.UUID) -> None:
        secuencia_id, lead_id = await _secuencia(tenant), await _lead(tenant)
        await _inscribir(tenant, lead_id, secuencia_id)
        async with tenant_session(tenant) as s:
            s.add(
                LeadSequenceEnrollment(
                    client_id=tenant, lead_id=lead_id, sequence_id=secuencia_id, status="paused"
                )
            )
            with pytest.raises(IntegrityError):
                await s.flush()

    @pytest.mark.parametrize(
        ("campos", "secuencia_activa", "motivo"),
        [
            ({"status": "won"}, True, "lead_closed"),
            ({"deleted_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}, True, "lead_deleted"),
            ({"first_name": "[ELIMINADO]", "phone": None}, True, "gdpr"),
            ({}, False, "sequence_disabled"),
        ],
    )
    async def test_no_inscribible(
        self, tenant: uuid.UUID, campos: dict[str, Any], secuencia_activa: bool, motivo: str
    ) -> None:
        secuencia_id, lead_id = await _secuencia(tenant), await _lead(tenant, **campos)
        if not secuencia_activa:
            async with tenant_session(tenant) as s:
                (await _cargar(s, LeadSequence, secuencia_id)).is_active = False
        with pytest.raises(SecuenciaError, match=motivo):
            await _inscribir(tenant, lead_id, secuencia_id)

    async def test_cerrada_necesita_fecha(self, tenant: uuid.UUID) -> None:
        secuencia_id, lead_id = await _secuencia(tenant), await _lead(tenant)
        inscripcion_id = await _inscribir(tenant, lead_id, secuencia_id)
        async with tenant_session(tenant) as s:
            with pytest.raises(IntegrityError):
                await s.execute(
                    text("UPDATE lead_sequence_enrollments SET status = 'exited' WHERE id = :i"),
                    {"i": str(inscripcion_id)},
                )

    async def test_salir_de_una_sola_secuencia(self, tenant: uuid.UUID) -> None:
        a, b = await _secuencia(tenant, nombre="A"), await _secuencia(tenant, nombre="B")
        lead_id = await _lead(tenant)
        await _inscribir(tenant, lead_id, a)
        await _inscribir(tenant, lead_id, b)
        ahora = datetime.now(timezone.utc)
        async with tenant_session(tenant) as s:
            assert await salir_de_secuencias(
                s, tenant, [lead_id], EXIT_REPLIED, ahora=ahora, sequence_id=a
            ) == 1  # fmt: skip
        async with tenant_session(tenant) as s:
            estados = dict(
                (
                    await s.execute(
                        select(LeadSequenceEnrollment.sequence_id, LeadSequenceEnrollment.status)
                    )
                )
                .tuples()
                .all()
            )
        assert estados == {a: ENROLLMENT_EXITED, b: "active"}
        # La ya cerrada no se vuelve a tocar: solo sale la viva.
        async with tenant_session(tenant) as s:
            assert await salir_de_secuencias(s, tenant, [lead_id], "manual", ahora=ahora) == 1


# ─── Disparadores ───────────────────────────────────────────────────────────────────────────


class TestProteccionDelLead:
    """Lo que encontro la revision del PR #66: a quien no quiere mensajes no se le escriben."""

    async def test_la_bienvenida_espera_a_que_abra_el_negocio(self, tenant: uuid.UUID) -> None:
        secuencia_id, lead_id = await _secuencia(tenant), await _lead(tenant)
        madrugada = datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)  # martes 03:00 en Bogota
        async with tenant_session(tenant) as s:
            inscripcion = await inscribir(
                s,
                await _cargar(s, Lead, lead_id),
                await _cargar(s, LeadSequence, secuencia_id),
                ahora=madrugada,
            )
            # 08:00 en Bogota = 13:00 UTC.
            assert inscripcion.next_step_at == datetime(2026, 10, 6, 13, 0, tzinfo=timezone.utc)

    @pytest.mark.parametrize("motivo", [EXIT_UNSUBSCRIBED, EXIT_NEGATIVE_REPLY])
    async def test_quien_se_dio_de_baja_no_vuelve_a_entrar(
        self, tenant: uuid.UUID, motivo: str
    ) -> None:
        a, b = await _secuencia(tenant, nombre="A"), await _secuencia(tenant, nombre="B")
        lead_id = await _lead(tenant)
        await _inscribir(tenant, lead_id, a)
        async with tenant_session(tenant) as s:
            await salir_de_secuencias(
                s, tenant, [lead_id], motivo, ahora=datetime.now(timezone.utc)
            )
        # Ni en la misma, ni en otra, ni a mano.
        for secuencia in (a, b):
            with pytest.raises(SecuenciaError) as exc:
                await _inscribir(tenant, lead_id, secuencia)
            assert exc.value.code == "opted_out"

    async def test_otro_motivo_de_salida_si_deja_reinscribir_a_mano(
        self, tenant: uuid.UUID
    ) -> None:
        secuencia_id, lead_id = await _secuencia(tenant), await _lead(tenant)
        await _inscribir(tenant, lead_id, secuencia_id)
        async with tenant_session(tenant) as s:
            await salir_de_secuencias(
                s, tenant, [lead_id], EXIT_REPLIED, ahora=datetime.now(timezone.utc)
            )
        await _inscribir(tenant, lead_id, secuencia_id)

    async def test_un_disparador_no_reinscribe_en_una_secuencia_ya_recorrida(
        self, tenant: uuid.UUID
    ) -> None:
        secuencia_id = await _secuencia(
            tenant, trigger_conditions=TriggerConditions(events=["score_changed"])
        )
        lead_id = await _lead(tenant)
        async with tenant_session(tenant) as s:
            lead = await _cargar(s, Lead, lead_id)
            [primera] = await inscribir_por_evento(
                s, lead, "score_changed", ahora=datetime.now(timezone.utc)
            )
            await salir_de_secuencias(
                s, tenant, [lead_id], EXIT_REPLIED, ahora=datetime.now(timezone.utc)
            )
        # Contestar cambia su score: el disparador vuelve a cumplirse, pero no debe reinscribir.
        async with tenant_session(tenant) as s:
            lead = await _cargar(s, Lead, lead_id)
            assert await inscribir_por_evento(
                s, lead, "score_changed", ahora=datetime.now(timezone.utc)
            ) == []  # fmt: skip
        assert primera.sequence_id == secuencia_id

    async def test_un_error_de_fk_no_se_disfraza_de_ya_inscrito(self, tenant: uuid.UUID) -> None:
        secuencia_id, lead_id = await _secuencia(tenant), await _lead(tenant)
        with pytest.raises(IntegrityError) as exc:
            await _inscribir(tenant, lead_id, secuencia_id, user_id=uuid.uuid4())
        assert not isinstance(exc.value, YaInscritoError)

    async def test_respuesta_de_un_lead_sin_contacto(self, tenant: uuid.UUID) -> None:
        lead_id = await _lead(tenant, email="ana@example.com")
        inicio = datetime.now(timezone.utc)
        async with tenant_session(tenant) as s:
            registrar_actividad(s, client_id=tenant, lead_id=lead_id, tipo=ACTIVITY_EMAIL_REPLIED)
        async with tenant_session(tenant) as s:
            foto = await snapshot_del_lead(s, await _cargar(s, Lead, lead_id), desde=inicio)
        assert foto.replied is True


class TestDisparadores:
    async def test_inscribe_solo_en_las_que_cumplen(self, tenant: uuid.UUID) -> None:
        fuente = await crear_fuente(tenant, source_type="web_form")
        etapa = await crear_etapa(tenant, "new", 1)
        web = await _secuencia(
            tenant,
            nombre="Web",
            trigger_conditions=TriggerConditions(
                events=["lead_created"], source_types=["web_form"], stages=["new"]
            ),
        )
        await _secuencia(
            tenant,
            nombre="Calientes",
            trigger_conditions=TriggerConditions(events=["lead_created"], temperatures=["hot"]),
        )
        await _secuencia(tenant, nombre="Manual")
        apagada = await _secuencia(
            tenant, nombre="Apagada", trigger_conditions=TriggerConditions(events=["lead_created"])
        )
        async with tenant_session(tenant) as s:
            (await _cargar(s, LeadSequence, apagada)).is_active = False
        lead_id = await _lead(tenant, source_id=fuente, pipeline_stage_id=etapa)

        async with tenant_session(tenant) as s:
            lead = await _cargar(s, Lead, lead_id)
            nuevas = await inscribir_por_evento(
                s, lead, "lead_created", ahora=datetime.now(timezone.utc)
            )
            assert [n.sequence_id for n in nuevas] == [web]
            # Repetir el evento no duplica.
            assert await inscribir_por_evento(
                s, lead, "lead_created", ahora=datetime.now(timezone.utc)
            ) == []  # fmt: skip
            assert await inscribir_por_evento(
                s, lead, "stage_changed", ahora=datetime.now(timezone.utc)
            ) == []  # fmt: skip


# ─── Foto del lead y avance ─────────────────────────────────────────────────────────────────


async def _contacto_con_mensajes(
    client_id: uuid.UUID, mensajes: list[tuple[str, datetime]]
) -> uuid.UUID:
    async with tenant_session(client_id) as s:
        contacto = Contact(client_id=client_id, first_name="Ana")
        s.add(contacto)
        await s.flush()
        s.add(
            ContactIdentifier(
                client_id=client_id, contact_id=contacto.id, channel="instagram",
                identifier_value=f"ana_{uuid.uuid4().hex[:6]}",
            )
        )  # fmt: skip
        s.add(
            ContactIdentifier(
                client_id=client_id, contact_id=contacto.id, channel="webchat",
                identifier_value=uuid.uuid4().hex,
            )
        )  # fmt: skip
        conv = Conversation(client_id=client_id, contact_id=contacto.id, channel="instagram")
        s.add(conv)
        await s.flush()
        for direccion, cuando in mensajes:
            s.add(
                Message(
                    client_id=client_id, conversation_id=conv.id, direction=direccion,
                    message_type="text", content="x", created_at=cuando,
                    sender_type="contact" if direccion == "inbound" else "agent",
                )
            )  # fmt: skip
        return contacto.id


class TestSnapshot:
    async def test_lee_canales_respuestas_y_clics(self, tenant: uuid.UUID) -> None:
        ahora = datetime.now(timezone.utc)
        inicio = ahora - timedelta(days=1)
        contacto = await _contacto_con_mensajes(
            tenant, [("inbound", inicio - timedelta(hours=2)), ("outbound", ahora)]
        )
        etapa = await crear_etapa(tenant, "qualified", 1)
        lead_id = await _lead(
            tenant, email="ana@example.com", contact_id=contacto, pipeline_stage_id=etapa,
            total_score=42,
        )  # fmt: skip
        async with tenant_session(tenant) as s:
            lead = await _cargar(s, Lead, lead_id)
            foto = await snapshot_del_lead(s, lead, desde=inicio)
        # El mensaje entrante fue antes de la inscripcion; webchat no es canal de salida.
        assert foto.replied is False
        assert foto.channels == frozenset({"email", "whatsapp", "instagram"})
        assert (foto.stage_slug, foto.total_score) == ("qualified", 42)
        assert not foto.anonymized

        async with tenant_session(tenant) as s:
            conv = (await s.execute(select(Conversation.id))).scalar_one()
            s.add(
                Message(
                    client_id=tenant, conversation_id=conv, direction="inbound",
                    message_type="text", content="si", sender_type="contact", created_at=ahora,
                )
            )  # fmt: skip
            registrar_actividad(s, client_id=tenant, lead_id=lead_id, tipo=ACTIVITY_LINK_CLICKED)
        async with tenant_session(tenant) as s:
            foto = await snapshot_del_lead(s, await _cargar(s, Lead, lead_id), desde=inicio)
        assert (foto.replied, foto.link_clicked, foto.email_opened) == (True, True, False)

    async def test_no_ve_mensajes_de_otro_tenant(
        self, tenant: uuid.UUID, otro_tenant: uuid.UUID
    ) -> None:
        ahora = datetime.now(timezone.utc)
        ajeno = await _contacto_con_mensajes(otro_tenant, [("inbound", ahora)])
        # Un lead que apunta (mal) al contacto de otro tenant no hereda sus respuestas.
        lead_id = await _lead(tenant)
        async with tenant_session(tenant) as s:
            lead = await _cargar(s, Lead, lead_id)
            lead.contact_id = None
            foto = await snapshot_del_lead(s, lead, desde=ahora - timedelta(days=1))
            lead.contact_id = ajeno
            foto_ajena = await snapshot_del_lead(s, lead, desde=ahora - timedelta(days=1))
            assert await horas_de_respuesta(s, lead, ZoneInfo("America/Bogota")) == []
            await s.rollback()
        assert foto.replied is False
        assert foto_ajena.replied is False
        assert foto_ajena.channels == frozenset({"whatsapp"})


class TestRecorrido:
    async def test_mensaje_espera_condicion_y_tarea(self, tenant: uuid.UUID) -> None:
        """La tarea de Celery de Dev B en miniatura, contra la base real."""
        secuencia_id, lead_id = await _secuencia(tenant), await _lead(tenant)
        t0 = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)  # lunes 09:00 en Bogota
        inscripcion_id = await _inscribir(tenant, lead_id, secuencia_id, ahora=t0)

        async def turno(ahora: datetime) -> Any:
            async with tenant_session(tenant) as s:
                [inscripcion] = await inscripciones_pendientes(s, tenant, ahora)
                pasos = await cargar_pasos(s, tenant, secuencia_id)
                lead = await _cargar(s, Lead, lead_id)
                decision = decidir(
                    pasos=pasos,
                    current_step=inscripcion.current_step,
                    steps_executed=inscripcion.steps_executed,
                    lead=await snapshot_del_lead(s, lead, desde=inscripcion.created_at),
                    channel_priority=["whatsapp", "email"],
                )
                siguiente = None
                ventana = await ventana_del_tenant(s, tenant)
                if isinstance(decision.action, Wait):
                    siguiente = programar_siguiente(ahora, decision.action.step, ventana)
                registrar_avance(
                    s, inscripcion, decision, ahora=ahora, ventana=ventana, next_step_at=siguiente
                )
                return decision.action

        accion = await turno(t0)
        assert isinstance(accion, SendMessage)
        assert accion.channel == "whatsapp"
        assert isinstance(await turno(t0), Wait)

        # Antes de que venza la espera no hay nada pendiente.
        async with tenant_session(tenant) as s:
            assert await inscripciones_pendientes(s, tenant, t0 + timedelta(days=1)) == []

        # Condicion (no contesto) + tarea, en el mismo turno.
        accion = await turno(t0 + timedelta(days=2))
        assert isinstance(accion, CreateTask)
        assert isinstance(await turno(t0 + timedelta(days=2)), Complete)

        async with tenant_session(tenant) as s:
            inscripcion = await _cargar(s, LeadSequenceEnrollment, inscripcion_id)
            assert inscripcion.status == ENROLLMENT_COMPLETED
            assert inscripcion.steps_executed == 4
            tipos = (
                await s.execute(
                    select(LeadActivity.activity_type)
                    .where(LeadActivity.lead_id == lead_id)
                    .order_by(LeadActivity.created_at)
                )
            ).scalars()
            assert list(tipos) == ["sequence_enrolled", "sequence_completed"]

    async def test_lead_cerrado_sale_en_su_turno(self, tenant: uuid.UUID) -> None:
        secuencia_id, lead_id = await _secuencia(tenant), await _lead(tenant)
        inscripcion_id = await _inscribir(tenant, lead_id, secuencia_id)
        async with tenant_session(tenant) as s:
            (await _cargar(s, Lead, lead_id)).status = "lost"
        ahora = datetime.now(timezone.utc)
        async with tenant_session(tenant) as s:
            [inscripcion] = await inscripciones_pendientes(s, tenant, ahora)
            lead = await _cargar(s, Lead, lead_id)
            decision = decidir(
                pasos=await cargar_pasos(s, tenant, secuencia_id),
                current_step=1,
                steps_executed=0,
                lead=await snapshot_del_lead(s, lead, desde=inscripcion.created_at),
                channel_priority=["whatsapp"],
            )
            assert decision.action == Exit("lead_closed")
            registrar_avance(s, inscripcion, decision, ahora=ahora, ventana=None)
        async with tenant_session(tenant) as s:
            inscripcion = await _cargar(s, LeadSequenceEnrollment, inscripcion_id)
            assert (inscripcion.status, inscripcion.exit_reason) == ("exited", "lead_closed")
            assert inscripcion.completed_at is not None

    async def test_pendientes_se_reparten_entre_workers(self, tenant: uuid.UUID) -> None:
        secuencia_id = await _secuencia(tenant)
        for _ in range(3):
            await _inscribir(tenant, await _lead(tenant), secuencia_id)
        ahora = datetime.now(timezone.utc) + timedelta(seconds=1)
        async with tenant_session(tenant) as primero:
            mias = await inscripciones_pendientes(primero, tenant, ahora, limite=2)
            async with tenant_session(tenant) as segundo:
                suyas = await inscripciones_pendientes(segundo, tenant, ahora)
            assert len(mias) == 2
            assert len(suyas) == 1
            assert {i.id for i in mias}.isdisjoint({i.id for i in suyas})


# ─── Correcciones de la segunda revision ────────────────────────────────────────────────────


class TestCorreccionesSegundaRevision:
    async def test_una_baja_saca_de_todas_aunque_se_pida_una(self, tenant: uuid.UUID) -> None:
        a, b = await _secuencia(tenant, nombre="A"), await _secuencia(tenant, nombre="B")
        lead_id = await _lead(tenant)
        await _inscribir(tenant, lead_id, a)
        await _inscribir(tenant, lead_id, b)
        async with tenant_session(tenant) as s:
            cerradas = await salir_de_secuencias(
                s, tenant, [lead_id], EXIT_UNSUBSCRIBED,
                ahora=datetime.now(timezone.utc), sequence_id=a,
            )  # fmt: skip
        assert cerradas == 2

    async def test_el_motor_no_escribe_a_quien_se_dio_de_baja_en_otra(
        self, tenant: uuid.UUID
    ) -> None:
        """Aunque una inscripcion siga viva (p. ej. una carrera), su turno la saca."""
        a, b = await _secuencia(tenant, nombre="A"), await _secuencia(tenant, nombre="B")
        lead_id = await _lead(tenant)
        await _inscribir(tenant, lead_id, a)
        viva = await _inscribir(tenant, lead_id, b)
        async with tenant_session(tenant) as s:
            # Simula la carrera: la baja de A se escribe a mano, B queda activa.
            await s.execute(
                text(
                    "UPDATE lead_sequence_enrollments SET status = 'exited', "
                    "exit_reason = :m, completed_at = now(), next_step_at = NULL "
                    "WHERE sequence_id = :a"
                ),
                {"m": EXIT_UNSUBSCRIBED, "a": str(a)},
            )
        async with tenant_session(tenant) as s:
            foto = await snapshot_del_lead(
                s, await _cargar(s, Lead, lead_id), desde=datetime.now(timezone.utc)
            )
            assert foto.opted_out
            inscripcion = await _cargar(s, LeadSequenceEnrollment, viva)
            decision = decidir(
                pasos=await cargar_pasos(s, tenant, b),
                current_step=inscripcion.current_step,
                steps_executed=inscripcion.steps_executed,
                lead=foto,
                channel_priority=["whatsapp"],
            )
        assert decision.action == Exit(EXIT_UNSUBSCRIBED)

    async def test_la_supresion_no_espera_a_un_worker_que_tiene_la_fila(
        self, tenant: uuid.UUID
    ) -> None:
        secuencia_id, lead_id = await _secuencia(tenant), await _lead(tenant)
        await _inscribir(tenant, lead_id, secuencia_id)
        ahora = datetime.now(timezone.utc) + timedelta(seconds=1)
        async with tenant_session(tenant) as worker:
            assert len(await inscripciones_pendientes(worker, tenant, ahora)) == 1
            async with tenant_session(tenant) as rgpd:
                cerradas = await salir_de_secuencias(
                    rgpd, tenant, [lead_id], "gdpr", ahora=ahora, saltar_bloqueadas=True
                )
            assert cerradas == 0  # la saca el motor en su turno (lead anonimizado)

    async def test_una_activa_sin_next_step_at_la_rechaza_la_base(self, tenant: uuid.UUID) -> None:
        secuencia_id, lead_id = await _secuencia(tenant), await _lead(tenant)
        inscripcion_id = await _inscribir(tenant, lead_id, secuencia_id)
        async with tenant_session(tenant) as s:
            with pytest.raises(IntegrityError):
                await s.execute(
                    text("UPDATE lead_sequence_enrollments SET next_step_at = NULL WHERE id = :i"),
                    {"i": str(inscripcion_id)},
                )

    async def test_un_disparador_no_inscribe_a_quien_se_dio_de_baja(
        self, tenant: uuid.UUID
    ) -> None:
        a = await _secuencia(tenant, nombre="A")
        await _secuencia(
            tenant, nombre="Auto", trigger_conditions=TriggerConditions(events=["lead_created"])
        )
        lead_id = await _lead(tenant)
        await _inscribir(tenant, lead_id, a)
        async with tenant_session(tenant) as s:
            await salir_de_secuencias(
                s, tenant, [lead_id], EXIT_UNSUBSCRIBED, ahora=datetime.now(timezone.utc)
            )
        async with tenant_session(tenant) as s:
            nuevas = await inscribir_por_evento(
                s, await _cargar(s, Lead, lead_id), "lead_created", ahora=datetime.now(timezone.utc)
            )
        assert nuevas == []
