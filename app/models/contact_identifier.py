"""Modelo ContactIdentifier — teléfonos, emails y handles por contacto.

`identifier_value` está cifrado en la base con pgcrypto (Sprint 8, migración
`008_encrypt_contact_identifiers`): la columna es `BYTEA` y el valor en claro
solo existe en memoria del proceso. `identifier_hash` es el índice ciego que
permite lo que el ciphertext impide — buscar por igualdad y garantizar
unicidad. Ver `app/core/encryption.py` para el porqué de las dos piezas.

El hash no lo escribe nadie a mano: lo deriva un listener de SQLAlchemy antes
de cada INSERT y de cada UPDATE. Dejarlo en manos de quien escribe garantizaba
que tarde o temprano alguien cambiase el valor sin recalcular el hash, y ese
error no falla: deja una fila que ya no se puede encontrar, con el UNIQUE
apuntando al identificador viejo.
"""

from typing import TYPE_CHECKING, Any
from uuid import UUID as _UUID

from sqlalchemy import ForeignKey, String, UniqueConstraint, event
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.encryption import EncryptedString, blind_index
from app.models.base import TenantBaseModel

if TYPE_CHECKING:
    from app.models.contact import Contact


class ContactIdentifier(TenantBaseModel):
    """Identificadores de contacto multi-canal.

    Attributes:
        contact_id: FK al contacto propietario.
        channel: Canal de comunicación (whatsapp, instagram, facebook, email, etc.).
        identifier_value: Valor del identificador (teléfono, email, ID de red
            social). Cifrado en la base, en claro en Python.
        identifier_hash: HMAC-SHA256 del valor normalizado y del tenant. Es la columna por la
            que se busca y sobre la que vive el UNIQUE.
    """

    __tablename__ = "contact_identifiers"
    __table_args__ = (
        # El UNIQUE va sobre el hash y no sobre el valor cifrado: dos filas con
        # el mismo telefono tienen ciphertexts distintos (IV aleatorio) y un
        # UNIQUE sobre la columna cifrada las aceptaria como diferentes.
        UniqueConstraint("client_id", "channel", "identifier_hash", name="uq_contact_identifier"),
    )

    contact_id: Mapped[_UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contacts.id"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(50), nullable=False)
    identifier_value: Mapped[str] = mapped_column(EncryptedString, nullable=False)
    identifier_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # Relationships
    contact: Mapped["Contact"] = relationship("Contact", back_populates="identifiers")


@event.listens_for(ContactIdentifier, "before_insert")
@event.listens_for(ContactIdentifier, "before_update")
def _sincronizar_indice_ciego(_mapper: Any, _connection: Any, target: ContactIdentifier) -> None:
    """Recalcula `identifier_hash` a partir del valor antes de escribir.

    Cubre el ORM, que es por donde pasa todo el código de la aplicación. Un
    `UPDATE` masivo hecho con `sqlalchemy.update()` **no** dispara este evento:
    si alguna vez hace falta uno sobre esta columna, tiene que escribir el hash
    en el mismo `.values()`.

    Args:
        _mapper: Mapper de SQLAlchemy (no se usa).
        _connection: Conexión en curso (no se usa).
        target: Fila que está a punto de escribirse.
    """
    target.identifier_hash = blind_index(target.identifier_value, target.client_id) or ""
