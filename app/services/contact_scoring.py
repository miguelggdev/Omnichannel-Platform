"""Scoring predictivo de contactos (Sprint 12, Dev B).

Que mide
---------
Un numero de 0 a 100 que resume que tan "vivo" esta un contacto, para que el
`score_min` de la segmentacion de campanas (`app/services/segmentation.py`)
pueda apuntar a los mas activos. Hasta cinco componentes ponderados (pesos
sin / con sentimiento medido):

- **Recencia** (0.31 / 0.25): cuanto hace que hablo por ultima vez.
- **Frecuencia** (0.31 / 0.25): cuantos mensajes mando en los ultimos 30 dias.
- **Sentimiento** (— / 0.20): promedio del sentimiento de sus mensajes.
- **Engagement** (0.19 / 0.15): que proporcion de lo que le mandamos contesto.
- **Conversion** (0.19 / 0.15): citas agendadas y facturas emitidas en 90 dias.

El sentimiento, cuando hay datos
---------------------------------
El spec (§7) reparte los pesos entre cinco componentes, con 0.20 para el
sentimiento acumulado del contacto. Desde el Sprint 10 (nodo
`sentiment_analysis`) el sentimiento de cada mensaje entrante queda en
`messages.metadata.sentiment.level`, pero **solo** para los tenants que activan
ese agente, y solo para los mensajes posteriores.

- **Con mensajes clasificados en la ventana:** se usan los cinco pesos del spec
  (`PESOS_CON_SENTIMIENTO`), con el promedio de los niveles en escala 0-100
  (`PUNTAJE_SENTIMIENTO`).
- **Sin ninguno:** los cuatro pesos de siempre (`PESOS`), con el 0.20 del
  sentimiento repartido en proporcion. El `return 50` "neutral por defecto" del
  spec seria la misma constante para todos: no distingue a nadie y solo
  comprime el rango util.

Asi el score de un tenant no cambia hasta que empieza a medir sentimiento, y
dentro de un tenant compara igual a quien no tiene mensajes medidos todavia.

Separacion IO / calculo
------------------------
`cargar_metricas()` toca la base y `calcular_score()` es una funcion pura
sobre esas metricas. El calculo — que es donde estan las reglas de negocio y
los umbrales — se testea entero sin PostgreSQL.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import case, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contact import Contact
from app.models.conversation import Conversation
from app.models.invoice import Invoice
from app.models.message import Message
from app.models.service_type import Appointment
from app.schemas.sentiment import PUNTAJE_SENTIMIENTO

logger = logging.getLogger(__name__)

#: Pesos de cada componente. Suman 1.0.
#:
#: El spec reparte 0.25/0.25/0.20/0.15/0.15 entre recencia, frecuencia,
#: sentimiento, engagement y conversion. Sacado el sentimiento (ver el
#: docstring del modulo), su 0.20 se reparte entre los otros cuatro en
#: proporcion a sus pesos originales: 0.25/0.80 -> 0.3125, 0.15/0.80 -> 0.1875.
PESOS: dict[str, float] = {
    "recency": 0.31,
    "frequency": 0.31,
    "engagement": 0.19,
    "conversion": 0.19,
}

#: Pesos del spec (§7) cuando el contacto tiene mensajes con sentimiento medido.
PESOS_CON_SENTIMIENTO: dict[str, float] = {
    "recency": 0.25,
    "frequency": 0.25,
    "sentiment": 0.20,
    "engagement": 0.15,
    "conversion": 0.15,
}

#: Ventana de la frecuencia y del engagement, en dias.
VENTANA_ACTIVIDAD_DIAS = 30

#: Ventana de las conversiones, en dias.
VENTANA_CONVERSION_DIAS = 90

#: Cada conversion suma esto al componente, hasta 100.
PUNTOS_POR_CONVERSION = 20


@dataclass(frozen=True)
class MetricasDelContacto:
    """Datos crudos con los que se calcula el score.

    Attributes:
        ultima_actividad: Momento del ultimo mensaje del contacto, si hubo.
        entrantes: Mensajes que mando el contacto en la ventana de actividad.
        salientes: Mensajes que le mandamos en la ventana de actividad.
        conversiones: Citas + facturas en la ventana de conversion.
        sentimiento: Promedio (0-100) del sentimiento de los mensajes del
            contacto en la ventana de actividad; `None` si ninguno esta medido.
    """

    ultima_actividad: datetime | None
    entrantes: int
    salientes: int
    conversiones: int
    sentimiento: float | None = None


def _score_recencia(ultima_actividad: datetime | None, ahora: datetime) -> float:
    """Puntua cuanto hace que el contacto hablo por ultima vez.

    Args:
        ultima_actividad: Momento del ultimo mensaje entrante, o None.
        ahora: Momento de referencia.

    Returns:
        Puntaje de 0 a 100.
    """
    if ultima_actividad is None:
        return 0.0

    # Un contacto con actividad "en el futuro" (reloj torcido, import de
    # historico con fechas mal) cuenta como recentisimo, no como negativo.
    dias = max(0, (ahora - ultima_actividad).days)
    if dias <= 1:
        return 100.0
    if dias <= 7:
        return 80.0
    if dias <= 30:
        return 50.0
    if dias <= 90:
        return 20.0
    return 0.0


def _score_frecuencia(entrantes: int) -> float:
    """Puntua cuantos mensajes mando el contacto en la ventana.

    Args:
        entrantes: Mensajes entrantes en la ventana de actividad.

    Returns:
        Puntaje de 0 a 100.
    """
    if entrantes >= 20:
        return 100.0
    if entrantes >= 10:
        return 80.0
    if entrantes >= 5:
        return 60.0
    if entrantes >= 1:
        return 30.0
    return 0.0


def _score_engagement(entrantes: int, salientes: int) -> float:
    """Puntua que proporcion de lo que le mandamos contesto.

    Es una aproximacion por volumen (entrantes sobre salientes), no un
    emparejamiento mensaje a mensaje: para saber si *ese* saliente concreto
    tuvo respuesta habria que correlacionar cada uno con el siguiente entrante
    de su conversacion, una subconsulta por mensaje que no paga lo que cuesta
    para un score que se recalcula seguido. La proporcion captura lo mismo a
    nivel de contacto: quien nunca contesta da 0, quien contesta todo da 100.

    Args:
        entrantes: Mensajes del contacto en la ventana.
        salientes: Mensajes que le mandamos en la ventana.

    Returns:
        Puntaje de 0 a 100.
    """
    if salientes <= 0:
        # Nunca le escribimos: no hay nada que responder, asi que no se le
        # puede premiar ni castigar. Neutral explicito, no 0.
        return 50.0
    return min(entrantes / salientes, 1.0) * 100


def _score_conversion(conversiones: int) -> float:
    """Puntua citas y facturas del contacto en la ventana de conversion.

    Args:
        conversiones: Citas + facturas en la ventana.

    Returns:
        Puntaje de 0 a 100.
    """
    return float(min(conversiones * PUNTOS_POR_CONVERSION, 100))


def calcular_score(metricas: MetricasDelContacto, ahora: datetime | None = None) -> float:
    """Combina las metricas en el score final.

    Funcion pura: no toca la base ni el reloj salvo por `ahora`, que se puede
    fijar desde los tests.

    Args:
        metricas: Datos crudos del contacto.
        ahora: Momento de referencia; por defecto, el actual en UTC.

    Returns:
        Score de 0 a 100, con dos decimales.
    """
    momento = ahora or datetime.now(timezone.utc)

    componentes = {
        "recency": _score_recencia(metricas.ultima_actividad, momento),
        "frequency": _score_frecuencia(metricas.entrantes),
        "engagement": _score_engagement(metricas.entrantes, metricas.salientes),
        "conversion": _score_conversion(metricas.conversiones),
    }
    pesos = PESOS
    if metricas.sentimiento is not None:
        componentes["sentiment"] = max(0.0, min(float(metricas.sentimiento), 100.0))
        pesos = PESOS_CON_SENTIMIENTO
    total = sum(componentes[nombre] * peso for nombre, peso in pesos.items())
    return round(total, 2)


async def cargar_metricas(
    session: AsyncSession, client_id: UUID, contact_id: UUID, ahora: datetime | None = None
) -> MetricasDelContacto:
    """Lee de la base las metricas crudas de un contacto.

    Todas salen en un solo SELECT con subconsultas escalares, no en cinco
    viajes como el pseudocodigo del spec: el endpoint recalcula de a un
    contacto hoy, pero un recalculo por lotes sobre miles de contactos con
    cinco round-trips cada uno no se sostiene.

    Todas las subconsultas filtran por `client_id` explicito ademas de por
    RLS, que es la convencion del repo desde la revision del PR #43.

    Args:
        session: Sesion con el contexto de tenant ya aplicado.
        client_id: Tenant dueno del contacto.
        contact_id: Contacto a medir.
        ahora: Momento de referencia; por defecto, el actual en UTC.

    Returns:
        Las metricas crudas del contacto.
    """
    momento = ahora or datetime.now(timezone.utc)
    desde_actividad = momento - timedelta(days=VENTANA_ACTIVIDAD_DIAS)
    desde_conversion = momento - timedelta(days=VENTANA_CONVERSION_DIAS)

    # Conversaciones del contacto: el puente entre `messages` y `contacts`,
    # porque `messages` no lleva `contact_id`.
    conversaciones = (
        select(Conversation.id)
        .where(Conversation.client_id == client_id, Conversation.contact_id == contact_id)
        .scalar_subquery()
    )

    def _contar_mensajes(direccion: str) -> Any:
        return (
            select(func.count())
            .select_from(Message)
            .where(
                Message.client_id == client_id,
                Message.conversation_id.in_(conversaciones),
                Message.direction == direccion,
                Message.created_at >= desde_actividad,
            )
            .scalar_subquery()
        )

    ultima_actividad = (
        select(func.max(Message.created_at))
        .where(
            Message.client_id == client_id,
            Message.conversation_id.in_(conversaciones),
            Message.direction == "inbound",
        )
        .scalar_subquery()
    )

    # Promedio del sentimiento de los mensajes entrantes medidos en la ventana
    # (`sentiment_analysis_node`). AVG ignora los NULL: los mensajes sin medir
    # no cuentan como neutrales, y si no hay ninguno el resultado es NULL.
    nivel = Message.metadata_["sentiment"]["level"].astext
    puntaje = case(
        *((nivel == nombre, valor) for nombre, valor in PUNTAJE_SENTIMIENTO.items()),
        else_=None,
    )
    sentimiento = (
        select(func.avg(puntaje))
        .where(
            Message.client_id == client_id,
            Message.conversation_id.in_(conversaciones),
            Message.direction == "inbound",
            Message.created_at >= desde_actividad,
        )
        .scalar_subquery()
    )

    citas = (
        select(func.count())
        .select_from(Appointment)
        .where(
            Appointment.client_id == client_id,
            Appointment.contact_id == contact_id,
            Appointment.created_at >= desde_conversion,
        )
        .scalar_subquery()
    )

    facturas = (
        select(func.count())
        .select_from(Invoice)
        .where(
            Invoice.client_id == client_id,
            Invoice.contact_id == contact_id,
            Invoice.created_at >= desde_conversion,
        )
        .scalar_subquery()
    )

    fila = (
        await session.execute(
            select(
                ultima_actividad.label("ultima_actividad"),
                _contar_mensajes("inbound").label("entrantes"),
                _contar_mensajes("outbound").label("salientes"),
                (citas + facturas).label("conversiones"),
                sentimiento.label("sentimiento"),
            )
        )
    ).one()

    return MetricasDelContacto(
        ultima_actividad=fila.ultima_actividad,
        entrantes=int(fila.entrantes or 0),
        salientes=int(fila.salientes or 0),
        conversiones=int(fila.conversiones or 0),
        sentimiento=None if fila.sentimiento is None else float(fila.sentimiento),
    )


async def recalcular_score(
    session: AsyncSession, client_id: UUID, contact_id: UUID, ahora: datetime | None = None
) -> tuple[float, datetime]:
    """Calcula el score de un contacto y lo guarda en su `metadata`.

    El `metadata` se actualiza con `jsonb_set` en SQL, no leyendo el dict,
    modificandolo en Python y reescribiendolo entero: esa columna tambien la
    edita el tenant por la API del CRM (`PUT /contacts/{id}`), y un
    read-modify-write pisaria cualquier clave que otro escribiera en el medio.
    Es la leccion de BUG-022 y del contador de fallos de ADR-065.

    El score queda como texto numerico en `metadata->>'score'`, que es
    exactamente lo que lee el filtro `score_min` de `segmentation.py`.

    Args:
        session: Sesion con el contexto de tenant ya aplicado.
        client_id: Tenant dueno del contacto.
        contact_id: Contacto a recalcular.
        ahora: Momento de referencia; por defecto, el actual en UTC.

    Returns:
        `(score, momento_del_calculo)`.
    """
    momento = ahora or datetime.now(timezone.utc)
    metricas = await cargar_metricas(session, client_id, contact_id, momento)
    score = calcular_score(metricas, momento)

    vacio = text("'{}'::jsonb")
    await session.execute(
        update(Contact)
        .where(Contact.id == contact_id, Contact.client_id == client_id)
        .values(
            # `||` en vez de `jsonb_set` anidado por clave: para claves de
            # primer nivel el resultado es el mismo (crea o reemplaza sin
            # tocar el resto), pero agregar una tercera clave el dia de manana
            # no exige otro nivel de anidamiento. Hallazgo de /code-review
            # sobre el PR #46.
            metadata_=func.coalesce(Contact.metadata_, vacio).op("||")(
                func.jsonb_build_object("score", score, "score_updated_at", momento.isoformat())
            )
        )
        .execution_options(synchronize_session=False)
    )

    logger.info(
        "Score del contacto %s recalculado: %.2f (entrantes=%d salientes=%d conversiones=%d)",
        contact_id,
        score,
        metricas.entrantes,
        metricas.salientes,
        metricas.conversiones,
    )
    return score, momento
