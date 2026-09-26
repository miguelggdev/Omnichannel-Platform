"""Concurrencia del lanzamiento de campanas contra PostgreSQL real (Sprint 12).

Los tests unitarios comprueban que las consultas llevan `FOR UPDATE`; estos
comprueban que eso de verdad impide el doble envio. Sin candado, dos
transacciones que leen la campana antes de que la otra commitee ven las dos
`scheduled`, las dos escriben `sending` y el segmento recibe el mensaje dos
veces.

Para que la carrera no dependa del scheduling, `campana_duplicada()` (que corre
entre la lectura y la escritura) se envuelve en una barrera: cada lanzamiento
espera ahi a que el otro llegue, hasta `ESPERA_BARRERA` segundos. Sin candado
los dos llegan y siguen juntos; con candado el segundo esta bloqueado en el
lock, el primero agota la espera, commitea, y recien entonces el segundo lee.
"""

import asyncio
import contextlib
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

#: Cuanto espera cada lanzamiento al otro en la barrera.
ESPERA_BARRERA = 1.0


@pytest_asyncio.fixture
async def tenant_campanas() -> AsyncGenerator[uuid.UUID, None]:
    """Crea un tenant commiteado y borra sus campanas al terminar."""
    from app.core.database import engine, tenant_session

    # Ver `webhook_tenant` en conftest.py: el pool del engine puede traer
    # conexiones atadas al loop de un test anterior.
    await engine.dispose()

    client_id = uuid.uuid4()
    async with tenant_session(client_id) as session:
        await session.execute(
            text(
                "INSERT INTO clients (id, name, slug, plan, is_active) "
                "VALUES (:id, 'Tenant Campanas', :slug, 'free', true)"
            ),
            {"id": str(client_id), "slug": f"campanas-{client_id.hex[:8]}"},
        )

    yield client_id

    async with tenant_session(client_id) as session:
        await session.execute(
            text("DELETE FROM campaigns WHERE client_id = :cid"), {"cid": str(client_id)}
        )
        await session.execute(text("DELETE FROM clients WHERE id = :cid"), {"cid": str(client_id)})

    # Liberar el pool en ESTE loop: si lo hace el test siguiente, desde otro
    # loop, cada conexion vieja deja un "Exception closing connection".
    await engine.dispose()


async def _crear_campana(client_id: uuid.UUID, **campos: Any) -> uuid.UUID:
    """Inserta una campana confirmada (`scheduled`) y lista para salir."""
    from app.core.database import tenant_session
    from app.models.campaign import CAMPAIGN_SCHEDULED, Campaign

    campaign_id = uuid.uuid4()
    datos: dict[str, Any] = {
        "name": "Promo",
        # Telegram: sin chequeo de plantilla aprobada, que aca no importa.
        "channel": "telegram",
        "segment_criteria": {"tags": ["vip"]},
        "message_template": "Hola {{contact_name}}",
        "status": CAMPAIGN_SCHEDULED,
        "scheduled_for": datetime.now(timezone.utc) - timedelta(minutes=1),
    }
    datos.update(campos)
    async with tenant_session(client_id) as session:
        session.add(Campaign(id=campaign_id, client_id=client_id, **datos))
    return campaign_id


async def _estado(client_id: uuid.UUID, campaign_id: uuid.UUID) -> str:
    """Estado commiteado de una campana."""
    from app.core.database import tenant_session

    async with tenant_session(client_id) as session:
        return str(
            (
                await session.execute(
                    text("SELECT status FROM campaigns WHERE id = :id"), {"id": str(campaign_id)}
                )
            ).scalar_one()
        )


def _con_barrera(monkeypatch: pytest.MonkeyPatch, participantes: int) -> None:
    """Hace que cada lanzamiento espere a los demas entre la lectura y la escritura."""
    from app.services import campaigns as servicio
    from app.tasks import campaign_tasks as ct

    original = servicio.campana_duplicada
    llegados = 0
    todos = asyncio.Event()

    async def _duplicada(*args: Any, **kwargs: Any) -> Any:
        nonlocal llegados
        llegados += 1
        if llegados >= participantes:
            todos.set()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(todos.wait(), timeout=ESPERA_BARRERA)
        return await original(*args, **kwargs)

    monkeypatch.setattr(ct, "campana_duplicada", _duplicada)


async def test_la_misma_campana_se_lanza_una_sola_vez(
    tenant_campanas: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dos tasks de la misma campana: solo una la pasa a `sending`."""
    from app.tasks import campaign_tasks as ct

    campaign_id = await _crear_campana(tenant_campanas)
    _con_barrera(monkeypatch, participantes=2)

    resultados = await asyncio.gather(
        ct._marcar_en_envio(tenant_campanas, campaign_id),
        ct._marcar_en_envio(tenant_campanas, campaign_id),
    )

    lanzadas = [r for r in resultados if r is not None]
    assert len(lanzadas) == 1, "la campana salio dos veces"
    assert await _estado(tenant_campanas, campaign_id) == "sending"


async def test_dos_campanas_del_mismo_segmento_no_salen_juntas(
    tenant_campanas: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El anti-duplicado de 24h tambien vale entre lanzamientos simultaneos."""
    from app.tasks import campaign_tasks as ct

    primera = await _crear_campana(tenant_campanas, name="Primera")
    segunda = await _crear_campana(tenant_campanas, name="Segunda")
    _con_barrera(monkeypatch, participantes=2)

    resultados = await asyncio.gather(
        ct._marcar_en_envio(tenant_campanas, primera),
        ct._marcar_en_envio(tenant_campanas, segunda),
        return_exceptions=True,
    )

    lanzadas = [r for r in resultados if r is not None and not isinstance(r, BaseException)]
    rechazadas = [r for r in resultados if isinstance(r, ct.CampanaNoEnviableError)]
    assert len(lanzadas) == 1, f"salieron {len(lanzadas)} campanas al mismo segmento"
    assert len(rechazadas) == 1


async def test_una_programada_a_futuro_no_sale_antes_de_hora(
    tenant_campanas: uuid.UUID,
) -> None:
    """Una task que llega antes de `scheduled_for` no la lanza ni la toca."""
    from app.tasks import campaign_tasks as ct

    campaign_id = await _crear_campana(
        tenant_campanas, scheduled_for=datetime.now(timezone.utc) + timedelta(hours=2)
    )

    assert await ct._marcar_en_envio(tenant_campanas, campaign_id) is None
    assert await _estado(tenant_campanas, campaign_id) == "scheduled"


async def test_el_despachador_solo_ve_las_que_ya_tienen_que_salir(
    tenant_campanas: uuid.UUID,
) -> None:
    from app.tasks import campaign_tasks as ct

    vencida = await _crear_campana(tenant_campanas, name="Vencida")
    await _crear_campana(
        tenant_campanas,
        name="Manana",
        scheduled_for=datetime.now(timezone.utc) + timedelta(days=1),
    )
    await _crear_campana(tenant_campanas, name="Borrador", status="draft")

    assert await ct._campanas_vencidas(tenant_campanas) == [vencida]
