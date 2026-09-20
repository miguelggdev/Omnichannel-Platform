"""ContactUnifier — fusión manual de contactos duplicados.

Contrato (`app/api/v1/contacts.py::merge_contacts()`, de Dev B):
`ContactUnifier(session).merge(source_id=..., target_id=..., client_id=...)`,
sobre una `session` ya abierta con `tenant_session()`. El endpoint valida antes
de llamar — `source_id != target_id`, ambos contactos existen, ninguno está ya
fusionado (`merged_into_id is not None`) — y `merge()` no repite el chequeo de
fusión previa; solo mueve las filas y marca el origen.

El `client_id` llega **explícito** del llamador autenticado, no se deduce de la
fila: `source_id`/`target_id` salen de la URL de un endpoint HTTP, y tomar el
tenant de la propia fila que se busca sin filtro sería una defensa circular
(cualquier fila que RLS dejara pasar "confirmaría" su propio tenant). Con él,
`merge()` comprueba que **origen y destino** pertenecen a ese tenant antes de
tocar nada: sin la comprobación del destino, `merged_into_id` podría apuntar a
un contacto de otro tenant.

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

    async def merge(self, source_id: UUID, target_id: UUID, client_id: UUID) -> None:
        """Mueve todo lo del contacto origen al destino y marca el origen fusionado.

        Mueve, en este orden: identificadores de canal, conversaciones, notas
        internas y etiquetas (sin duplicar las que el destino ya tenía). Al
        final, marca `source.merged_into_id = target_id`.

        Args:
            source_id: Contacto duplicado que se absorbe.
            target_id: Contacto que sobrevive a la fusión.
            client_id: Tenant del usuario autenticado; ambos contactos tienen
                que pertenecerle.

        Raises:
            ContactNotFoundError: Si `source_id` o `target_id` no resuelven a un
                contacto de `client_id` (el endpoint ya lo valida antes de
                llamar; solo pasaría ante una fila borrada entre la validación y
                esta llamada, o ante un llamador que se salte esa validación).
        """
        logger.info("Fusionando contacto %s en %s", source_id, target_id)

        source = await self._contacto_del_tenant(source_id, client_id, "origen")
        await self._contacto_del_tenant(target_id, client_id, "destino")

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

    async def _contacto_del_tenant(self, contact_id: UUID, client_id: UUID, rol: str) -> Contact:
        """Carga un contacto exigiendo que sea del tenant indicado.

        Filtro explícito en el WHERE además de RLS: `session.get()` no deja ver
        ningún filtro y, si la política fallara en esa tabla, devolvería el
        contacto de otro tenant sin que nada avisara.

        Args:
            contact_id: Contacto buscado.
            client_id: Tenant al que tiene que pertenecer.
            rol: `"origen"` o `"destino"`, para el mensaje de error.

        Returns:
            El contacto.

        Raises:
            ContactNotFoundError: Si no existe en ese tenant.
        """
        contacto = (
            await self.session.execute(
                select(Contact).where(Contact.id == contact_id, Contact.client_id == client_id)
            )
        ).scalar_one_or_none()
        if contacto is None:
            raise ContactNotFoundError(f"Contacto {rol} {contact_id} no encontrado")
        return contacto
