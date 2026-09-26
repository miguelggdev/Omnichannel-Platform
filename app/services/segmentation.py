"""Segmentacion de contactos para campanas de marketing (Sprint 12).

Un criterio que no se entiende NO se ignora: `resolver_segmento()` lanza
`CriterioInvalidoError`. Ignorarlo en silencio es la forma mas facil de mandar
una campana a mas gente de la que el tenant creia haber elegido — pide "los
etiquetados VIP con score alto", el filtro de score no existe, y sale a todos
los VIP. Es el mismo criterio de "fallar ruidoso" que el resto del proyecto.

Criterios soportados
---------------------
- `tags`: nombres de etiqueta; el contacto debe tenerlas **todas**.
- `channel`: solo contactos con identificador en ese canal (el que se usa para
  enviarles).
- `last_active_days`: con algun mensaje en los ultimos N dias.
- `score_min`: score minimo, leido de `contacts.metadata->>'score'`. Ese campo
  lo escribe el scoring predictivo de Dev B (§7 del spec); si todavia no
  existe, el filtro simplemente no encuentra a nadie.
- `metadata`: pares clave/valor que deben estar en `contacts.metadata`.

- `sentiment_avg`: sentimiento promedio (0-100) de los mensajes del contacto
  en los ultimos 30 dias, medido por el nodo `sentiment_analysis` (Sprint 10).
  Un numero es el minimo; `{"min": x, "max": y}` acota por los dos lados (para
  una campana de recuperacion interesa el maximo). Misma escala y ventana que
  el scoring (`contact_scoring.puntaje_de_sentimiento`). Un contacto **sin**
  mensajes medidos no entra: no se sabe como esta, y tratarlo como neutral
  seria inventarle un dato.

Nunca entran en un segmento los contactos fusionados (`merged_into_id`) ni los
borrados por RGPD (`is_gdpr_deleted`): los primeros duplicarian el envio en la
persona que sobrevivio, y los segundos pidieron expresamente que no se les
contacte.
"""

import json
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, Numeric, Select, and_, case, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contact import Contact
from app.models.contact_identifier import ContactIdentifier
from app.models.contact_tag import ContactTag
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.tag import Tag
from app.services.contact_scoring import VENTANA_ACTIVIDAD_DIAS, puntaje_de_sentimiento

logger = logging.getLogger(__name__)

#: Claves que `resolver_segmento()` sabe interpretar.
CRITERIOS_SOPORTADOS: frozenset[str] = frozenset(
    {"tags", "channel", "last_active_days", "score_min", "metadata", "sentiment_avg"}
)


#: Tope de `last_active_days`: un siglo.
MAX_DIAS_ACTIVIDAD = 36_500


class CriterioInvalidoError(ValueError):
    """Los criterios de segmentacion traen algo que no se puede aplicar."""


def _validar(criterios: dict[str, Any]) -> None:
    """Rechaza criterios desconocidos o mal tipados.

    Args:
        criterios: Criterios tal como vienen de la campana.

    Raises:
        CriterioInvalidoError: Si hay una clave desconocida o un tipo que no
            corresponde.
    """
    desconocidos = sorted(set(criterios) - CRITERIOS_SOPORTADOS)
    if desconocidos:
        raise CriterioInvalidoError(
            f"Criterios no soportados: {', '.join(desconocidos)}. "
            f"Validos: {', '.join(sorted(CRITERIOS_SOPORTADOS))}"
        )

    if "tags" in criterios and (
        not isinstance(criterios["tags"], list)
        or not all(isinstance(tag, str) for tag in criterios["tags"])
    ):
        raise CriterioInvalidoError("`tags` tiene que ser una lista de nombres de etiqueta")
    if "metadata" in criterios and not isinstance(criterios["metadata"], dict):
        raise CriterioInvalidoError("`metadata` tiene que ser un objeto clave/valor")
    for numerico in ("last_active_days", "score_min"):
        valor = criterios.get(numerico)
        # `bool` es subclase de `int` en Python: sin excluirlo, `score_min: true`
        # pasaba la validacion y filtraba por `score >= 1.0`.
        if numerico in criterios and (
            isinstance(valor, bool)
            or not isinstance(valor, (int, float))
            or not math.isfinite(valor)
        ):
            raise CriterioInvalidoError(f"`{numerico}` tiene que ser un numero")
    # `timedelta(days=1e10)` levanta OverflowError y `NaN`/`inf` no son JSON
    # valido para PostgreSQL: los dos tumbaban la consulta con un 500 en vez de
    # un 400 (BUG-045). Un segmento por actividad de mas de un siglo no tiene
    # sentido, y uno negativo tampoco.
    dias = criterios.get("last_active_days")
    if dias is not None and not 0 <= dias <= MAX_DIAS_ACTIVIDAD:
        raise CriterioInvalidoError(
            f"`last_active_days` tiene que estar entre 0 y {MAX_DIAS_ACTIVIDAD}"
        )
    if "sentiment_avg" in criterios:
        _rango_de_sentimiento(criterios["sentiment_avg"])
    for clave, valor in (criterios.get("metadata") or {}).items():
        if isinstance(valor, float) and not math.isfinite(valor):
            raise CriterioInvalidoError(f"`metadata.{clave}` tiene que ser un numero finito")


