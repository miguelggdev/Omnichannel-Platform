"""Sincronizacion lead <-> contacto (Sprint 16, ADR-083).

Un lead es alguien a quien se le vende; un contacto es alguien con quien se conversa. Cuando son
la misma persona, enlazarlos deja que el agente y el panel sepan que quien escribe por WhatsApp
es un lead (y, en el Sprint 17, que su actividad mueva su score).

Reglas (todas por una razon concreta):

- **Solo se enlaza automaticamente lo inequivoco**: exactamente un contacto coincide por email
  o telefono, igual que la unificacion de contactos (`phone_unification`). Si hay dos o mas, no se
  sabe cual es y se deja al enlace manual; juntar a dos personas distintas es peor que no juntar.
- **Un contacto, un lead**: `contacts.lead_id` es una sola columna. Un contacto que ya apunta a
  otro lead no se reasigna en silencio.
- **Nunca cruza tenants** y nunca toca contactos fusionados ni anonimizados por RGPD.
- El telefono se compara con y sin `+` (WhatsApp lo entrega de las dos formas).
"""

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import blind_index
from app.models.contact import Contact
from app.models.contact_identifier import ContactIdentifier
from app.models.lead import Lead, normalizar_telefono_de_lead
from app.services.phone_unification import normalizar_telefono

logger = logging.getLogger(__name__)

#: Canales cuyo identificador es un telefono.
_CANALES_TELEFONO = ("whatsapp", "verified_phone")
_CANAL_EMAIL = "email"


class ContactoYaVinculadoError(Exception):
    """El contacto ya esta enlazado a otro lead."""

    def __init__(self, lead_id: UUID) -> None:
        super().__init__(f"El contacto ya esta vinculado al lead {lead_id}")
        self.lead_id = lead_id


async def contactos_candidatos(
    session: AsyncSession, client_id: UUID, email: str | None, phone: str | None
) -> set[UUID]:
    """Contactos del tenant que tienen ese email o ese telefono como identificador.

    Args:
        session: Sesion con el contexto del tenant fijado.
        client_id: Tenant.
        email: Email del lead, si lo tiene.
        phone: Telefono del lead, si lo tiene.

    Returns:
        Los ids de contactos coincidentes, sin los fusionados ni los anonimizados.
    """
    condiciones = []
    if email and email.strip():
        condiciones.append(
            (ContactIdentifier.channel == _CANAL_EMAIL)
            & (ContactIdentifier.identifier_hash == blind_index(email, client_id))
        )
    digitos = normalizar_telefono_de_lead(phone)
    if digitos:
        hashes = {
            h for h in (blind_index(digitos, client_id), blind_index(f"+{digitos}", client_id)) if h
        }
        condiciones.append(
            ContactIdentifier.channel.in_(_CANALES_TELEFONO)
            & ContactIdentifier.identifier_hash.in_(hashes)
        )
    if not condiciones:
        return set()
    consulta = (
        select(ContactIdentifier.contact_id)
        .join(Contact, Contact.id == ContactIdentifier.contact_id)
        .where(
            ContactIdentifier.client_id == client_id,
            Contact.client_id == client_id,
            Contact.merged_into_id.is_(None),
            Contact.is_gdpr_deleted.is_(False),
            condiciones[0] if len(condiciones) == 1 else condiciones[0] | condiciones[1],
        )
        .distinct()
    )
    return set((await session.execute(consulta)).scalars())


async def contacto_inequivoco(
    session: AsyncSession, client_id: UUID, email: str | None, phone: str | None
) -> UUID | None:
    """El contacto que corresponde al lead, solo si hay exactamente uno y esta libre.

    Args:
        session: Sesion con el contexto del tenant fijado.
        client_id: Tenant.
        email: Email del lead.
        phone: Telefono del lead.

    Returns:
        El id del contacto, o `None` si no hay ninguno, hay varios o ya esta enlazado a un lead.
    """
    candidatos = await contactos_candidatos(session, client_id, email, phone)
    if len(candidatos) != 1:
        return None
    (contacto_id,) = candidatos
    contacto = (
        await session.execute(
            select(Contact).where(Contact.id == contacto_id, Contact.client_id == client_id)
        )
    ).scalar_one_or_none()
    if contacto is None or contacto.lead_id is not None:
        return None
    return contacto_id


async def vincular(session: AsyncSession, lead: Lead, contacto: Contact) -> None:
    """Enlaza un lead y un contacto por los dos lados.

    Si el lead ya estaba enlazado a otro contacto, se suelta ese primero.

    Args:
        session: Sesion con el contexto del tenant fijado.
        lead: Lead (del mismo tenant que el contacto).
        contacto: Contacto a enlazar.

    Raises:
        ContactoYaVinculadoError: Si el contacto ya apunta a otro lead.
    """
    if contacto.lead_id is not None and contacto.lead_id != lead.id:
        raise ContactoYaVinculadoError(contacto.lead_id)
    if lead.contact_id is not None and lead.contact_id != contacto.id:
        await desvincular(session, lead)
    contacto.lead_id = lead.id
    contacto.is_lead = True
    lead.contact_id = contacto.id
    await session.flush()


async def desvincular(session: AsyncSession, lead: Lead) -> bool:
    """Suelta el enlace con el contacto, por los dos lados.

    Args:
        session: Sesion con el contexto del tenant fijado.
        lead: Lead a desenlazar.

    Returns:
        `True` si tenia un contacto enlazado.
    """
    if lead.contact_id is None:
        return False
    contacto = (
        await session.execute(
            select(Contact).where(
                Contact.id == lead.contact_id, Contact.client_id == lead.client_id
            )
        )
    ).scalar_one_or_none()
    if contacto is not None and contacto.lead_id == lead.id:
        contacto.lead_id = None
        contacto.is_lead = False
    lead.contact_id = None
    await session.flush()
    return True


async def crear_contacto_desde_lead(session: AsyncSession, lead: Lead) -> Contact:
    """Crea un contacto con los datos del lead (y sus identificadores) y lo enlaza.

    El email pasa a ser un identificador del canal `email`; el telefono, si tiene forma de
    numero internacional, a uno de `whatsapp` con `+` (como lo entrega el proveedor), de modo
    que cuando esa persona escriba por WhatsApp se reconozca como este contacto.

    Args:
        session: Sesion con el contexto del tenant fijado.
        lead: Lead de origen.

    Returns:
        El contacto nuevo, ya enlazado.

    Raises:
        ContactoYaVinculadoError: Nunca por el contacto nuevo; se declara por simetria con
            `vincular`.
    """
    nombre = " ".join(p for p in (lead.first_name, lead.last_name) if p) or None
    contacto = Contact(
        client_id=lead.client_id,
        first_name=lead.first_name,
        last_name=lead.last_name,
        display_name=nombre or lead.company_name,
    )
    session.add(contacto)
    await session.flush()
    if lead.email:
        session.add(
            ContactIdentifier(
                client_id=lead.client_id,
                contact_id=contacto.id,
                channel=_CANAL_EMAIL,
                identifier_value=lead.email.strip().lower(),
            )
        )
    canonico = normalizar_telefono(lead.phone)
    if canonico:
        session.add(
            ContactIdentifier(
                client_id=lead.client_id,
                contact_id=contacto.id,
                channel="whatsapp",
                identifier_value=f"+{canonico}",
            )
        )
    await vincular(session, lead, contacto)
    logger.info("Contacto %s creado desde el lead %s", contacto.id, lead.id)
    return contacto
