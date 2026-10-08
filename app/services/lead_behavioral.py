"""Score de comportamiento: cuanto interactua el lead (Sprint 17, ADR-022).

Dos piezas separadas a proposito:

- `calcular_behavioral(senales, ahora)`: funcion pura, facil de probar caso a caso.
- `recolectar_senales(session, lead, ahora)`: lee la base (mensajes del contacto enlazado y el
  historial del lead) y arma las `BehavioralSignals`.

Reparto de los 100 puntos (cada componente tiene tope; los factores guardan cuantos dio cada uno
y los conteos que lo explican, sin contenido de mensajes):

==================  ====  =============================================================
Componente          Tope  Regla
==================  ====  =============================================================
recency             30    ultima actividad <=1d 30, <=3d 25, <=7d 20, <=14d 14,
                          <=30d 8, <=90d 3; mas antigua o ninguna 0
inbound_volume      30    5 por mensaje del lead en los ultimos 30 dias (tope 6 mensajes)
responsiveness      25    mediana de lo que tarda en contestar a un mensaje nuestro:
                          <=1h 25, <=24h 18, <=72h 10, mas 5. Si nunca contesto a nada
                          nuestro pero escribio el primero: 15. Si no, 0
engagement          15    5 por clic y 2 por apertura de email en 30 dias (tope 15)
==================  ====  =============================================================

Aperturas y clics todavia no los produce nada (llegan con las secuencias del Sprint 18): se leen
del historial (`ACTIVITY_EMAIL_OPENED`, `ACTIVITY_LINK_CLICKED`) para que el calculo no cambie
cuando aparezcan.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.lead import Lead
from app.models.lead_activity import (
    ACTIVITY_EMAIL_OPENED,
    ACTIVITY_LINK_CLICKED,
    LeadActivity,
)
from app.models.message import Message
from app.schemas.lead_scoring import ScoreResult

BEHAVIORAL_VERSION = 1
VENTANA = timedelta(days=30)

_RECENCIA: tuple[tuple[timedelta, int], ...] = (
    (timedelta(days=1), 30),
    (timedelta(days=3), 25),
    (timedelta(days=7), 20),
    (timedelta(days=14), 14),
    (timedelta(days=30), 8),
    (timedelta(days=90), 3),
)
_RESPUESTA: tuple[tuple[float, int], ...] = ((60, 25), (24 * 60, 18), (72 * 60, 10))
_RESPUESTA_LENTA = 5
_INICIATIVA = 15
_PUNTOS_POR_MENSAJE = 5
_MENSAJES_TOPE = 6
_PUNTOS_CLIC = 5
_PUNTOS_APERTURA = 2
_ENGAGEMENT_TOPE = 15


@dataclass(frozen=True)
class BehavioralSignals:
    """Lo que se sabe de la interaccion del lead, ya contado.

    Attributes:
        last_activity_at: Ultima actividad conocida del lead.
        inbound_messages: Mensajes del lead en la ventana.
        outbound_messages: Mensajes nuestros (agente o bot) en la ventana.
        response_minutes: Minutos que tardo en contestar a cada mensaje nuestro, en la ventana.
        lead_initiated: Si el primer mensaje de todas sus conversaciones lo escribio el lead.
        email_opens: Aperturas de email en la ventana.
        link_clicks: Clics en enlaces en la ventana.
    """

    last_activity_at: datetime | None = None
    inbound_messages: int = 0
    outbound_messages: int = 0
    response_minutes: tuple[float, ...] = ()
    lead_initiated: bool = False
    email_opens: int = 0
    link_clicks: int = 0


def _recencia(ultima: datetime | None, ahora: datetime) -> int:
    """Puntos por lo reciente de la ultima actividad."""
    if ultima is None:
        return 0
    edad = ahora - ultima
    # Una fecha en el futuro (reloj de otra maquina) cuenta como "ahora", no como un premio extra.
    edad = max(edad, timedelta(0))
    return next((puntos for limite, puntos in _RECENCIA if edad <= limite), 0)


def _respuesta(senales: BehavioralSignals) -> tuple[int, float | None]:
    """Puntos por la rapidez de respuesta y la mediana usada (en minutos)."""
    if senales.response_minutes:
        mediana = float(median(senales.response_minutes))
        puntos = next((p for limite, p in _RESPUESTA if mediana <= limite), _RESPUESTA_LENTA)
        return puntos, round(mediana, 1)
    return (_INICIATIVA if senales.lead_initiated else 0), None


def calcular_behavioral(senales: BehavioralSignals, ahora: datetime) -> ScoreResult:
    """Score de comportamiento a partir de las senales.

    Siempre es aplicable: un lead sin ninguna interaccion tiene 0, y eso es informacion.

    Args:
        senales: Interaccion del lead (ver `recolectar_senales()`).
        ahora: Instante de referencia (aware), para la recencia.

    Returns:
        El score con sus factores.
    """
    recencia = _recencia(senales.last_activity_at, ahora)
    volumen = min(senales.inbound_messages, _MENSAJES_TOPE) * _PUNTOS_POR_MENSAJE
    respuesta, mediana = _respuesta(senales)
    engagement = min(
        _ENGAGEMENT_TOPE,
        senales.link_clicks * _PUNTOS_CLIC + senales.email_opens * _PUNTOS_APERTURA,
    )
    score = min(100, recencia + volumen + respuesta + engagement)
    factores: dict[str, Any] = {
        "version": BEHAVIORAL_VERSION,
        "window_days": VENTANA.days,
        "components": {
            "recency": recencia,
            "inbound_volume": volumen,
            "responsiveness": respuesta,
            "engagement": engagement,
        },
        "signals": {
            "days_since_last_activity": (
                max((ahora - senales.last_activity_at).days, 0)
                if senales.last_activity_at
                else None
            ),
            "inbound_messages": senales.inbound_messages,
            "outbound_messages": senales.outbound_messages,
            "median_response_minutes": mediana,
            "lead_initiated": senales.lead_initiated,
            "email_opens": senales.email_opens,
            "link_clicks": senales.link_clicks,
        },
    }
    return ScoreResult(score=score, factors=factores)


async def recolectar_senales(
    session: AsyncSession, lead: Lead, ahora: datetime
) -> BehavioralSignals:
    """Cuenta la interaccion del lead en la base.

    Los mensajes salen de las conversaciones del **contacto enlazado** (`leads.contact_id`): un
    lead sin contacto no ha escrito por ningun canal y solo puntua por recencia y por lo que haya
    en su historial. Se cuentan los mensajes y no las filas `inbound_message` del historial,
    porque esas se anotan como mucho una cada 30 minutos (ADR-084).

    Args:
        session: Sesion con el contexto del tenant fijado (RLS).
        lead: Lead a medir.
        ahora: Instante de referencia (aware).

    Returns:
        Las senales para `calcular_behavioral()`.
    """
    desde = ahora - VENTANA
    entrantes = salientes = 0
    tiempos: list[float] = []
    inicio_lead = False

    if lead.contact_id is not None:
        de_contacto = and_(
            Message.client_id == lead.client_id,
            Conversation.client_id == lead.client_id,
            Conversation.contact_id == lead.contact_id,
        )
        conteo = await session.execute(
            select(Message.direction, func.count())
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(de_contacto, Message.created_at >= desde, Message.created_at <= ahora)
            .group_by(Message.direction)
        )
        por_direccion = dict(conteo.tuples().all())
        entrantes = int(por_direccion.get("inbound", 0))
        salientes = int(por_direccion.get("outbound", 0))

        # Respuesta = mensaje del lead cuyo mensaje anterior en la conversacion fue nuestro.
        # `type_`: sin el, `lag()` devuelve VARCHAR y comparar con el enum `message_direction`
        # falla en PostgreSQL ("operator does not exist").
        anterior_dir = func.lag(Message.direction, type_=Message.direction.type).over(
            partition_by=Message.conversation_id, order_by=(Message.created_at, Message.id)
        )
        anterior_at = func.lag(Message.created_at).over(
            partition_by=Message.conversation_id, order_by=(Message.created_at, Message.id)
        )
        secuencia = (
            select(
                Message.direction.label("dir"),
                Message.created_at.label("at"),
                anterior_dir.label("prev_dir"),
                anterior_at.label("prev_at"),
            )
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(de_contacto, Message.created_at <= ahora)
            .subquery()
        )
        filas = await session.execute(
            select(secuencia.c.at, secuencia.c.prev_at).where(
                secuencia.c.dir == "inbound",
                secuencia.c.prev_dir == "outbound",
                secuencia.c.at >= desde,
            )
        )
        tiempos = [
            max((at - prev_at).total_seconds() / 60, 0.0) for at, prev_at in filas.tuples().all()
        ]

        primero = await session.execute(
            select(Message.direction)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(de_contacto)
            .order_by(Message.created_at, Message.id)
            .limit(1)
        )
        inicio_lead = primero.scalar_one_or_none() == "inbound"

    eventos = await session.execute(
        select(LeadActivity.activity_type, func.count())
        .where(
            LeadActivity.client_id == lead.client_id,
            LeadActivity.lead_id == lead.id,
            LeadActivity.activity_type.in_((ACTIVITY_EMAIL_OPENED, ACTIVITY_LINK_CLICKED)),
            LeadActivity.created_at >= desde,
            LeadActivity.created_at <= ahora,
        )
        .group_by(LeadActivity.activity_type)
    )
    por_tipo = dict(eventos.tuples().all())

    return BehavioralSignals(
        last_activity_at=lead.last_activity_at,
        inbound_messages=entrantes,
        outbound_messages=salientes,
        response_minutes=tuple(tiempos),
        lead_initiated=inicio_lead,
        email_opens=int(por_tipo.get(ACTIVITY_EMAIL_OPENED, 0)),
        link_clicks=int(por_tipo.get(ACTIVITY_LINK_CLICKED, 0)),
    )