def _coincide_metadata(clave: str, valor: Any) -> ColumnElement[bool]:
    """Condicion de un criterio `metadata`: la clave vale `valor`.

    Contencion JSONB (`metadata @> '{"clave": valor}'`), no
    `metadata->>'clave' = str(valor)`: el `str()` de Python no es el texto de
    JSON, asi que `{"vip": true}` buscaba "True" contra el "true" guardado y
    `{"nivel": 1.0}` buscaba "1.0" contra "1" — el filtro no encontraba a
    nadie (BUG-044). La contencion compara con el tipo, y los numeros por
    valor (1 = 1.0).

    Pero `contacts.metadata` tambien lo escribe el tenant por la API del CRM, y
    ahi un numero o un booleano puede llegar como texto (`"5"`, `"true"`). Para
    un criterio escalar que no es texto se acepta ademas el valor guardado como
    texto con su representacion JSON (`json.dumps`), que es lo que el tenant
    escribiria: `{"nivel": 5}` encuentra `5` y `"5"`, y `{"vip": true}`
    encuentra `true` y `"true"`.

    Args:
        clave: Clave de primer nivel de `metadata`.
        valor: Valor buscado, tal como vino en los criterios.

    Returns:
        La condicion para el WHERE.
    """
    por_json = Contact.metadata_.contains({clave: valor})
    if isinstance(valor, bool | int | float):
        como_texto = Contact.metadata_.contains({clave: json.dumps(valor)})
        return or_(por_json, como_texto)
    return por_json


def _es_puntaje(valor: Any) -> bool:
    """Si `valor` es un numero finito entre 0 y 100 (y no un booleano)."""
    return (
        not isinstance(valor, bool)
        and isinstance(valor, (int, float))
        and math.isfinite(valor)
        and 0 <= valor <= 100
    )


def _rango_de_sentimiento(valor: Any) -> tuple[float | None, float | None]:
    """Interpreta el criterio `sentiment_avg`.

    Args:
        valor: Un numero (minimo) o `{"min": x, "max": y}`, en escala 0-100.

    Returns:
        `(minimo, maximo)`; cualquiera de los dos puede ser `None`.

    Raises:
        CriterioInvalidoError: Si no tiene una de esas dos formas, algun extremo
            no esta entre 0 y 100, o el minimo supera al maximo.
    """
    error = CriterioInvalidoError(
        "`sentiment_avg` tiene que ser un numero entre 0 y 100 (minimo) o "
        '{"min": x, "max": y} con valores entre 0 y 100'
    )
    if _es_puntaje(valor):
        return float(valor), None
    if not isinstance(valor, dict) or not valor or set(valor) - {"min", "max"}:
        raise error
    minimo, maximo = valor.get("min"), valor.get("max")
    for extremo in (minimo, maximo):
        if extremo is not None and not _es_puntaje(extremo):
            raise error
    if minimo is not None and maximo is not None and minimo > maximo:
        raise CriterioInvalidoError("`sentiment_avg.min` no puede ser mayor que `max`")
    return (
        None if minimo is None else float(minimo),
        None if maximo is None else float(maximo),
    )


