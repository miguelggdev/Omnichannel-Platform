"""Recorrido del CRM contra PostgreSQL real, con RLS activo.

Requiere base de datos: `pytest tests/ --run-db`.

Cubre lo que los tests unitarios no pueden, porque ahi la sesion es un doble:

  - que los endpoints escriban de verdad en el schema (contactos, etiquetas,
    notas, estados de conversacion) y que las FK sin CASCADE no revienten;
  - que un tenant no vea ni toque nada de otro por ninguna de estas rutas, con
    la politica de RLS haciendo el trabajo;
  - que el worker de auto-cierre mueva los estados que dice mover, comparando
    contra el `now()` del servidor.

Nada esta sustituido salvo el listado de tenants del worker: `clients` no es
legible sin contexto de tenant (su politica filtra por `id`), asi que el barrido
recibe el tenant sembrado y el resto del recorrido es real.
"""

import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.core.database import engine, tenant_session
from app.core.security import create_access_token
from app.main import create_app
from app.tasks import auto_close as auto_close_module
from app.tasks.auto_close import _auto_close

pytestmark = pytest.mark.db

CONTACTS = "/api/v1/contacts"
CONVERSATIONS = "/api/v1/conversations"
TAGS = "/api/v1/tags"

# Una sentencia por columna, con el intervalo como bind param: asi el SQL de los
# tests no se arma con f-strings.
ENVEJECER_SQL = {
    "updated_at": (
        "UPDATE conversations SET status = :status, "
        "updated_at = now() - CAST(:intervalo AS interval) WHERE id = :id"
    ),
    "resolved_at": (
        "UPDATE conversations SET status = :status, "
        "resolved_at = now() - CAST(:intervalo AS interval) WHERE id = :id"
    ),
}


class Escenario:
    """Ids de un tenant sembrado.

    Attributes:
        client_id: Tenant.
        user_id: Usuario que firma las notas y recibe las asignaciones.
        contact_id: Contacto con una conversacion.
        conversation_id: Conversacion en `bot_active`.
    """

    def __init__(
        self,
        client_id: uuid.UUID,
        user_id: uuid.UUID,
        contact_id: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> None:
        """Guarda los ids del escenario."""
        self.client_id = client_id
        self.user_id = user_id
        self.contact_id = contact_id
        self.conversation_id = conversation_id


async def _sembrar(slug: str) -> Escenario:
    """Crea un tenant con usuario, contacto y una conversacion activa.

    Args:
        slug: Slug del tenant (unico).

    Returns:
        Escenario con los ids sembrados.
    """
    client_id = uuid.uuid4()
    user_id = uuid.uuid4()
    contact_id = uuid.uuid4()
    conversation_id = uuid.uuid4()

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
                "INSERT INTO users (id, client_id, email, password_hash, first_name, "
                "last_name, role, is_active) VALUES (:id, :cid, :email, 'x', 'Agente', "
                "'De Prueba', 'agent', true)"
            ),
            {
                "id": str(user_id),
                "cid": str(client_id),
                "email": f"agente-{uuid.uuid4().hex[:12]}@example.com",
            },
        )
        await session.execute(
            text(
                "INSERT INTO contacts (id, client_id, first_name, last_name, display_name) "
                "VALUES (:id, :cid, 'Ada', 'Lovelace', 'Ada L.')"
            ),
            {"id": str(contact_id), "cid": str(client_id)},
        )
        await session.execute(
            text(
                "INSERT INTO contact_identifiers (client_id, contact_id, channel, "
                "identifier_value) VALUES (:cid, :contact, 'whatsapp', :valor)"
            ),
            {
                "cid": str(client_id),
                "contact": str(contact_id),
                "valor": f"57300{uuid.uuid4().int % 10_000_000:07d}",
            },
        )
        await session.execute(
            text(
                "INSERT INTO conversations (id, client_id, contact_id, channel, status, "
                "last_message_at) VALUES (:id, :cid, :contact, 'whatsapp', 'bot_active', now())"
            ),
            {"id": str(conversation_id), "cid": str(client_id), "contact": str(contact_id)},
        )

    return Escenario(client_id, user_id, contact_id, conversation_id)


