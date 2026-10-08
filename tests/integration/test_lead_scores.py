"""Scoring y enriquecimiento de leads contra PostgreSQL real con RLS (Sprint 17, slice Dev A).

Requiere base de datos: `pytest tests/ --run-db`. Lo que un doble no puede probar: que
`lead_scores` aisle por tenant (lectura y `WITH CHECK`), que aplicar un score deje columna,
total con los pesos del tenant, historial y actividad en la misma transaccion, que las senales de
comportamiento salgan de los mensajes reales del contacto, y que el enriquecimiento persista sin
pisar lo escrito y alimente el FIT.
"""

import json
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.core.database import tenant_session
from app.models.contact import Contact
from app.models.conversation import Conversation
from app.models.lead import Lead
from app.models.lead_activity import (
    ACTIVITY_EMAIL_OPENED,
    ACTIVITY_ENRICHED,
    ACTIVITY_SCORE_CHANGED,
    LeadActivity,
)
from app.models.lead_score import (
    SCORE_BEHAVIORAL,
    SCORE_FIT,
    TRIGGER_ENRICHMENT,
    TRIGGER_RECALC,
    LeadScore,
)
from app.models.message import Message
from app.schemas.lead_scoring import CompanyData, PersonData, ScoreResult
from app.services.enrichment.base import EnrichmentProvider, PersonQuery
from app.services.enrichment.engine import enriquecer_lead
from app.services.lead_activity import registrar_actividad
from app.services.lead_behavioral import recolectar_senales
from app.services.lead_score_service import (
    aplicar_score,
    borrar_historial_scores,
    historial_scores,
    recalcular_behavioral,
    recalcular_fit,
    scores_detallados,
    scores_para_export,
)

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

AHORA = datetime.now(timezone.utc)
ICP = {
    "industries": ["software"],
    "company_sizes": ["51-200"],
    "job_titles": ["cto"],
    "countries": ["CO"],
}


async def _crear_tenant(**config: Any) -> uuid.UUID:
    client_id = uuid.uuid4()
    async with tenant_session(client_id) as s:
        await s.execute(
            text(
                "INSERT INTO clients (id, name, slug, plan, lead_management_enabled, icp_config, "
                "lead_scoring_weights) VALUES (:id, 'T', :slug, 'free', true, "
                "CAST(:icp AS jsonb), CAST(:pesos AS jsonb))"
            ),
            {
                "id": str(client_id),
                "slug": f"score-{client_id.hex[:10]}",
                "icp": json.dumps(config.get("icp", {})),
                "pesos": json.dumps(config.get("pesos", {"fit": 40, "behavioral": 30, "ai": 30})),
            },
        )
    return client_id


async def _borrar_tenant(client_id: uuid.UUID) -> None:
    async with tenant_session(client_id) as s:
        await s.execute(
            text("UPDATE contacts SET lead_id = NULL WHERE client_id = :c"), {"c": str(client_id)}
        )
        for tabla in ("leads", "messages", "conversations", "contacts"):
            await s.execute(
                text(f"DELETE FROM {tabla} WHERE client_id = :c"),  # noqa: S608
                {"c": str(client_id)},
            )
        await s.execute(text("DELETE FROM clients WHERE id = :c"), {"c": str(client_id)})


@pytest_asyncio.fixture
async def tenant() -> AsyncGenerator[uuid.UUID, None]:
    client_id = await _crear_tenant(icp=ICP)
    yield client_id
    await _borrar_tenant(client_id)


@pytest_asyncio.fixture
async def otro_tenant() -> AsyncGenerator[uuid.UUID, None]:
    client_id = await _crear_tenant()
    yield client_id
    await _borrar_tenant(client_id)


async def _lead(client_id: uuid.UUID, **campos: Any) -> uuid.UUID:
    async with tenant_session(client_id) as s:
        lead = Lead(client_id=client_id, **campos)
        s.add(lead)
        await s.flush()
        return lead.id


async def _cargar(s: Any, client_id: uuid.UUID, lead_id: uuid.UUID) -> Lead:
    return (
        await s.execute(select(Lead).where(Lead.client_id == client_id, Lead.id == lead_id))
    ).scalar_one()


# ─── RLS de lead_scores ──────────────────────────────────────────────────────────────────────


