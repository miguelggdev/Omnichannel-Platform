"""Cifrado de columnas contra PostgreSQL real (Sprint 8, Dev A).

Comprueba el criterio de aceptación §5 del spec —"INSERT con dato → verificar
que en DB está cifrado → SELECT devuelve dato legible"— y las dos propiedades
que el índice ciego tiene que sostener: unicidad y búsqueda por igualdad.

Nada de esto se puede probar con dobles: el cifrado lo hace `pgcrypto` dentro
de PostgreSQL, y lo que interesa verificar es precisamente lo que queda escrito
en la tabla.
"""

import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.database import engine, tenant_session
from app.core.encryption import blind_index
from app.models.contact_identifier import ContactIdentifier

pytestmark = pytest.mark.db

#: Tenants sembrados por el test en curso, para limpiarlos al terminar.
_sembrados: list[uuid.UUID] = []


@pytest_asyncio.fixture(autouse=True)
async def _motor_limpio() -> AsyncGenerator[None, None]:
    """Suelta el pool del engine antes del test y borra lo sembrado al final.

    `engine` es un singleton de módulo y pytest-asyncio abre un event loop por
    test: una conexión del pool abierta en el loop de otro test revienta con
    "attached to a different loop". Mismo patrón que el resto de la suite de
    integración (`test_crm_api.py`, `test_graph_flow.py`).

    Yields:
        Control al test.
    """
    await engine.dispose()
    _sembrados.clear()
    yield
    for client_id in _sembrados:
        async with tenant_session(client_id) as session:
            await session.execute(
                text("DELETE FROM contact_identifiers WHERE client_id = :cid"),
                {"cid": str(client_id)},
            )
            await session.execute(
                text("DELETE FROM contacts WHERE client_id = :cid"),
                {"cid": str(client_id)},
            )
            await session.execute(
                text("DELETE FROM clients WHERE id = :cid"), {"cid": str(client_id)}
            )
    _sembrados.clear()


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
    _sembrados.append(client_id)
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
        assert guardado == blind_index(telefono, client_id)

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
                        ContactIdentifier.identifier_hash == blind_index(telefono, client_id),
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
                {"h": blind_index(telefono, cliente_b)},
            )
            del_otro_tenant = await session.scalar(
                text("SELECT count(*) FROM contact_identifiers WHERE identifier_hash = :h"),
                {"h": blind_index(telefono, cliente_a)},
            )
        assert visibles == 1
        assert del_otro_tenant == 0

    async def test_el_mismo_telefono_guarda_hashes_distintos_por_tenant(self) -> None:
        """Cada fila guarda el hash de su propio tenant, no uno compartido.

        Con el hash global, las dos filas tendrian el mismo `identifier_hash`, y
        un dump o un rol con BYPASSRLS mostraria que la misma persona escribe a
        dos clientes de la plataforma. Aqui cada tenant lee su propia fila (la
        RLS no deja mirar la del otro) y se comprueba que los valores guardados
        difieren y coinciden con `blind_index(telefono, <su tenant>)`.
        """
        cliente_a, contacto_a = await _sembrar_tenant(f"hash-a-{uuid.uuid4().hex[:8]}")
        cliente_b, contacto_b = await _sembrar_tenant(f"hash-b-{uuid.uuid4().hex[:8]}")
        telefono = f"+57300{uuid.uuid4().int % 10_000_000:07d}"
        guardados: dict[uuid.UUID, str] = {}

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
            async with tenant_session(cliente) as session:
                guardados[cliente] = await session.scalar(
                    text("SELECT identifier_hash FROM contact_identifiers WHERE contact_id = :id"),
                    {"id": str(contacto)},
                )

        assert guardados[cliente_a] != guardados[cliente_b]
        assert guardados[cliente_a] == blind_index(telefono, cliente_a)
        assert guardados[cliente_b] == blind_index(telefono, cliente_b)


def _cargar_migracion_009() -> Any:
    """Carga el modulo de la migracion 009 por ruta de archivo.

    `migrations/` no es un paquete y el nombre empieza por un digito, asi que
    `import` normal no sirve. Es sincrona a proposito: resuelve rutas.

    Returns:
        El modulo de la migracion, con `HASH_POR_TENANT` y `HASH_GLOBAL`.
    """
    import importlib.util
    from pathlib import Path

    ruta = Path(__file__).resolve().parents[2] / "migrations" / "versions"
    ruta = ruta / "009_blind_index_per_tenant.py"
    spec = importlib.util.spec_from_file_location("migracion_009", ruta)
    assert spec is not None
    assert spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


class TestParidadConLaMigracion:
    """La expresion SQL de la migracion 009 y `blind_index()` dan lo mismo.

    Si divergen, la aplicacion deja de encontrar las filas que migro el script:
    cada mensaje entrante crearia un contacto nuevo. Solo se puede comprobar
    contra Postgres real, porque `hmac()` es de pgcrypto.
    """

    @pytest.mark.parametrize(
        "valor",
        ["+573001234567", "  Juan@X.com  ", "[ELIMINADO-abc12345]", "PSID-6789000000000001"],
    )
    async def test_el_sql_y_python_calculan_el_mismo_hash(self, valor: str) -> None:
        """Se evalua la expresion de la migracion sobre un valor cifrado real."""
        migracion = _cargar_migracion_009()
        cliente, _ = await _sembrar_tenant(f"paridad-{uuid.uuid4().hex[:8]}")
        clave = get_settings().ENCRYPTION_KEY

        async with tenant_session(cliente) as session:
            # La expresion de la migracion lee `client_id` e `identifier_value`
            # de la fila: se evalua sobre una subconsulta que los aporta.
            expresion = migracion.HASH_POR_TENANT
            desde_sql = await session.scalar(
                text(
                    f"SELECT {expresion} FROM "  # noqa: S608
                    "(SELECT CAST(:cliente AS uuid) AS client_id, "
                    "pgp_sym_encrypt(:valor, :clave) AS identifier_value) t"
                ),
                {"cliente": str(cliente), "valor": valor, "clave": clave},
            )

        assert desde_sql == blind_index(valor, cliente)