async def _limpiar(client_id: uuid.UUID) -> None:
    """Borra el tenant y todo lo que cuelga de el, en orden de FKs.

    Args:
        client_id: Tenant a borrar.
    """
    async with tenant_session(client_id) as session:
        for tabla in (
            "internal_notes",
            "contact_tags",
            "tags",
            "messages",
            "conversations",
            "contact_identifiers",
        ):
            await session.execute(
                text(f"DELETE FROM {tabla} WHERE client_id = :cid"),  # noqa: S608
                {"cid": str(client_id)},
            )
        # Los contactos fusionados apuntan a otro contacto: primero se suelta la
        # referencia, si no el DELETE choca con la FK de merged_into_id.
        await session.execute(
            text("UPDATE contacts SET merged_into_id = NULL WHERE client_id = :cid"),
            {"cid": str(client_id)},
        )
        for tabla in ("contacts", "users"):
            await session.execute(
                text(f"DELETE FROM {tabla} WHERE client_id = :cid"),  # noqa: S608
                {"cid": str(client_id)},
            )
        await session.execute(text("DELETE FROM clients WHERE id = :cid"), {"cid": str(client_id)})


def _cliente(escenario: Escenario, role: str = "admin") -> AsyncClient:
    """Cliente HTTP autenticado como un usuario del tenant sembrado.

    Args:
        escenario: Tenant al que pertenece el usuario.
        role: Rol con el que se firma el token.

    Returns:
        Cliente listo para pegarle a la app.
    """
    token = create_access_token(
        {
            "user_id": str(escenario.user_id),
            "client_id": str(escenario.client_id),
            "email": "agente@example.com",
            "role": role,
        }
    )
    client = AsyncClient(
        transport=ASGITransport(app=create_app()),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )
    return client


@pytest_asyncio.fixture
async def escenario() -> AsyncGenerator[Escenario, None]:
    """Siembra un tenant y lo limpia al terminar.

    `engine.dispose()` antes de empezar: el engine es un singleton de modulo y
    pytest-asyncio abre un event loop por test, asi que una conexion del pool
    abierta en el loop de otro test revienta con "attached to a different loop".
    """
    await engine.dispose()
    datos = await _sembrar(f"crm-{uuid.uuid4().hex[:8]}")
    yield datos
    await _limpiar(datos.client_id)


@pytest_asyncio.fixture
async def dos_tenants() -> AsyncGenerator[tuple[Escenario, Escenario], None]:
    """Siembra dos tenants para los tests de aislamiento."""
    await engine.dispose()
    a = await _sembrar(f"crm-a-{uuid.uuid4().hex[:8]}")
    b = await _sembrar(f"crm-b-{uuid.uuid4().hex[:8]}")
    yield a, b
    await _limpiar(a.client_id)
    await _limpiar(b.client_id)


# ─── Recorrido completo ──────────────────────────────────────────────────────


