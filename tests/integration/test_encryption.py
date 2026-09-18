"""Cifrado de columnas contra PostgreSQL real (Sprint 8, Dev A).

Comprueba el criterio de aceptación §5 del spec —"INSERT con dato → verificar
que en DB está cifrado → SELECT devuelve dato legible"— y las dos propiedades
que el índice ciego tiene que sostener: unicidad y búsqueda por igualdad.

Nada de esto se puede probar con dobles: el cifrado lo hace `pgcrypto` dentro
de PostgreSQL, y lo que interesa verificar es precisamente lo que queda escrito
en la tabla.
"""

import uuid

import pytest
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.database import tenant_session
from app.core.encryption import blind_index
from app.models.contact_identifier import ContactIdentifier

pytestmark = pytest.mark.db


async def _sembrar_tenant(slug: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Crea un tenant con un contacto vacío.

    Args:
        slug: Slug único del tenant.

    Returns:
        Par (client_id, contact_id).
    """
    client_id = uuid.uuid4()
    contact_id = uuid.uuid4()
    async with tenant_session(client_id) as session:
        await session.execute(
            text(
                "INSERT INTO clients (id, name, slug, plan, is_active) "
                "VALUES (:id, :name, :slug, 'free', true)"
            ),
            {"id": str(client_id), "name": f"Tenant {slug}", "slug": slug},
        )
        await session.execute(
            text(
                "INSERT INTO contacts (id, client_id, display_name) "
                "VALUES (:id, :cid, 'Contacto cifrado')"
            ),
            {"id": str(contact_id), "cid": str(client_id)},
        )
    return client_id, contact_id


class TestColumnaCifrada:
    """Lo que queda en disco y lo que devuelve la aplicación."""

    async def test_en_la_tabla_esta_cifrado_y_al_leer_vuelve_en_claro(self) -> None:
        """Criterio §5 completo, en una sola comprobación de ida y vuelta."""
        client_id, contact_id = await _sembrar_tenant(f"cifrado-{uuid.uuid4().hex[:8]}")
        telefono = f"+57300{uuid.uuid4().int % 10_000_000:07d}"

        async with tenant_session(client_id) as session:
            session.add(
                ContactIdentifier(
                    client_id=client_id,
                    contact_id=contact_id,
                    channel="whatsapp",
                    identifier_value=telefono,
                )
            )

        async with tenant_session(client_id) as session:
            # 1. En la tabla: bytes, y el telefono no aparece por ningun lado.
            crudo = await session.scalar(
                text("SELECT identifier_value FROM contact_identifiers WHERE contact_id = :id"),
                {"id": str(contact_id)},
            )
            assert isinstance(crudo, bytes | memoryview)
            assert telefono.encode() not in bytes(crudo)

            # 2. Descifrando con la clave: el valor original.
            descifrado = await session.scalar(
                text(
                    "SELECT pgp_sym_decrypt(identifier_value, :clave) "
                    "FROM contact_identifiers WHERE contact_id = :id"
                ),
                {"id": str(contact_id), "clave": get_settings().ENCRYPTION_KEY},
            )
            assert descifrado == telefono

            # 3. Por el ORM, sin que el codigo de negocio sepa nada del cifrado.
            por_orm = await session.scalar(
                select(ContactIdentifier.identifier_value).where(
                    ContactIdentifier.contact_id == contact_id
                )
            )
            assert por_orm == telefono

    async def test_dos_cifrados_del_mismo_valor_no_coinciden(self) -> None:
        """`pgp_sym_encrypt` no es determinista: IV aleatorio en cada llamada.

        Es la premisa que el spec §8.4 da por falsa y de la que dependen tanto
        el índice ciego como el cambio de UNIQUE de esta migración.
        """
        client_id, _ = await _sembrar_tenant(f"iv-{uuid.uuid4().hex[:8]}")
        async with tenant_session(client_id) as session:
            primero, segundo = (
                await session.execute(
                    text(
                        "SELECT pgp_sym_encrypt('+573001112222', :clave), "
                        "pgp_sym_encrypt('+573001112222', :clave)"
                    ),
                    {"clave": get_settings().ENCRYPTION_KEY},
                )
            ).one()
        assert bytes(primero) != bytes(segundo)


class TestIndiceCiego:
    """Unicidad y búsqueda, las dos cosas que el ciphertext no permite."""

    async def test_el_hash_se_escribe_solo(self) -> None:
        """El listener del modelo deja el índice al día sin intervención."""
        client_id, contact_id = await _sembrar_tenant(f"hash-{uuid.uuid4().hex[:8]}")
        telefono = f"+57300{uuid.uuid4().int % 10_000_000:07d}"

        async with tenant_session(client_id) as session:
            session.add(
                ContactIdentifier(
                    client_id=client_id,
                    contact_id=contact_id,
                    channel="whatsapp",
                    identifier_value=telefono,
                )
            )

        async with tenant_session(client_id) as session:
            guardado = await session.scalar(
                text("SELECT identifier_hash FROM contact_identifiers WHERE contact_id = :id"),
                {"id": str(contact_id)},
            )
        assert guardado == blind_index(telefono)

    async def test_se_encuentra_buscando_por_el_hash(self) -> None:
        """La busqueda del webhook (`_resolve_contact`) sigue funcionando.

        Si esta consulta fallara, cada mensaje entrante crearía un contacto
        nuevo y el CRM se llenaría de duplicados en horas.
        """
        client_id, contact_id = await _sembrar_tenant(f"buscar-{uuid.uuid4().hex[:8]}")
        telefono = f"+57300{uuid.uuid4().int % 10_000_000:07d}"

        async with tenant_session(client_id) as session:
            session.add(
                ContactIdentifier(
                    client_id=client_id,
                    contact_id=contact_id,
                    channel="whatsapp",
                    identifier_value=telefono,
                )
            )

        async with tenant_session(client_id) as session:
            encontrado = (
                await session.execute(
                    select(ContactIdentifier).where(
                        ContactIdentifier.client_id == client_id,
                        ContactIdentifier.channel == "whatsapp",
                        ContactIdentifier.identifier_hash == blind_index(telefono),
                    )
                )
            ).scalar_one_or_none()

        assert encontrado is not None
        assert encontrado.contact_id == contact_id

    async def test_el_unique_sigue_rechazando_duplicados(self) -> None:
        """Dos veces el mismo identificador en el mismo canal: violacion.

        Con el UNIQUE sobre la columna cifrada esto pasaria sin error, porque
        los dos ciphertexts son distintos, y el aislamiento del dedup de
        contactos se perderia en silencio.
        """
        from sqlalchemy.exc import IntegrityError

        client_id, contact_id = await _sembrar_tenant(f"unico-{uuid.uuid4().hex[:8]}")
        telefono = f"+57300{uuid.uuid4().int % 10_000_000:07d}"

        async with tenant_session(client_id) as session:
            session.add(
                ContactIdentifier(
                    client_id=client_id,
                    contact_id=contact_id,
                    channel="whatsapp",
                    identifier_value=telefono,
                )
            )

        with pytest.raises(IntegrityError):
            async with tenant_session(client_id) as session:
                session.add(
                    ContactIdentifier(
                        client_id=client_id,
                        contact_id=contact_id,
                        channel="whatsapp",
                        identifier_value=telefono,
                    )
                )

    async def test_otro_tenant_puede_tener_el_mismo_telefono(self) -> None:
        """El UNIQUE lleva `client_id`: el mismo numero en dos tenants es legal."""
        cliente_a, contacto_a = await _sembrar_tenant(f"multi-a-{uuid.uuid4().hex[:8]}")
        cliente_b, contacto_b = await _sembrar_tenant(f"multi-b-{uuid.uuid4().hex[:8]}")
        telefono = f"+57300{uuid.uuid4().int % 10_000_000:07d}"

        for cliente, contacto in ((cliente_a, contacto_a), (cliente_b, contacto_b)):
            async with tenant_session(cliente) as session:
                session.add(
                    ContactIdentifier(
                        client_id=cliente,
                        contact_id=contacto,
                        channel="whatsapp",
                        identifier_value=telefono,
                    )
                )

        async with tenant_session(cliente_b) as session:
            visibles = await session.scalar(
                text("SELECT count(*) FROM contact_identifiers WHERE identifier_hash = :h"),
                {"h": blind_index(telefono)},
            )
        # Uno solo: la RLS tapa la fila del otro tenant aunque el hash coincida.
        assert visibles == 1