class TestAislamiento:
    async def test_un_tenant_no_ve_ni_escribe_los_scores_de_otro(
        self, tenant: uuid.UUID, otro_tenant: uuid.UUID
    ) -> None:
        lead_id = await _lead(tenant, first_name="Ana")
        async with tenant_session(tenant) as s:
            lead = await _cargar(s, tenant, lead_id)
            aplicar_score(
                s, lead, SCORE_FIT, ScoreResult(score=50), trigger=TRIGGER_RECALC,
                pesos={"fit": 40, "behavioral": 30, "ai": 30},
            )  # fmt: skip

        async with tenant_session(otro_tenant) as s:
            assert (await s.execute(select(func.count()).select_from(LeadScore))).scalar() == 0

        async with tenant_session(otro_tenant) as s:
            s.add(
                LeadScore(
                    client_id=tenant, lead_id=lead_id, score_type=SCORE_FIT, score=1,
                    trigger=TRIGGER_RECALC, factors={},
                )
            )  # fmt: skip
            # El WITH CHECK de la politica rechaza escribir con el client_id de otro tenant.
            with pytest.raises(DBAPIError):
                await s.flush()

    async def test_borrar_el_lead_borra_su_historial(self, tenant: uuid.UUID) -> None:
        lead_id = await _lead(tenant, first_name="Ana", industry="Software")
        async with tenant_session(tenant) as s:
            await recalcular_fit(s, await _cargar(s, tenant, lead_id), trigger=TRIGGER_RECALC)
        async with tenant_session(tenant) as s:
            await s.execute(text("DELETE FROM leads WHERE id = :i"), {"i": str(lead_id)})
        async with tenant_session(tenant) as s:
            assert (await s.execute(select(func.count()).select_from(LeadScore))).scalar() == 0


# ─── Aplicar un score ────────────────────────────────────────────────────────────────────────


class TestAplicarScore:
    async def test_fit_usa_el_icp_y_los_pesos_del_tenant(self) -> None:
        client_id = await _crear_tenant(icp=ICP, pesos={"fit": 1, "behavioral": 1, "ai": 0})
        try:
            lead_id = await _lead(
                client_id, first_name="Ana", industry="Software", company_size="120",
                job_title="CTO", enrichment_data={"company": {"country": "CO"}},
            )  # fmt: skip
            async with tenant_session(client_id) as s:
                fila = await recalcular_fit(
                    s, await _cargar(s, client_id, lead_id), trigger=TRIGGER_RECALC
                )
                assert fila is not None
            async with tenant_session(client_id) as s:
                lead = await _cargar(s, client_id, lead_id)
                # FIT 100 con pesos 1/1/0 -> total 50 (y no 40, el de 40/30/30).
                assert (lead.fit_score, lead.total_score) == (100, 50)
                [historial] = await historial_scores(s, client_id, lead_id)
                assert (historial.score, historial.previous_score) == (100, 0)
                assert historial.factors["completeness"] == "4/4"
                actividad = (
                    await s.execute(
                        select(LeadActivity).where(
                            LeadActivity.lead_id == lead_id,
                            LeadActivity.activity_type == ACTIVITY_SCORE_CHANGED,
                        )
                    )
                ).scalar_one()
                assert actividad.metadata_ == {
                    "score_type": "fit",
                    "score_from": 0,
                    "score_to": 100,
                    "total_from": 0,
                    "total_to": 50,
                    "trigger": "recalc",
                }
        finally:
            await _borrar_tenant(client_id)

    async def test_sin_cambio_no_hay_historial_salvo_que_se_fuerce(self, tenant: uuid.UUID) -> None:
        lead_id = await _lead(tenant, first_name="Ana", industry="Software")
        async with tenant_session(tenant) as s:
            lead = await _cargar(s, tenant, lead_id)
            assert await recalcular_fit(s, lead, trigger=TRIGGER_RECALC) is not None
            assert await recalcular_fit(s, lead, trigger=TRIGGER_RECALC) is None
            forzada = await recalcular_fit(s, lead, trigger=TRIGGER_RECALC, forzar_historial=True)
            assert forzada is not None
            assert forzada.previous_score == forzada.score
        async with tenant_session(tenant) as s:
            assert len(await historial_scores(s, tenant, lead_id)) == 2
            cambios = (
                await s.execute(
                    select(func.count())
                    .select_from(LeadActivity)
                    .where(LeadActivity.activity_type == ACTIVITY_SCORE_CHANGED)
                )
            ).scalar()
            assert cambios == 1  # el forzado sin cambio no ensucia el historial del lead

    async def test_sin_icp_el_fit_vigente_no_se_toca(self, otro_tenant: uuid.UUID) -> None:
        lead_id = await _lead(otro_tenant, first_name="Ana", fit_score=42, total_score=17)
        async with tenant_session(otro_tenant) as s:
            assert await recalcular_fit(
                s, await _cargar(s, otro_tenant, lead_id), trigger=TRIGGER_RECALC
            ) is None  # fmt: skip
        async with tenant_session(otro_tenant) as s:
            lead = await _cargar(s, otro_tenant, lead_id)
            assert (lead.fit_score, lead.total_score) == (42, 17)
            assert await historial_scores(s, otro_tenant, lead_id) == []

    async def test_detalle_trae_el_ultimo_de_cada_tipo(self, tenant: uuid.UUID) -> None:
        lead_id = await _lead(tenant, first_name="Ana")
        pesos = {"fit": 40, "behavioral": 30, "ai": 30}
        async with tenant_session(tenant) as s:
            lead = await _cargar(s, tenant, lead_id)
            for tipo, valor in ((SCORE_FIT, 10), (SCORE_FIT, 60), (SCORE_BEHAVIORAL, 20)):
                aplicar_score(
                    s, lead, tipo, ScoreResult(score=valor), trigger=TRIGGER_RECALC, pesos=pesos
                )
        async with tenant_session(tenant) as s:
            detalle = await scores_detallados(s, await _cargar(s, tenant, lead_id))
        assert set(detalle.latest) == {"fit", "behavioral"}
        assert detalle.latest["fit"].score == 60
        assert detalle.latest["fit"].previous_score == 10
        assert (detalle.fit_score, detalle.behavioral_score, detalle.total_score) == (60, 20, 30)
        assert detalle.weights == pesos