class TestRecorridoDelCrm:
    """De crear un contacto a leerlo con todo lo que cuelga de el."""

    async def test_alta_etiqueta_nota_y_detalle(self, escenario: Escenario) -> None:
        """Un contacto nuevo se etiqueta, se anota y el detalle lo devuelve todo."""
        async with _cliente(escenario) as client:
            alta = await client.post(CONTACTS, json={"first_name": "Grace", "last_name": "Hopper"})
            assert alta.status_code == 201
            contacto_id = alta.json()["id"]

            tag = await client.post(TAGS, json={"name": "vip", "color": "#FF5733"})
            assert tag.status_code == 201
            tag_id = tag.json()["id"]

            asignada = await client.post(f"{CONTACTS}/{contacto_id}/tags/{tag_id}")
            assert asignada.status_code == 201

            nota = await client.post(
                f"{CONTACTS}/{contacto_id}/notes", json={"content": "llamar el lunes"}
            )
            assert nota.status_code == 201

            detalle = await client.get(f"{CONTACTS}/{contacto_id}")
            assert detalle.status_code == 200
            cuerpo = detalle.json()
            assert cuerpo["first_name"] == "Grace"
            assert [t["name"] for t in cuerpo["tags"]] == ["vip"]
            assert [n["content"] for n in cuerpo["notes"]] == ["llamar el lunes"]

    async def test_la_nota_queda_firmada_por_el_usuario_del_token(
        self, escenario: Escenario
    ) -> None:
        """`internal_notes.author_id` es NOT NULL y FK a users: sale del JWT."""
        async with _cliente(escenario) as client:
            respuesta = await client.post(
                f"{CONTACTS}/{escenario.contact_id}/notes", json={"content": "anotacion"}
            )

        assert respuesta.status_code == 201
        assert respuesta.json()["author_id"] == str(escenario.user_id)

    async def test_el_listado_pagina_de_verdad(self, escenario: Escenario) -> None:
        """Con 3 contactos y page_size=2, la primera pagina trae 2 y el total dice 4."""
        async with _cliente(escenario) as client:
            for nombre in ("Uno", "Dos", "Tres"):
                await client.post(CONTACTS, json={"first_name": nombre})

            pagina = await client.get(CONTACTS, params={"page": 1, "page_size": 2})
            segunda = await client.get(CONTACTS, params={"page": 2, "page_size": 2})

        # 3 nuevos + el sembrado.
        assert pagina.json()["total"] == 4
        assert len(pagina.json()["items"]) == 2
        assert len(segunda.json()["items"]) == 2

    async def test_la_busqueda_por_nombre_filtra(self, escenario: Escenario) -> None:
        """El ILIKE encuentra sin distinguir mayusculas."""
        async with _cliente(escenario) as client:
            await client.post(CONTACTS, json={"first_name": "Grace", "last_name": "Hopper"})

            encontrado = await client.get(CONTACTS, params={"search": "hopp"})
            vacio = await client.get(CONTACTS, params={"search": "zzzz"})

        assert encontrado.json()["total"] == 1
        assert encontrado.json()["items"][0]["last_name"] == "Hopper"
        assert vacio.json()["total"] == 0

    async def test_el_filtro_por_tag_devuelve_solo_los_etiquetados(
        self, escenario: Escenario
    ) -> None:
        """El join contra contact_tags acota el listado y tambien el total."""
        async with _cliente(escenario) as client:
            tag_id = (await client.post(TAGS, json={"name": "vip"})).json()["id"]
            otro = (await client.post(CONTACTS, json={"first_name": "Sin etiqueta"})).json()
            await client.post(f"{CONTACTS}/{escenario.contact_id}/tags/{tag_id}")

            filtrado = await client.get(CONTACTS, params={"tag_id": tag_id})

        assert filtrado.json()["total"] == 1
        assert filtrado.json()["items"][0]["id"] == str(escenario.contact_id)
        assert filtrado.json()["items"][0]["id"] != otro["id"]

    async def test_el_historial_trae_la_conversacion_del_contacto(
        self, escenario: Escenario
    ) -> None:
        """El contacto sembrado tiene una conversacion y el endpoint la devuelve."""
        async with _cliente(escenario) as client:
            respuesta = await client.get(f"{CONTACTS}/{escenario.contact_id}/conversations")

        assert respuesta.status_code == 200
        assert respuesta.json()["total"] == 1
        assert respuesta.json()["items"][0]["id"] == str(escenario.conversation_id)


# ─── Etiquetas y FKs sin CASCADE ─────────────────────────────────────────────


class TestEtiquetas:
    """Integridad referencial real de contact_tags."""

    async def test_borrar_una_etiqueta_en_uso_no_rompe_la_fk(self, escenario: Escenario) -> None:
        """La FK de Sprint 1 no declara ON DELETE CASCADE.

        Sin el borrado explicito de `contact_tags`, este DELETE fallaria con
        violacion de integridad en cuanto la etiqueta estuviera asignada.
        """
        async with _cliente(escenario) as client:
            tag_id = (await client.post(TAGS, json={"name": "vip"})).json()["id"]
            await client.post(f"{CONTACTS}/{escenario.contact_id}/tags/{tag_id}")

            borrado = await client.delete(f"{TAGS}/{tag_id}")
            detalle = await client.get(f"{CONTACTS}/{escenario.contact_id}")

        assert borrado.status_code == 200
        assert borrado.json()["assignments_removed"] == 1
        assert detalle.json()["tags"] == []

    async def test_nombre_repetido_es_409_no_un_error_de_integridad(
        self, escenario: Escenario
    ) -> None:
        """`uq_tag_name_per_client` existe; el endpoint lo traduce antes de chocar."""
        async with _cliente(escenario) as client:
            primera = await client.post(TAGS, json={"name": "repetida"})
            segunda = await client.post(TAGS, json={"name": "repetida"})

        assert primera.status_code == 201
        assert segunda.status_code == 409