def construir_query(client_id: UUID, criterios: dict[str, Any]) -> Select[tuple[Contact]]:
    """Arma la consulta de contactos que cumplen los criterios.

    Args:
        client_id: Tenant dueno de los contactos.
        criterios: Criterios de segmentacion.

    Returns:
        SELECT de `Contact` con todos los filtros aplicados.

    Raises:
        CriterioInvalidoError: Si algun criterio no se puede aplicar.
    """
    _validar(criterios)

    stmt = select(Contact).where(
        Contact.client_id == client_id,
        Contact.merged_into_id.is_(None),
        Contact.is_gdpr_deleted.is_(False),
    )

    canal = criterios.get("channel")
    if canal:
        stmt = stmt.where(
            exists().where(
                and_(
                    ContactIdentifier.contact_id == Contact.id,
                    ContactIdentifier.client_id == client_id,
                    ContactIdentifier.channel == canal,
                )
            )
        )

    for nombre_tag in criterios.get("tags", []):
        # Una subconsulta por etiqueta: el contacto tiene que tenerlas todas,
        # no al menos una (un IN daria la union, que es justo lo contrario).
        stmt = stmt.where(
            exists().where(
                and_(
                    ContactTag.contact_id == Contact.id,
                    ContactTag.client_id == client_id,
                    ContactTag.tag_id == Tag.id,
                    Tag.client_id == client_id,
                    Tag.name == nombre_tag,
                )
            )
        )

    dias = criterios.get("last_active_days")
    if dias is not None:
        desde = datetime.now(timezone.utc) - timedelta(days=float(dias))
        stmt = stmt.where(
            exists().where(
                and_(
                    Conversation.contact_id == Contact.id,
                    Conversation.client_id == client_id,
                    Conversation.last_message_at >= desde,
                )
            )
        )

    score_min = criterios.get("score_min")
    if score_min is not None:
        # El score lo escribe el scoring predictivo de Dev B en metadata, pero
        # `contacts.metadata` tambien lo edita el tenant por la API del CRM: un
        # `score: "alto"` haria fallar el CAST y con el la campana entera. El
        # CASE garantiza el orden de evaluacion (un AND no lo garantiza: el
        # planificador puede reordenar), asi que solo se castea lo que ya se
        # comprobo que es un numero.
        score_texto = Contact.metadata_["score"].astext
        es_numero = score_texto.op("~")(r"^-?[0-9]+(\.[0-9]+)?$")
        stmt = stmt.where(
            case((es_numero, score_texto.cast(Numeric)), else_=None) >= float(score_min)
        )

    if "sentiment_avg" in criterios:
        minimo, maximo = _rango_de_sentimiento(criterios["sentiment_avg"])
        desde = datetime.now(timezone.utc) - timedelta(days=VENTANA_ACTIVIDAD_DIAS)
        # Subconsulta correlacionada por contacto. AVG da NULL si no hay
        # mensajes medidos, y NULL no cumple ninguna comparacion: el contacto
        # queda fuera, que es lo que se quiere (ver el docstring del modulo).
        promedio = (
            select(func.avg(puntaje_de_sentimiento()))
            .select_from(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Conversation.contact_id == Contact.id,
                Conversation.client_id == client_id,
                Message.client_id == client_id,
                Message.direction == "inbound",
                Message.created_at >= desde,
            )
            .scalar_subquery()
        )
        if minimo is not None:
            stmt = stmt.where(promedio >= minimo)
        if maximo is not None:
            stmt = stmt.where(promedio <= maximo)

    for clave, valor in (criterios.get("metadata") or {}).items():
        stmt = stmt.where(_coincide_metadata(clave, valor))

    return stmt


async def contar_segmento(session: AsyncSession, client_id: UUID, criterios: dict[str, Any]) -> int:
    """Cuenta los contactos que cumplen los criterios.

    Args:
        session: Sesion con el contexto de tenant ya aplicado.
        client_id: Tenant dueno de los contactos.
        criterios: Criterios de segmentacion.

    Returns:
        Cuantos contactos entran en el segmento.

    Raises:
        CriterioInvalidoError: Si algun criterio no se puede aplicar.
    """
    base = construir_query(client_id, criterios).subquery()
    return int((await session.execute(select(func.count()).select_from(base))).scalar_one())


async def resolver_segmento(
    session: AsyncSession, client_id: UUID, criterios: dict[str, Any], limite: int | None = None
) -> list[Contact]:
    """Devuelve los contactos que cumplen los criterios.

    Args:
        session: Sesion con el contexto de tenant ya aplicado.
        client_id: Tenant dueno de los contactos.
        criterios: Criterios de segmentacion.
        limite: Maximo de contactos a devolver, opcional.

    Returns:
        Contactos del segmento, ordenados por fecha de creacion.

    Raises:
        CriterioInvalidoError: Si algun criterio no se puede aplicar.
    """
    stmt = construir_query(client_id, criterios).order_by(Contact.created_at.asc())
    if limite is not None:
        stmt = stmt.limit(limite)
    return list((await session.execute(stmt)).scalars().all())
