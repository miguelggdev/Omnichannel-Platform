"""Unificacion automatica de contactos por telefono verificado.

Una misma persona puede escribir por varios canales; sin unificar, el agente la
trata como desconocidos distintos y pierde el historial. Pero **unir dos
contactos que no son la misma persona es peor que no unirlos**: mezcla
conversaciones, notas y etiquetas de gente distinta. Por eso la unificacion
automatica se limita a la coincidencia inequivoca y todo lo demas queda para la
fusion manual (`POST /contacts/{id}/merge`, `ContactUnifier`).

**Que cuenta como coincidencia inequivoca.** Todas estas condiciones, a la vez:

1. **Telefono verificado por el propio canal.** En WhatsApp es el remitente (el
   proveedor verifica el numero). En Telegram solo cuenta el contacto que el
   usuario comparte *de si mismo* (`contact.user_id == from.id`, sin reenvio): un
   contacto ajeno o reenviado no prueba nada. Instagram, Facebook y email no
   traen telefono y nunca unifican; un id de Telegram que "se parece" a un
   numero tampoco.
2. **Mismo tenant.** Toda consulta filtra por `client_id` (ademas de RLS) y el
   indice ciego incluye el tenant en el HMAC: el mismo numero en dos tenants
   produce hashes distintos y jamas se cruza.
3. **Exactamente un otro contacto** conoce ese telefono. Con dos o mas (p. ej.
   un duplicado previo por formato `+57…`/`57…`) no se sabe cual es el correcto:
   no se une y se deja para revision manual.
4. **Sin telefonos contradictorios:** si alguno de los dos contactos ya tiene
   *otro* telefono, no son la misma persona con certeza (o el numero cambio de
   dueno).
5. **Sin canales solapados:** si ambos ya tienen un identificador en el mismo
   canal (dos cuentas de Telegram, por ejemplo), tampoco.
6. **Ninguno fue borrado por GDPR.**

El telefono verificado de un canal que no es WhatsApp (Telegram) se guarda como
un identificador de canal `verified_phone`, cifrado como cualquier otro, para que
un mensaje posterior de WhatsApp con ese numero encuentre el contacto de
Telegram. No es un canal por el que se pueda responder: `get_contact_identifier`
filtra por canal y nunca lo devuelve.

**Concurrencia.** Si la misma persona escribe por dos canales a la vez, dos
tareas podrian decidir fusionarse en sentidos opuestos y dejar un ciclo de
`merged_into_id`. Un lock advisory transaccional por (tenant, telefono) las
serializa; la segunda ya ve lo que commiteo la primera.

**Trazabilidad.** El superviviente guarda en `metadata.unifications` el id del
contacto absorbido y el motivo (sin datos personales), para poder auditar y, si
hiciera falta, deshacer a mano.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select, text

from app.core.encryption import blind_index
from app.models.contact import Contact
from app.models.contact_identifier import ContactIdentifier
from app.services.contact_unifier import ContactUnifier

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.schemas.message import NormalizedMessage

logger = logging.getLogger(__name__)

#: Canal de `contact_identifiers` para un telefono verificado por un canal que no
#: es WhatsApp. No es un canal de mensajeria (`ChannelEnum.phone` es otra cosa).
VERIFIED_PHONE_CHANNEL = "verified_phone"

#: Canales cuyo identificador ES un telefono (o lo prueba). Cuentan como "la
#: misma clase" al comparar telefonos y no al comparar canales solapados.
_CANALES_TELEFONO = frozenset({"whatsapp", VERIFIED_PHONE_CHANNEL})

#: Canales que pueden aportar un telefono verificado.
CANALES_CON_TELEFONO_VERIFICADO = frozenset({"whatsapp", "telegram"})

#: Profundidad maxima al seguir `merged_into_id` (proteccion ante ciclos).
_MAX_CADENA_MERGE = 20

#: Entradas de `metadata.unifications` que se conservan.
_MAX_TRAZA = 20

_SOLO_DIGITOS = re.compile(r"^\d{8,15}$")


class Resultado(StrEnum):
    """Que hizo la unificacion."""

    SIN_CAMBIOS = "sin_cambios"
    REGISTRADO = "registrado"
    FUSIONADO = "fusionado"
    AMBIGUO = "ambiguo"
    CONFLICTO = "conflicto"


@dataclass(frozen=True)
class Resolucion:
    """Resultado de `unificar_por_telefono`.

    Attributes:
        contacto: Contacto con el que hay que seguir (el superviviente si hubo
            fusion; el mismo que entro en cualquier otro caso).
        resultado: Que se hizo.
    """

    contacto: Contact
    resultado: Resultado


@dataclass(frozen=True)
class Ficha:
    """Lo que la decision necesita saber de un contacto, sin tocar la base.

    Attributes:
        contact_id: Id del contacto.
        creado: Fecha de creacion (el mas antiguo sobrevive).
        borrado_gdpr: Si fue anonimizado por una solicitud GDPR.
        hashes_telefono: Hashes de sus identificadores de clase telefono.
        canales: Canales de mensajeria en los que tiene identificador (sin los
            de clase telefono).
    """

    contact_id: UUID
    creado: datetime
    borrado_gdpr: bool
    hashes_telefono: frozenset[str]
    canales: frozenset[str]


@dataclass(frozen=True)
class Decision:
    """Decision pura sobre dos contactos.

    Attributes:
        resultado: `FUSIONADO` si procede unir; `CONFLICTO` si no.
        origen: Contacto que se absorbe (solo si procede).
        destino: Contacto que sobrevive (solo si procede).
        motivo: Por que no se une (solo si `CONFLICTO`).
    """

    resultado: Resultado
    origen: UUID | None = None
    destino: UUID | None = None
    motivo: str = ""


def normalizar_telefono(valor: str | None) -> str | None:
    """Lleva un telefono a su forma canonica: solo digitos, E.164 sin `+`.

    Args:
        valor: Telefono tal como lo entrega el canal (`+57 300 111-2233`).

    Returns:
        Entre 8 y 15 digitos sin cero inicial (un codigo de pais nunca empieza
        en 0), o `None` si no parece un telefono internacional. El `None` es
        deliberado: ante la duda no se unifica.
    """
    if not valor:
        return None
    digitos = re.sub(r"[\s\-().+]", "", valor)
    if not _SOLO_DIGITOS.match(digitos) or digitos.startswith("0"):
        return None
    return digitos


def telefono_verificado(mensaje: "NormalizedMessage") -> str | None:
    """Telefono que el canal prueba que pertenece a quien escribe.

    Args:
        mensaje: Mensaje entrante normalizado.

    Returns:
        El telefono canonico, o `None` si el mensaje no prueba ninguno.
    """
    canal = mensaje.channel.value
    if canal not in CANALES_CON_TELEFONO_VERIFICADO:
        return None
    if canal == "whatsapp":
        return normalizar_telefono(mensaje.sender_identifier)
    return normalizar_telefono(mensaje.verified_phone)


def decidir(actual: Ficha, otro: Ficha, hashes_telefono: frozenset[str]) -> Decision:
    """Decide si dos contactos que comparten un telefono verificado se unen.

    Funcion pura: recibe fichas ya cargadas y no toca la base, para poder
    probarla exhaustivamente.

    Args:
        actual: Contacto del mensaje que se esta procesando.
        otro: El unico otro contacto que conoce el telefono.
        hashes_telefono: Hashes del telefono verificado (con y sin `+`).

    Returns:
        `FUSIONADO` con origen/destino (el mas antiguo sobrevive), o `CONFLICTO`
        con el motivo.
    """
    if actual.borrado_gdpr or otro.borrado_gdpr:
        return Decision(Resultado.CONFLICTO, motivo="contacto borrado por GDPR")

    for ficha in (actual, otro):
        if not ficha.hashes_telefono <= hashes_telefono:
            return Decision(Resultado.CONFLICTO, motivo="telefonos contradictorios")

    if actual.canales & otro.canales:
        return Decision(Resultado.CONFLICTO, motivo="mismo canal en ambos contactos")

    # Desempate por id para que las dos tareas concurrentes elijan lo mismo.
    destino, origen = sorted((actual, otro), key=lambda f: (f.creado, str(f.contact_id)))
    return Decision(Resultado.FUSIONADO, origen=origen.contact_id, destino=destino.contact_id)


def _hashes(telefono: str, client_id: UUID) -> frozenset[str]:
    """Hashes ciegos de un telefono canonico, con y sin `+`.

    WhatsApp entrega el numero a veces como `+573001112233` y a veces como
    `573001112233`, y el indice ciego solo normaliza mayusculas y espacios.

    Args:
        telefono: Telefono canonico (solo digitos).
        client_id: Tenant (entra en el HMAC).

    Returns:
        Los dos hashes.
    """
    return frozenset(
        h for h in (blind_index(telefono, client_id), blind_index(f"+{telefono}", client_id)) if h
    )


async def _contacto(session: "AsyncSession", client_id: UUID, contact_id: UUID) -> Contact | None:
    """Carga un contacto del tenant, con filtro explicito ademas de RLS.

    Args:
        session: Sesion con contexto de tenant.
        client_id: Tenant.
        contact_id: Contacto.

    Returns:
        El contacto, o `None` si no existe en ese tenant.
    """
    return (
        await session.execute(
            select(Contact).where(Contact.id == contact_id, Contact.client_id == client_id)
        )
    ).scalar_one_or_none()


async def _superviviente(
    session: "AsyncSession", client_id: UUID, contact_id: UUID
) -> Contact | None:
    """Sigue `merged_into_id` hasta el contacto vigente.

    Args:
        session: Sesion con contexto de tenant.
        client_id: Tenant.
        contact_id: Contacto del que se parte.

    Returns:
        El contacto vigente, o `None` si la cadena esta rota o es cíclica.
    """
    visto: set[UUID] = set()
    actual = await _contacto(session, client_id, contact_id)
    while actual is not None and actual.merged_into_id is not None:
        if actual.id in visto or len(visto) >= _MAX_CADENA_MERGE:
            logger.error("Cadena de merges invalida en el contacto %s", actual.id)
            return None
        visto.add(actual.id)
        actual = await _contacto(session, client_id, actual.merged_into_id)
    return actual


async def _ficha(session: "AsyncSession", client_id: UUID, contacto: Contact) -> Ficha:
    """Carga los identificadores de un contacto y arma su ficha.

    Args:
        session: Sesion con contexto de tenant.
        client_id: Tenant.
        contacto: Contacto vigente.

    Returns:
        Su ficha.
    """
    creado, borrado_gdpr = (
        await session.execute(
            select(Contact.created_at, Contact.is_gdpr_deleted).where(
                Contact.id == contacto.id, Contact.client_id == client_id
            )
        )
    ).one()
    filas = (
        await session.execute(
            select(ContactIdentifier.channel, ContactIdentifier.identifier_hash).where(
                ContactIdentifier.client_id == client_id,
                ContactIdentifier.contact_id == contacto.id,
            )
        )
    ).all()
    return Ficha(
        contact_id=contacto.id,
        creado=creado,
        borrado_gdpr=bool(borrado_gdpr),
        hashes_telefono=frozenset(h for c, h in filas if c in _CANALES_TELEFONO),
        canales=frozenset(c for c, _ in filas if c not in _CANALES_TELEFONO),
    )


async def _otros_titulares(
    session: "AsyncSession", client_id: UUID, actual: UUID, hashes: frozenset[str]
) -> dict[UUID, Contact]:
    """Contactos vigentes, distintos del actual, que conocen el telefono.

    Args:
        session: Sesion con contexto de tenant.
        client_id: Tenant.
        actual: Contacto del mensaje.
        hashes: Hashes del telefono verificado.

    Returns:
        Contactos vigentes por id. Una cadena de merge rota descarta ese titular
        en vez de adivinar.
    """
    ids = (
        (
            await session.execute(
                select(ContactIdentifier.contact_id).where(
                    ContactIdentifier.client_id == client_id,
                    ContactIdentifier.channel.in_(_CANALES_TELEFONO),
                    ContactIdentifier.identifier_hash.in_(hashes),
                )
            )
        )
        .scalars()
        .all()
    )
    titulares: dict[UUID, Contact] = {}
    for contact_id in set(ids):
        vigente = await _superviviente(session, client_id, contact_id)
        if vigente is not None and vigente.id != actual:
            titulares[vigente.id] = vigente
    return titulares


async def unificar_por_telefono(
    session: "AsyncSession",
    client_id: UUID,
    contacto: Contact,
    telefono: str,
) -> Resolucion:
    """Une `contacto` con el unico otro contacto que tenga el mismo telefono verificado.

    Debe llamarse dentro de la transaccion del mensaje, con `contacto` ya
    persistido (con `flush`) y siendo el contacto vigente (no fusionado).

    Args:
        session: Sesion con el contexto de tenant aplicado.
        client_id: Tenant del mensaje.
        contacto: Contacto que escribio.
        telefono: Telefono canonico verificado por el canal (`normalizar_telefono`).

    Returns:
        El contacto con el que seguir y que se hizo. Ante cualquier duda
        (`AMBIGUO`, `CONFLICTO`) el contacto sale intacto y no se toca nada.
    """
    hashes = _hashes(telefono, client_id)
    if not hashes:
        return Resolucion(contacto, Resultado.SIN_CAMBIOS)

    # Serializa a las tareas que tratan el mismo telefono del mismo tenant. La
    # clave es el propio hash ciego: no lleva el numero en claro.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:clave, 0))"),
        {"clave": f"phone-unification:{sorted(hashes)[0]}"},
    )

    otros = await _otros_titulares(session, client_id, contacto.id, hashes)
    ficha_actual = await _ficha(session, client_id, contacto)

    if not otros:
        if ficha_actual.hashes_telefono & hashes:
            return Resolucion(contacto, Resultado.SIN_CAMBIOS)
        if ficha_actual.hashes_telefono:
            logger.warning(
                "Contacto %s ya tiene otro telefono verificado; no se registra", contacto.id
            )
            return Resolucion(contacto, Resultado.CONFLICTO)
        await _registrar(session, client_id, contacto.id, telefono)
        return Resolucion(contacto, Resultado.REGISTRADO)

    if len(otros) > 1:
        logger.warning(
            "Telefono verificado compartido por %d contactos del tenant %s; no se unifica "
            "(contacto %s). Requiere revision manual.",
            len(otros) + 1,
            client_id,
            contacto.id,
        )
        return Resolucion(contacto, Resultado.AMBIGUO)

    (otro,) = otros.values()
    decision = decidir(ficha_actual, await _ficha(session, client_id, otro), hashes)
    if decision.resultado is not Resultado.FUSIONADO:
        logger.warning(
            "No se unifican los contactos %s y %s: %s", contacto.id, otro.id, decision.motivo
        )
        return Resolucion(contacto, decision.resultado)

    if decision.origen is None or decision.destino is None:
        return Resolucion(contacto, Resultado.CONFLICTO)
    await ContactUnifier(session).merge(decision.origen, decision.destino, client_id)

    superviviente = contacto if decision.destino == contacto.id else otro
    await _dejar_traza(session, client_id, superviviente, decision.origen)
    logger.info(
        "Contactos %s y %s unificados por telefono verificado", decision.origen, decision.destino
    )
    return Resolucion(superviviente, Resultado.FUSIONADO)


async def _registrar(
    session: "AsyncSession", client_id: UUID, contact_id: UUID, telefono: str
) -> None:
    """Guarda el telefono verificado como identificador `verified_phone`.

    Args:
        session: Sesion con contexto de tenant.
        client_id: Tenant.
        contact_id: Contacto que lo prueba.
        telefono: Telefono canonico.
    """
    session.add(
        ContactIdentifier(
            client_id=client_id,
            contact_id=contact_id,
            channel=VERIFIED_PHONE_CHANNEL,
            identifier_value=telefono,
        )
    )
    await session.flush()


async def _dejar_traza(
    session: "AsyncSession", client_id: UUID, superviviente: Contact, absorbido: UUID
) -> None:
    """Anota en el superviviente que absorbio a otro contacto y por que.

    Args:
        session: Sesion con contexto de tenant.
        client_id: Tenant.
        superviviente: Contacto que sobrevive.
        absorbido: Id del contacto fusionado en el.
    """
    vigente = await _contacto(session, client_id, superviviente.id)
    if vigente is None:
        return
    previas = list((vigente.metadata_ or {}).get("unifications", []))
    previas.append(
        {
            "source_id": str(absorbido),
            "reason": "verified_phone",
            "at": datetime.now(timezone.utc).isoformat(),
        }
    )
    # Dict nuevo: SQLAlchemy no detecta mutaciones in situ de un JSONB.
    vigente.metadata_ = {**(vigente.metadata_ or {}), "unifications": previas[-_MAX_TRAZA:]}