# ─── Ciclo de vida sobre datos reales ────────────────────────────────────────


class TestCicloDeVida:
    """Asignacion y cambios de estado persistidos."""

    async def test_asignar_persiste_el_estado_y_el_agente(self, escenario: Escenario) -> None:
        """La conversacion queda en human_active y a nombre del agente."""
        async with _cliente(escenario) as client:
            respuesta = await client.put(
                f"{CONVERSATIONS}/{escenario.conversation_id}/assign",
                json={"user_id": str(escenario.user_id)},
            )

        assert respuesta.status_code == 200

        async with tenant_session(escenario.client_id) as session:
            fila = (
                await session.execute(
                    text("SELECT status, assigned_user_id FROM conversations WHERE id = :id"),
                    {"id": str(escenario.conversation_id)},
                )
            ).one()

        assert fila.status == "human_active"
        assert fila.assigned_user_id == escenario.user_id

    async def test_resolver_sella_resolved_at_en_la_base(self, escenario: Escenario) -> None:
        """El sello de resolucion lo lee despues el auto-archivado."""
        async with _cliente(escenario) as client:
            respuesta = await client.put(
                f"{CONVERSATIONS}/{escenario.conversation_id}/status",
                json={"status": "resolved"},
            )

        assert respuesta.status_code == 200

        async with tenant_session(escenario.client_id) as session:
            resolved_at = await session.scalar(
                text("SELECT resolved_at FROM conversations WHERE id = :id"),
                {"id": str(escenario.conversation_id)},
            )

        assert resolved_at is not None

    async def test_el_enum_de_la_base_acepta_los_siete_estados(self, escenario: Escenario) -> None:
        """Recorre new -> bot_active -> waiting_human -> human_active -> ... -> archived.

        Si la maquina de estados nombrara un estado que el enum
        `conversation_status` no tiene, el UPDATE reventaria aqui.
        """
        recorrido = [
            ("bot_active", "waiting_human"),
            ("waiting_human", "human_active"),
            ("human_active", "waiting_client"),
            ("waiting_client", "resolved"),
            ("resolved", "archived"),
        ]

        async with _cliente(escenario) as client:
            for origen, destino in recorrido:
                respuesta = await client.put(
                    f"{CONVERSATIONS}/{escenario.conversation_id}/status",
                    json={"status": destino},
                )
                assert respuesta.status_code == 200, f"{origen} -> {destino}"
                assert respuesta.json()["previous_status"] == origen

        async with tenant_session(escenario.client_id) as session:
            status = await session.scalar(
                text("SELECT status FROM conversations WHERE id = :id"),
                {"id": str(escenario.conversation_id)},
            )

        assert status == "archived"

    async def test_una_transicion_invalida_no_toca_la_fila(self, escenario: Escenario) -> None:
        """El 400 deja la conversacion como estaba."""
        async with _cliente(escenario) as client:
            respuesta = await client.put(
                f"{CONVERSATIONS}/{escenario.conversation_id}/status",
                json={"status": "archived"},
            )

        assert respuesta.status_code == 400

        async with tenant_session(escenario.client_id) as session:
            status = await session.scalar(
                text("SELECT status FROM conversations WHERE id = :id"),
                {"id": str(escenario.conversation_id)},
            )

        assert status == "bot_active"


# ─── Aislamiento entre tenants ───────────────────────────────────────────────


