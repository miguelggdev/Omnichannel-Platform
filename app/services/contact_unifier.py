"""ContactUnifier — fusión manual de contactos duplicados.

Contrato exacto (`app/api/v1/contacts.py::merge_contacts()`, ya entregado por
Dev B): `ContactUnifier(session).merge(source_id=..., target_id=...)`, sobre
una `session` ya abierta con `tenant_session()`. El endpoint valida antes de
llamar — `source_id != target_id`, ambos contactos existen, ninguno está ya
fusionado (`merged_into_id is not None`) — así que `merge()` no repite esas
comprobaciones; solo mueve las filas y marca el origen.

Solo se implementa `merge()`, no el `resolve_contact()` que trae el spec §8: esa
resolución automática (buscar contacto por `channel` + `identifier_value` al
recibir un mensaje, o crear uno si no existe) ya la tiene
`app/tasks/webhook_processor.py::_resolve_contact()` desde Sprint 4, con su
propia protección de ciclos de merge y ya probada en producción. Reimplementarla
acá crearía dos versiones de la misma lógica que se pueden desincronizar con el
tiempo; el flujo automático sigue viviendo solo en el webhook.

Desviación sobre el spec: `identifier_value` se busca/mueve en texto plano.
El spec asume `pgp_sym_encrypt`/`pgp_sym_decrypt` y una columna `is_primary`
en `contact_identifiers` que no existen — el cifrado de columnas sensibles es
un entregable de Sprint 8 (`app/core/encryption.py`, METHODOLOGY.md) que
todavía no existe, y ni el modelo real (`app/models/contact_identifier.py`) ni
la migración de Sprint 1 declaran `is_primary`. `merge()` no toca
`identifier_value` en absoluto (solo reasigna `contact_id`), así que no
depende de si esa columna termina cifrada o no.

`merge()` no hace `commit()`: la transacción la abre y cierra quien llama
(`tenant_session()` en el endpoint), mismo patrón que
`app/services/conversation_lifecycle.py::ConversationLifecycle.transition()`.
"""

import logging
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contact import Contact
from app.models.contact_identifier import ContactIdentifier
from app.models.contact_tag import ContactTag
from app.models.conversation import Conversation
from app.models.internal_note import InternalNote

logger = logging.getLogger(__name__)


class ContactNotFoundError(RuntimeError):
    """El contacto origen no existe en la sesión (no debería pasar: ver el docstring)."""


class ContactUnifier:
    """Fusiona un contacto duplicado (`source`) dentro de otro (`target`).

    Attributes:
        session: Sesión con el contexto de tenant ya aplicado (`SET LOCAL`).
    """

    def __init__(self, session: AsyncSession) -> None:
        """Guarda la sesión de trabajo.

        Args:
            session: `AsyncSession` con contexto de tenant.
        """
        self.session = session

    async def merge(self, source_id: UUID, target_id: UUID) -> None:
        """Mueve todo lo del contacto origen al destino y marca el origen fusionado.

        Mueve, en este orden: identificadores de canal, conversaciones, notas
        internas y etiquetas (sin duplicar las que el destino ya tenía). Al
        final, marca `source.merged_into_id = target_id`.

        Args:
            source_id: Contacto duplicado que se absorbe.
            target_id: Contacto que sobrevive a la fusión.

        Raises:
            ContactNotFoundError: Si `source_id` no resuelve a un contacto en
                esta sesión (el endpoint ya lo valida antes de llamar; solo
                pasaría ante una fila borrada entre la validación y esta llamada).
        """
        logger.info("Fusionando contacto %s en %s", source_id, target_id)

        # Se busca el origen primero (no al final, como en el spec) para tener
        # su client_id y filtrar con él el resto de las operaciones: RLS ya
        # aisla por tenant, pero source_id/target_id salen de la URL de un
        # endpoint HTTP, no de un valor inyectado server-side, así que llevan
        # la misma defensa explícita que el resto del proyecto (ver
        # `get_contact_or_404` en `app/api/v1/contacts.py`).
        source = await self.session.get(Contact, source_id)
        if source is None:
            raise ContactNotFoundError(f"Contacto origen {source_id} no encontrado")
        client_id = source.client_id

        await self.session.execute(
            update(ContactIdentifier)
            .where(
                ContactIdentifier.contact_id == source_id, ContactIdentifier.client_id == client_id
            )
            .values(contact_id=target_id)
        )
        await self.session.execute(
            update(Conversation)
            .where(Conversation.contact_id == source_id, Conversation.client_id == client_id)
            .values(contact_id=target_id)
        )
        await self.session.execute(
            update(InternalNote)
            .where(InternalNote.contact_id == source_id, InternalNote.client_id == client_id)
            .values(contact_id=target_id)
        )

        # Las etiquetas no se pueden mover con un UPDATE masivo: uq_contact_tag
        # (contact_id, tag_id) rompería si el destino ya tiene la misma. Las
        # duplicadas se descartan del origen; el resto se reasigna una por una.
        etiquetas_destino = set(
            (
                await self.session.execute(
                    select(ContactTag.tag_id).where(
                        ContactTag.contact_id == target_id, ContactTag.client_id == client_id
                    )
                )
            )
            .scalars()
            .all()
        )
        etiquetas_origen = (
            (
                await self.session.execute(
                    select(ContactTag).where(
                        ContactTag.contact_id == source_id, ContactTag.client_id == client_id
                    )
                )
            )
            .scalars()
            .all()
        )
        for etiqueta in etiquetas_origen:
            if etiqueta.tag_id in etiquetas_destino:
                await self.session.delete(etiqueta)
            else:
                etiqueta.contact_id = target_id

        source.merged_into_id = target_id

        logger.info("Contacto %s fusionado en %s", source_id, target_id)