# ─── Senales de comportamiento ───────────────────────────────────────────────────────────────


async def _conversacion(
    client_id: uuid.UUID, contacto: uuid.UUID, mensajes: list[tuple[str, timedelta]]
) -> None:
    """Una conversacion con mensajes (`direccion`, `hace cuanto`) en orden."""
    async with tenant_session(client_id) as s:
        conv = Conversation(client_id=client_id, contact_id=contacto, channel="whatsapp")
        s.add(conv)
        await s.flush()
        for direccion, hace in mensajes:
            s.add(
                Message(
                    client_id=client_id, conversation_id=conv.id, direction=direccion,
                    message_type="text", content="x", created_at=AHORA - hace,
                    sender_type="contact" if direccion == "inbound" else "agent",
                )
            )  # fmt: skip


class TestSenales:
    async def test_salen_de_los_mensajes_del_contacto_enlazado(self, tenant: uuid.UUID) -> None:
        async with tenant_session(tenant) as s:
            contacto = Contact(client_id=tenant, first_name="Ana")
            s.add(contacto)
            await s.flush()
            contacto_id = contacto.id
        await _conversacion(
            tenant,
            contacto_id,
            [
                ("inbound", timedelta(days=60)),  # escribio primero (fuera de la ventana)
                ("outbound", timedelta(days=5, hours=2)),
                ("inbound", timedelta(days=5)),  # contesto en 2 h
                ("outbound", timedelta(days=1, minutes=30)),
                ("inbound", timedelta(days=1)),  # contesto en 30 min
                ("inbound", timedelta(hours=1)),  # no es respuesta: el anterior es suyo
            ],
        )
        lead_id = await _lead(
            tenant,
            first_name="Ana",
            contact_id=contacto_id,
            last_activity_at=AHORA - timedelta(hours=1),
        )
        async with tenant_session(tenant) as s:
            registrar_actividad(s, client_id=tenant, lead_id=lead_id, tipo=ACTIVITY_EMAIL_OPENED)

        # La apertura se anota ahora mismo, despues de `AHORA` (fijado al importar).
        ahora = datetime.now(timezone.utc)
        async with tenant_session(tenant) as s:
            senales = await recolectar_senales(s, await _cargar(s, tenant, lead_id), ahora)
        assert senales.inbound_messages == 3
        assert senales.outbound_messages == 2
        assert sorted(round(m) for m in senales.response_minutes) == [30, 120]
        assert senales.lead_initiated is True
        assert senales.email_opens == 1

        async with tenant_session(tenant) as s:
            fila = await recalcular_behavioral(
                s, await _cargar(s, tenant, lead_id), trigger=TRIGGER_RECALC, ahora=ahora
            )
        assert fila is not None
        # recencia 30 + 3 mensajes 15 + mediana 75 min -> 18 + 1 apertura 2.
        assert fila.score == 65

    async def test_no_cuenta_mensajes_de_otro_contacto_ni_de_otro_tenant(
        self, tenant: uuid.UUID, otro_tenant: uuid.UUID
    ) -> None:
        async with tenant_session(tenant) as s:
            ana, beto = Contact(client_id=tenant), Contact(client_id=tenant)
            s.add_all([ana, beto])
            await s.flush()
            ana_id, beto_id = ana.id, beto.id
        async with tenant_session(otro_tenant) as s:
            ajeno = Contact(client_id=otro_tenant)
            s.add(ajeno)
            await s.flush()
            ajeno_id = ajeno.id
        await _conversacion(tenant, beto_id, [("inbound", timedelta(hours=1))])
        await _conversacion(otro_tenant, ajeno_id, [("inbound", timedelta(hours=1))])
        lead_id = await _lead(tenant, first_name="Ana", contact_id=ana_id)
        async with tenant_session(tenant) as s:
            senales = await recolectar_senales(s, await _cargar(s, tenant, lead_id), AHORA)
        assert (senales.inbound_messages, senales.lead_initiated) == (0, False)