class TestAislamiento:
    """Ningun endpoint del CRM deja cruzar datos entre tenants."""

    async def test_el_listado_solo_ve_lo_propio(
        self, dos_tenants: tuple[Escenario, Escenario]
    ) -> None:
        """Cada tenant ve su contacto y solo el suyo."""
        a, b = dos_tenants

        async with _cliente(a) as cliente_a, _cliente(b) as cliente_b:
            lista_a = await cliente_a.get(CONTACTS)
            lista_b = await cliente_b.get(CONTACTS)

        ids_a = {c["id"] for c in lista_a.json()["items"]}
        ids_b = {c["id"] for c in lista_b.json()["items"]}
        assert ids_a == {str(a.contact_id)}
        assert ids_b == {str(b.contact_id)}
        assert not ids_a & ids_b

    async def test_el_detalle_de_otro_tenant_es_404(
        self, dos_tenants: tuple[Escenario, Escenario]
    ) -> None:
        """Pedir el contacto del vecino responde 404, no 403 ni el contacto."""
        a, b = dos_tenants

        async with _cliente(a) as cliente_a:
            respuesta = await cliente_a.get(f"{CONTACTS}/{b.contact_id}")

        assert respuesta.status_code == 404

    async def test_no_se_puede_editar_el_contacto_de_otro_tenant(
        self, dos_tenants: tuple[Escenario, Escenario]
    ) -> None:
        """El PUT contra un contacto ajeno no lo encuentra ni lo modifica."""
        a, b = dos_tenants

        async with _cliente(a) as cliente_a:
            respuesta = await cliente_a.put(
                f"{CONTACTS}/{b.contact_id}", json={"first_name": "Secuestrado"}
            )

        assert respuesta.status_code == 404

        async with tenant_session(b.client_id) as session:
            nombre = await session.scalar(
                text("SELECT first_name FROM contacts WHERE id = :id"),
                {"id": str(b.contact_id)},
            )

        assert nombre == "Ada"

    async def test_no_se_puede_mover_la_conversacion_de_otro_tenant(
        self, dos_tenants: tuple[Escenario, Escenario]
    ) -> None:
        """Cambiar el estado de una conversacion ajena responde 404."""
        a, b = dos_tenants

        async with _cliente(a) as cliente_a:
            respuesta = await cliente_a.put(
                f"{CONVERSATIONS}/{b.conversation_id}/status", json={"status": "resolved"}
            )

        assert respuesta.status_code == 404

        async with tenant_session(b.client_id) as session:
            status = await session.scalar(
                text("SELECT status FROM conversations WHERE id = :id"),
                {"id": str(b.conversation_id)},
            )

        assert status == "bot_active"

    async def test_no_se_puede_asignar_a_un_agente_de_otro_tenant(
        self, dos_tenants: tuple[Escenario, Escenario]
    ) -> None:
        """El agente se busca con el client_id en el WHERE: 404, no asignacion cruzada."""
        a, b = dos_tenants

        async with _cliente(a) as cliente_a:
            respuesta = await cliente_a.put(
                f"{CONVERSATIONS}/{a.conversation_id}/assign",
                json={"user_id": str(b.user_id)},
            )

        assert respuesta.status_code == 404

    async def test_no_se_puede_etiquetar_con_la_etiqueta_de_otro_tenant(
        self, dos_tenants: tuple[Escenario, Escenario]
    ) -> None:
        """Una etiqueta del vecino no existe para este tenant."""
        a, b = dos_tenants

        async with _cliente(b) as cliente_b:
            tag_b = (await cliente_b.post(TAGS, json={"name": "de-b"})).json()["id"]

        async with _cliente(a) as cliente_a:
            respuesta = await cliente_a.post(f"{CONTACTS}/{a.contact_id}/tags/{tag_b}")

        assert respuesta.status_code == 404

    async def test_las_notas_de_otro_tenant_no_se_leen(
        self, dos_tenants: tuple[Escenario, Escenario]
    ) -> None:
        """Las notas internas cuelgan del contacto: si el contacto no se ve, ellas tampoco."""
        a, b = dos_tenants

        async with _cliente(b) as cliente_b:
            await cliente_b.post(
                f"{CONTACTS}/{b.contact_id}/notes", json={"content": "secreto de B"}
            )

        async with _cliente(a) as cliente_a:
            respuesta = await cliente_a.get(f"{CONTACTS}/{b.contact_id}/notes")

        assert respuesta.status_code == 404


# ─── Auto-cierre ─────────────────────────────────────────────────────────────


