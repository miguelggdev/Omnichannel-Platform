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

`sentiment_avg` (que el spec menciona) no esta: el sentimiento por mensaje lo
introduce el nodo de Dev B del Sprint 10 y todavia no hay un promedio por
contacto que consultar. Pedirlo da error en vez de colarse sin filtrar.

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
from app.models.tag import Tag

logger = logging.getLogger(__name__)

#: Claves que `resolver_segmento()` sabe interpretar.
CRITERIOS_SOPORTADOS: frozenset[str] = frozenset(
    {"tags", "channel", "last_active_days", "score_min", "metadata"}
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