# ─── Enriquecimiento persistido ──────────────────────────────────────────────────────────────


class ProveedorFijo(EnrichmentProvider):
    name: ClassVar[str] = "fijo"
    supports_company: ClassVar[bool] = True
    supports_person: ClassVar[bool] = True

    async def enrich_company(self, domain: str) -> CompanyData | None:
        return CompanyData(
            domain=domain, name="Acme", industry="Software", employees=90, country="CO"
        )

    async def enrich_person(self, query: PersonQuery) -> PersonData | None:
        return PersonData(job_title="CTO")


async def test_el_enriquecimiento_persiste_sin_pisar_y_sube_el_fit(tenant: uuid.UUID) -> None:
    lead_id = await _lead(
        tenant, first_name="Ana", email="ana@acme.com", company_name="ACME (manual)"
    )
    async with tenant_session(tenant) as s:
        lead = await _cargar(s, tenant, lead_id)
        salida = await enriquecer_lead(s, lead, [ProveedorFijo()], ahora=AHORA)
        assert salida.providers_used == ["fijo"]
        await recalcular_fit(s, lead, trigger=TRIGGER_ENRICHMENT)

    async with tenant_session(tenant) as s:
        lead = await _cargar(s, tenant, lead_id)
        assert lead.company_name == "ACME (manual)"
        assert (lead.industry, lead.company_size, lead.job_title) == ("Software", "51-200", "CTO")
        assert lead.enrichment_data["company"]["country"] == "CO"
        assert lead.enriched_at is not None
        assert lead.fit_score == 100
        tipos = (
            (
                await s.execute(
                    select(LeadActivity.activity_type)
                    .where(LeadActivity.lead_id == lead_id)
                    .order_by(LeadActivity.created_at)
                )
            )
            .scalars()
            .all()
        )
        assert tipos == [ACTIVITY_ENRICHED, ACTIVITY_SCORE_CHANGED]


# ─── RGPD ────────────────────────────────────────────────────────────────────────────────────


async def test_export_y_borrado_del_historial(tenant: uuid.UUID) -> None:
    lead_id = await _lead(tenant, first_name="Ana", industry="Software")
    otro_id = await _lead(tenant, first_name="Beto", industry="Software", email="b@x.com")
    async with tenant_session(tenant) as s:
        for lid in (lead_id, otro_id):
            await recalcular_fit(s, await _cargar(s, tenant, lid), trigger=TRIGGER_RECALC)

    async with tenant_session(tenant) as s:
        export = await scores_para_export(s, tenant, [lead_id])
        assert [f["score_type"] for f in export[lead_id]] == ["fit"]
        assert await borrar_historial_scores(s, tenant, [lead_id]) == 1
    async with tenant_session(tenant) as s:
        assert await historial_scores(s, tenant, lead_id) == []
        assert len(await historial_scores(s, tenant, otro_id)) == 1
        # El valor vigente se conserva (estadisticas), como la etapa.
        assert (await _cargar(s, tenant, lead_id)).fit_score > 0