class TestAutoCierre:
    """El worker de Beat, contra datos reales."""

    async def _envejecer(
        self, escenario: Escenario, status: str, campo: str, intervalo: str
    ) -> None:
        """Deja la conversacion sembrada en un estado con la fecha corrida hacia atras.

        Args:
            escenario: Tenant y conversacion sembrados.
            status: Estado en el que dejarla.
            campo: Columna de fecha a envejecer (`updated_at` o `resolved_at`).
            intervalo: Intervalo de PostgreSQL, ej. "25 hours".
        """
        async with tenant_session(escenario.client_id) as session:
            await session.execute(
                text(ENVEJECER_SQL[campo]),
                {
                    "status": status,
                    "intervalo": intervalo,
                    "id": str(escenario.conversation_id),
                },
            )

    async def _status(self, escenario: Escenario) -> str:
        """Lee el estado actual de la conversacion sembrada."""
        async with tenant_session(escenario.client_id) as session:
            return str(
                await session.scalar(
                    text("SELECT status FROM conversations WHERE id = :id"),
                    {"id": str(escenario.conversation_id)},
                )
            )

    async def _barrer(
        self, monkeypatch: pytest.MonkeyPatch, escenario: Escenario
    ) -> dict[str, Any]:
        """Corre el barrido sobre el tenant sembrado.

        `clients` no es legible sin contexto de tenant (su politica de RLS filtra
        por `id`), asi que la lista de tenants se inyecta y todo lo demas —los dos
        UPDATE, la RLS, el `now()` del servidor— es real.
        """

        async def _solo_este() -> list[uuid.UUID]:
            return [escenario.client_id]

        monkeypatch.setattr(auto_close_module, "_load_active_client_ids", _solo_este)
        return await _auto_close()

    async def test_waiting_client_viejo_pasa_a_resolved(
        self, escenario: Escenario, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mas de 24 h esperando al contacto: se da por resuelta."""
        await self._envejecer(escenario, "waiting_client", "updated_at", "25 hours")

        resultado = await self._barrer(monkeypatch, escenario)

        assert resultado["resolved"] == 1
        assert await self._status(escenario) == "resolved"

    async def test_waiting_client_reciente_no_se_toca(
        self, escenario: Escenario, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Con 23 h todavia no se cierra: el umbral es de 24."""
        await self._envejecer(escenario, "waiting_client", "updated_at", "23 hours")

        resultado = await self._barrer(monkeypatch, escenario)

        assert resultado["resolved"] == 0
        assert await self._status(escenario) == "waiting_client"

    async def test_resolved_viejo_pasa_a_archived(
        self, escenario: Escenario, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mas de 7 dias resuelta: se archiva."""
        await self._envejecer(escenario, "resolved", "resolved_at", "8 days")

        resultado = await self._barrer(monkeypatch, escenario)

        assert resultado["archived"] == 1
        assert await self._status(escenario) == "archived"

    async def test_resolved_reciente_no_se_archiva(
        self, escenario: Escenario, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Con 6 dias sigue visible."""
        await self._envejecer(escenario, "resolved", "resolved_at", "6 days")

        resultado = await self._barrer(monkeypatch, escenario)

        assert resultado["archived"] == 0
        assert await self._status(escenario) == "resolved"

    async def test_resolved_sin_sello_no_se_archiva(
        self, escenario: Escenario, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Una fila en `resolved` sin `resolved_at` es un dato inconsistente.

        Archivarla a ciegas esconderia el problema; el WHERE exige el sello.
        """
        async with tenant_session(escenario.client_id) as session:
            await session.execute(
                text(
                    "UPDATE conversations SET status = 'resolved', resolved_at = NULL "
                    "WHERE id = :id"
                ),
                {"id": str(escenario.conversation_id)},
            )

        resultado = await self._barrer(monkeypatch, escenario)

        assert resultado["archived"] == 0
        assert await self._status(escenario) == "resolved"

    async def test_una_pasada_no_salta_dos_estados(
        self, escenario: Escenario, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Una conversacion vieja en waiting_client termina en resolved, no en archived.

        Los dos UPDATE corren en la misma transaccion y en ese orden, asi que el
        segundo ve ya la fila como `resolved`; lo que la salva es que su
        `resolved_at` es de ahora mismo, no de hace 7 dias.
        """
        await self._envejecer(escenario, "waiting_client", "updated_at", "40 days")

        await self._barrer(monkeypatch, escenario)

        assert await self._status(escenario) == "resolved"

    async def test_el_barrido_respeta_el_aislamiento_entre_tenants(
        self, dos_tenants: tuple[Escenario, Escenario], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Barrer el tenant A no toca las conversaciones de B, aunque esten igual de viejas."""
        a, b = dos_tenants
        await self._envejecer(a, "waiting_client", "updated_at", "25 hours")
        await self._envejecer(b, "waiting_client", "updated_at", "25 hours")

        resultado = await self._barrer(monkeypatch, a)

        assert resultado["resolved"] == 1
        assert await self._status(a) == "resolved"
        assert await self._status(b) == "waiting_client"
