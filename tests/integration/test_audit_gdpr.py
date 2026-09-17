"""Auditoria y RGPD contra PostgreSQL real, con RLS activo.

Requiere base de datos: `pytest tests/ --run-db`.

El rastro de auditoria es lo unico del sistema que NO se puede probar sin base:
lo escribe un trigger de PL/pgSQL, no Python. Un test unitario solo podria
comprobar que la migracion contiene cierto texto, que no es lo mismo que
comprobar que el trigger dispara.

Tambien viven aqui los tests de BUG-025 (el login bajo RLS) y de la funcion
`auth_lookup_user()` que lo cierra, porque dependen de la misma pieza — la RLS
sobre `users` — y son los que deciden si esa tabla puede llevar trigger de
auditoria. Ver `TestLoginBajoRls` y `TestFuncionDeBusqueda`.
"""

import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.core.database import engine, tenant_session
from app.core.security import create_access_token, hash_password
from app.main import create_app

pytestmark = pytest.mark.db

ADMIN = "/api/v1/admin/contacts"


class Escenario:
    """Ids de un tenant sembrado con un contacto que ya converso.

    Attributes:
        client_id: Tenant.
        user_id: Usuario admin que ejecuta las operaciones.
        email: Email de ese usuario, para el login.
        password: Password en claro de ese usuario.
        contact_id: Contacto con identificador, conversacion y mensajes.
        conversation_id: Su conversacion.
        inbound_id: Mensaje entrante (palabras del contacto).
        outbound_id: Mensaje saliente (respuesta de la empresa).
    """

    def __init__(self, **kwargs: Any) -> None:
        """Guarda los ids del escenario."""
        for clave, valor in kwargs.items():
            setattr(self, clave, valor)


PASSWORD = "Contrasena-De-Prueba-123"


async def _sembrar(slug: str) -> Escenario:
    """Crea tenant, usuario admin, contacto, conversacion y dos mensajes.

    Args:
        slug: Slug del tenant (unico).

    Returns:
        Escenario con los ids sembrados.
    """
    ids = {
        "client_id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "contact_id": uuid.uuid4(),
        "conversation_id": uuid.uuid4(),
        "inbound_id": uuid.uuid4(),
        "outbound_id": uuid.uuid4(),
    }
    email = f"admin-{uuid.uuid4().hex[:12]}@example.com"

    async with tenant_session(ids["client_id"]) as session:
        await session.execute(
            text(
                "INSERT INTO clients (id, name, slug, plan, is_active) "
                "VALUES (:id, :name, :slug, 'free', true)"
            ),
            {"id": str(ids["client_id"]), "name": f"Tenant {slug}", "slug": slug},
        )
        await session.execute(
            text(
                "INSERT INTO users (id, client_id, email, password_hash, first_name, "
                "last_name, role, is_active) VALUES (:id, :cid, :email, :hash, 'Ada', "
                "'Admin', 'admin', true)"
            ),
            {
                "id": str(ids["user_id"]),
                "cid": str(ids["client_id"]),
                "email": email,
                "hash": hash_password(PASSWORD),
            },
        )
        await session.execute(
            text(
                "INSERT INTO contacts (id, client_id, first_name, last_name, display_name, "
                "metadata) VALUES (:id, :cid, 'Grace', 'Hopper', 'Grace H.', "
                '\'{"origen": "webhook"}\'::jsonb)'
            ),
            {"id": str(ids["contact_id"]), "cid": str(ids["client_id"])},
        )
        await session.execute(
            text(
                "INSERT INTO contact_identifiers (client_id, contact_id, channel, "
                "identifier_value) VALUES (:cid, :contact, 'whatsapp', :valor)"
            ),
            {
                "cid": str(ids["client_id"]),
                "contact": str(ids["contact_id"]),
                "valor": f"57300{uuid.uuid4().int % 10_000_000:07d}",
            },
        )
        await session.execute(
            text(
                "INSERT INTO conversations (id, client_id, contact_id, channel, status) "
                "VALUES (:id, :cid, :contact, 'whatsapp', 'bot_active')"
            ),
            {
                "id": str(ids["conversation_id"]),
                "cid": str(ids["client_id"]),
                "contact": str(ids["contact_id"]),
            },
        )
        for mensaje_id, direccion, contenido, remitente in (
            (ids["inbound_id"], "inbound", "mi telefono es 600123123", "contact"),
            (ids["outbound_id"], "outbound", "Gracias, lo anotamos.", "bot"),
        ):
            await session.execute(
                text(
                    "INSERT INTO messages (id, client_id, conversation_id, direction, "
                    "message_type, content, sender_type) VALUES (:id, :cid, :conv, "
                    ":dir, 'text', :contenido, :remitente)"
                ),
                {
                    "id": str(mensaje_id),
                    "cid": str(ids["client_id"]),
                    "conv": str(ids["conversation_id"]),
                    "dir": direccion,
                    "contenido": contenido,
                    "remitente": remitente,
                },
            )

    return Escenario(email=email, password=PASSWORD, **ids)


async def _limpiar(client_id: uuid.UUID) -> None:
    """Borra el tenant y todo lo que cuelga de el, en orden de FKs.

    `audit_logs` va primero: referencia a `clients` y a `users`, y ademas el
    propio borrado de los mensajes y contactos genera filas nuevas, asi que se
    limpia otra vez al final.

    Args:
        client_id: Tenant a borrar.
    """
    async with tenant_session(client_id) as session:
        for tabla in (
            "audit_logs",
            "messages",
            "conversations",
            "contact_identifiers",
            "quick_replies",
            "contacts",
        ):
            await session.execute(
                text(f"DELETE FROM {tabla} WHERE client_id = :cid"),  # noqa: S608
                {"cid": str(client_id)},
            )
        # Los DELETE de arriba dispararon el trigger otra vez.
        await session.execute(
            text("DELETE FROM audit_logs WHERE client_id = :cid"), {"cid": str(client_id)}
        )
        await session.execute(
            text("DELETE FROM users WHERE client_id = :cid"), {"cid": str(client_id)}
        )
        await session.execute(text("DELETE FROM clients WHERE id = :cid"), {"cid": str(client_id)})


def _cliente(escenario: Escenario, role: str = "admin") -> AsyncClient:
    """Cliente HTTP autenticado como el usuario del tenant sembrado.

    Args:
        escenario: Tenant al que pertenece el usuario.
        role: Rol con el que se firma el token.

    Returns:
        Cliente listo para pegarle a la app.
    """
    token = create_access_token(
        {
            "user_id": str(escenario.user_id),  # type: ignore[attr-defined]
            "client_id": str(escenario.client_id),  # type: ignore[attr-defined]
            "email": escenario.email,  # type: ignore[attr-defined]
            "role": role,
        }
    )
    return AsyncClient(
        transport=ASGITransport(app=create_app()),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )


async def _auditoria(escenario: Escenario, tabla: str, record_id: uuid.UUID) -> list[Any]:
    """Devuelve las filas de auditoria de un registro, de la mas vieja a la mas nueva.

    Args:
        escenario: Tenant sembrado.
        tabla: Nombre de la tabla auditada.
        record_id: Fila auditada.

    Returns:
        Las filas de `audit_logs` que le corresponden.
    """
    async with tenant_session(escenario.client_id) as session:  # type: ignore[attr-defined]
        return (
            await session.execute(
                text(
                    "SELECT action::text, old_values, new_values, user_id, table_name "
                    "FROM audit_logs WHERE client_id = :cid AND table_name = :tabla "
                    "AND record_id = :rid ORDER BY created_at ASC"
                ),
                {
                    "cid": str(escenario.client_id),  # type: ignore[attr-defined]
                    "tabla": tabla,
                    "rid": str(record_id),
                },
            )
        ).all()


@pytest_asyncio.fixture
async def escenario() -> AsyncGenerator[Escenario, None]:
    """Siembra un tenant completo y lo limpia al terminar."""
    await engine.dispose()
    datos = await _sembrar(f"audit-{uuid.uuid4().hex[:8]}")
    yield datos
    await _limpiar(datos.client_id)  # type: ignore[attr-defined]


@pytest_asyncio.fixture
async def dos_tenants() -> AsyncGenerator[tuple[Escenario, Escenario], None]:
    """Siembra dos tenants para los tests de aislamiento."""
    await engine.dispose()
    a = await _sembrar(f"audit-a-{uuid.uuid4().hex[:8]}")
    b = await _sembrar(f"audit-b-{uuid.uuid4().hex[:8]}")
    yield a, b
    await _limpiar(a.client_id)  # type: ignore[attr-defined]
    await _limpiar(b.client_id)  # type: ignore[attr-defined]


# ─── El trigger de auditoria ─────────────────────────────────────────────────


class TestTriggerDeAuditoria:
    """Lo que el trigger de la migracion 006 graba de verdad."""

    async def test_el_insert_del_contacto_quedo_registrado(self, escenario: Escenario) -> None:
        """Sembrar el contacto ya genero su fila de auditoria."""
        filas = await _auditoria(escenario, "contacts", escenario.contact_id)  # type: ignore[attr-defined]

        assert len(filas) == 1
        assert filas[0].action == "INSERT"
        assert filas[0].old_values is None
        assert filas[0].new_values["first_name"] == "Grace"

    async def test_un_update_guarda_el_antes_y_el_despues(self, escenario: Escenario) -> None:
        """`old_values` y `new_values` permiten reconstruir el cambio."""
        async with tenant_session(escenario.client_id) as session:  # type: ignore[attr-defined]
            await session.execute(
                text("UPDATE contacts SET first_name = 'Ada' WHERE id = :id"),
                {"id": str(escenario.contact_id)},  # type: ignore[attr-defined]
            )

        filas = await _auditoria(escenario, "contacts", escenario.contact_id)  # type: ignore[attr-defined]

        assert [f.action for f in filas] == ["INSERT", "UPDATE"]
        assert filas[1].old_values["first_name"] == "Grace"
        assert filas[1].new_values["first_name"] == "Ada"

    async def test_un_delete_guarda_la_fila_completa(self, escenario: Escenario) -> None:
        """Lo borrado se puede reconstruir desde el rastro."""
        async with tenant_session(escenario.client_id) as session:  # type: ignore[attr-defined]
            await session.execute(
                text("DELETE FROM messages WHERE id = :id"),
                {"id": str(escenario.outbound_id)},  # type: ignore[attr-defined]
            )

        filas = await _auditoria(escenario, "messages", escenario.outbound_id)  # type: ignore[attr-defined]

        assert filas[-1].action == "DELETE"
        assert filas[-1].new_values is None
        assert filas[-1].old_values["content"] == "Gracias, lo anotamos."

    async def test_audita_las_tres_tablas(self, escenario: Escenario) -> None:
        """contacts, conversations y messages llevan trigger."""
        for tabla, record_id in (
            ("contacts", escenario.contact_id),  # type: ignore[attr-defined]
            ("conversations", escenario.conversation_id),  # type: ignore[attr-defined]
            ("messages", escenario.inbound_id),  # type: ignore[attr-defined]
        ):
            filas = await _auditoria(escenario, tabla, record_id)
            assert filas, f"{tabla} no genero rastro"

    async def test_sin_usuario_el_autor_queda_nulo(self, escenario: Escenario) -> None:
        """Un worker de Celery no actua en nombre de nadie.

        El sembrado corrio sin contexto de usuario, como lo haria un worker.
        """
        filas = await _auditoria(escenario, "contacts", escenario.contact_id)  # type: ignore[attr-defined]

        assert filas[0].user_id is None

    async def test_con_usuario_el_autor_queda_registrado(self, escenario: Escenario) -> None:
        """Lo que hace una persona queda a su nombre."""
        async with tenant_session(
            escenario.client_id,  # type: ignore[attr-defined]
            user_id=escenario.user_id,  # type: ignore[attr-defined]
        ) as session:
            await session.execute(
                text("UPDATE contacts SET last_name = 'Cambiada' WHERE id = :id"),
                {"id": str(escenario.contact_id)},  # type: ignore[attr-defined]
            )

        filas = await _auditoria(escenario, "contacts", escenario.contact_id)  # type: ignore[attr-defined]

        assert filas[-1].user_id == escenario.user_id  # type: ignore[attr-defined]

    async def test_un_cambio_por_la_api_queda_a_nombre_del_usuario_del_token(
        self, escenario: Escenario
    ) -> None:
        """La cadena completa: JWT -> middleware -> ContextVar -> GUC -> trigger.

        Es lo que hace util al rastro: sin esto, todo cambio hecho desde la API
        apareceria como accion del sistema.
        """
        async with _cliente(escenario) as client:
            respuesta = await client.put(
                f"/api/v1/contacts/{escenario.contact_id}",  # type: ignore[attr-defined]
                json={"display_name": "Editado por la API"},
            )
        assert respuesta.status_code == 200

        filas = await _auditoria(escenario, "contacts", escenario.contact_id)  # type: ignore[attr-defined]

        assert filas[-1].action == "UPDATE"
        assert filas[-1].new_values["display_name"] == "Editado por la API"
        assert filas[-1].user_id == escenario.user_id  # type: ignore[attr-defined]


# ─── Aislamiento del rastro ──────────────────────────────────────────────────


class TestAislamientoDelRastro:
    """El rastro de un tenant no se ve desde otro."""

    async def test_cada_tenant_solo_ve_su_rastro(
        self, dos_tenants: tuple[Escenario, Escenario]
    ) -> None:
        """La RLS de `audit_logs` filtra igual que la de las demas tablas."""
        a, b = dos_tenants

        async with tenant_session(a.client_id) as session:  # type: ignore[attr-defined]
            filas_a = (
                (await session.execute(text("SELECT client_id FROM audit_logs"))).scalars().all()
            )

        assert filas_a, "el tenant A deberia ver su propio rastro"
        assert all(cid == a.client_id for cid in filas_a)  # type: ignore[attr-defined]
        assert b.client_id not in filas_a  # type: ignore[attr-defined]

    async def test_el_rastro_del_vecino_no_se_puede_leer_ni_apuntando_al_id(
        self, dos_tenants: tuple[Escenario, Escenario]
    ) -> None:
        """Ni con el id del registro del otro tenant en la mano."""
        a, b = dos_tenants

        async with tenant_session(a.client_id) as session:  # type: ignore[attr-defined]
            total = await session.scalar(
                text("SELECT count(*) FROM audit_logs WHERE record_id = :rid"),
                {"rid": str(b.contact_id)},  # type: ignore[attr-defined]
            )

        assert total == 0


# ─── RGPD de punta a punta ───────────────────────────────────────────────────


class TestRgpd:
    """Export y anonimizacion por HTTP, contra datos reales."""

    async def test_el_export_trae_todo(self, escenario: Escenario) -> None:
        """Contacto, identificadores y conversaciones con sus dos mensajes."""
        async with _cliente(escenario) as client:
            respuesta = await client.get(
                f"{ADMIN}/{escenario.contact_id}/export"  # type: ignore[attr-defined]
            )

        assert respuesta.status_code == 200
        cuerpo = respuesta.json()
        assert cuerpo["contact"]["first_name"] == "Grace"
        assert cuerpo["contact"]["metadata"] == {"origen": "webhook"}
        assert len(cuerpo["identifiers"]) == 1
        assert len(cuerpo["conversations"]) == 1
        assert len(cuerpo["conversations"][0]["messages"]) == 2

    async def test_la_anonimizacion_borra_los_datos_personales(self, escenario: Escenario) -> None:
        """Lo que queda en la base ya no identifica a nadie."""
        async with _cliente(escenario) as client:
            respuesta = await client.delete(
                f"{ADMIN}/{escenario.contact_id}/gdpr-delete"  # type: ignore[attr-defined]
            )
        assert respuesta.status_code == 200

        async with tenant_session(escenario.client_id) as session:  # type: ignore[attr-defined]
            contacto = (
                await session.execute(
                    text(
                        "SELECT first_name, last_name, display_name, metadata, "
                        "is_gdpr_deleted, gdpr_deleted_at FROM contacts WHERE id = :id"
                    ),
                    {"id": str(escenario.contact_id)},  # type: ignore[attr-defined]
                )
            ).one()
            identificador = await session.scalar(
                text("SELECT identifier_value FROM contact_identifiers WHERE contact_id = :id"),
                {"id": str(escenario.contact_id)},  # type: ignore[attr-defined]
            )

        assert contacto.first_name == "[ELIMINADO]"
        assert contacto.display_name == "[ELIMINADO]"
        assert contacto.metadata == {}
        assert contacto.is_gdpr_deleted is True
        assert contacto.gdpr_deleted_at is not None
        assert identificador.startswith("[ELIMINADO-")

    async def test_conserva_el_mensaje_saliente_y_borra_el_entrante(
        self, escenario: Escenario
    ) -> None:
        """Lo que dijo el contacto se va; lo que respondio la empresa se queda.

        Borrar tambien lo saliente destruiria la trazabilidad de la atencion.
        """
        async with _cliente(escenario) as client:
            await client.delete(f"{ADMIN}/{escenario.contact_id}/gdpr-delete")  # type: ignore[attr-defined]

        async with tenant_session(escenario.client_id) as session:  # type: ignore[attr-defined]
            filas = (
                await session.execute(
                    text(
                        "SELECT direction, content FROM messages "
                        "WHERE conversation_id = :conv ORDER BY direction"
                    ),
                    {"conv": str(escenario.conversation_id)},  # type: ignore[attr-defined]
                )
            ).all()

        por_direccion = {f.direction: f.content for f in filas}
        assert por_direccion["inbound"] == "[CONTENIDO ELIMINADO POR SOLICITUD RGPD]"
        assert por_direccion["outbound"] == "Gracias, lo anotamos."

    async def test_la_conversacion_sobrevive(self, escenario: Escenario) -> None:
        """La supresion no debe llevarse el historico agregado del tenant."""
        async with _cliente(escenario) as client:
            await client.delete(f"{ADMIN}/{escenario.contact_id}/gdpr-delete")  # type: ignore[attr-defined]

        async with tenant_session(escenario.client_id) as session:  # type: ignore[attr-defined]
            total = await session.scalar(
                text("SELECT count(*) FROM conversations WHERE contact_id = :id"),
                {"id": str(escenario.contact_id)},  # type: ignore[attr-defined]
            )

        assert total == 1

    async def test_la_anonimizacion_queda_auditada_a_nombre_de_quien_la_pidio(
        self, escenario: Escenario
    ) -> None:
        """Una supresion de datos sin constancia de quien la ordeno no sirve."""
        async with _cliente(escenario) as client:
            await client.delete(f"{ADMIN}/{escenario.contact_id}/gdpr-delete")  # type: ignore[attr-defined]

        filas = await _auditoria(escenario, "contacts", escenario.contact_id)  # type: ignore[attr-defined]

        ultima = filas[-1]
        assert ultima.action == "UPDATE"
        assert ultima.old_values["first_name"] == "Grace"
        assert ultima.new_values["first_name"] == "[ELIMINADO]"
        assert ultima.user_id == escenario.user_id  # type: ignore[attr-defined]

    async def test_repetirla_es_400(self, escenario: Escenario) -> None:
        """Un doble clic no reescribe la fecha real de la supresion."""
        async with _cliente(escenario) as client:
            primera = await client.delete(
                f"{ADMIN}/{escenario.contact_id}/gdpr-delete"  # type: ignore[attr-defined]
            )
            segunda = await client.delete(
                f"{ADMIN}/{escenario.contact_id}/gdpr-delete"  # type: ignore[attr-defined]
            )

        assert primera.status_code == 200
        assert segunda.status_code == 400

    async def test_no_se_puede_exportar_el_contacto_de_otro_tenant(
        self, dos_tenants: tuple[Escenario, Escenario]
    ) -> None:
        """El export es la via mas directa a los datos personales: 404, no fuga."""
        a, b = dos_tenants

        async with _cliente(a) as cliente_a:
            respuesta = await cliente_a.get(
                f"{ADMIN}/{b.contact_id}/export"  # type: ignore[attr-defined]
            )

        assert respuesta.status_code == 404


# ─── Quick replies contra la base real ───────────────────────────────────────


class TestQuickRepliesEnLaBase:
    """El unique de `shortcut` y el ciclo completo por HTTP."""

    async def test_ciclo_completo(self, escenario: Escenario) -> None:
        """Crear, listar, editar, resolver variables y borrar."""
        async with _cliente(escenario) as client:
            creada = await client.post(
                "/api/v1/quick-replies",
                json={
                    "shortcut": "/saludo",
                    "title": "Saludo",
                    "content": "Hola {{contact_name}}, ticket {{ticket_id}}",
                    "category": "saludos",
                },
            )
            assert creada.status_code == 201
            qr_id = creada.json()["id"]
            assert creada.json()["created_by"] == str(escenario.user_id)  # type: ignore[attr-defined]

            listado = await client.get("/api/v1/quick-replies")
            assert [q["shortcut"] for q in listado.json()] == ["/saludo"]

            render = await client.post(
                f"/api/v1/quick-replies/{qr_id}/render",
                json={"conversation_id": str(escenario.conversation_id)},  # type: ignore[attr-defined]
            )
            assert render.status_code == 200
            assert render.json()["content"].startswith("Hola Grace H., ticket ")
            assert render.json()["unresolved"] == []

            borrada = await client.delete(f"/api/v1/quick-replies/{qr_id}")
            assert borrada.status_code == 200

    async def test_el_atajo_repetido_choca_con_el_unique(self, escenario: Escenario) -> None:
        """`uq_quick_reply_shortcut` existe; el endpoint lo traduce a 409."""
        async with _cliente(escenario) as client:
            primera = await client.post(
                "/api/v1/quick-replies",
                json={"shortcut": "/repetido", "title": "Uno", "content": "x"},
            )
            segunda = await client.post(
                "/api/v1/quick-replies",
                json={"shortcut": "/repetido", "title": "Dos", "content": "y"},
            )

        assert primera.status_code == 201
        assert segunda.status_code == 409

    async def test_dos_tenants_pueden_usar_el_mismo_atajo(
        self, dos_tenants: tuple[Escenario, Escenario]
    ) -> None:
        """El unique es por tenant, no global."""
        a, b = dos_tenants

        async with _cliente(a) as cliente_a, _cliente(b) as cliente_b:
            en_a = await cliente_a.post(
                "/api/v1/quick-replies",
                json={"shortcut": "/saludo", "title": "A", "content": "x"},
            )
            en_b = await cliente_b.post(
                "/api/v1/quick-replies",
                json={"shortcut": "/saludo", "title": "B", "content": "y"},
            )

        assert en_a.status_code == 201
        assert en_b.status_code == 201


# ─── Comprobacion del login (no es de este sprint) ───────────────────────────


class TestLoginBajoRls:
    """BUG-025 (CERRADO): el login funciona contra un rol sujeto a RLS.

    Autenticar es la unica operacion que necesita mirar `users` sin saber a que
    tenant pertenece la fila — el tenant se deduce del usuario, y el usuario es
    justo lo que se busca. Antes, la politica de `users` hacia fallar esa
    consulta contra un rol con `NOBYPASSRLS` y **ningun login funcionaba**.

    Ahora la busqueda va por `auth_lookup_user()`, una funcion SECURITY DEFINER
    (migracion 007) que es el unico punto del sistema con ese acceso. Estos
    tests corren con `app_user` (NOBYPASSRLS), que es lo que hace la
    verificacion genuina: contra un superusuario pasarian aunque la funcion no
    existiera.
    """

    async def test_el_login_funciona_contra_la_base_real(self, escenario: Escenario) -> None:
        """Credenciales validas devuelven 200 y un token utilizable."""
        transport = ASGITransport(app=create_app())
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            respuesta = await client.post(
                "/api/v1/auth/login",
                json={
                    "email": escenario.email,  # type: ignore[attr-defined]
                    "password": PASSWORD,
                },
            )

        assert respuesta.status_code == 200, (
            f"El login fallo: {respuesta.status_code} {respuesta.text[:300]}"
        )
        cuerpo = respuesta.json()
        assert cuerpo["access_token"]
        assert cuerpo["refresh_token"]

    async def test_el_token_emitido_sirve_para_operar(self, escenario: Escenario) -> None:
        """El JWT del login abre la API: lleva el tenant y el rol correctos.

        Sin esto, el login podria devolver 200 con un token mal formado y el
        test anterior no se enteraria.
        """
        transport = ASGITransport(app=create_app())
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            login = await client.post(
                "/api/v1/auth/login",
                json={
                    "email": escenario.email,  # type: ignore[attr-defined]
                    "password": PASSWORD,
                },
            )
            token = login.json()["access_token"]

            contactos = await client.get(
                "/api/v1/contacts", headers={"Authorization": f"Bearer {token}"}
            )

        assert contactos.status_code == 200
        ids = {c["id"] for c in contactos.json()["items"]}
        assert str(escenario.contact_id) in ids  # type: ignore[attr-defined]

    async def test_password_incorrecto_sigue_siendo_401(self, escenario: Escenario) -> None:
        """El arreglo no abre la puerta: la verificacion de password no cambia."""
        transport = ASGITransport(app=create_app())
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            respuesta = await client.post(
                "/api/v1/auth/login",
                json={
                    "email": escenario.email,  # type: ignore[attr-defined]
                    "password": "no-es-la-buena",
                },
            )

        assert respuesta.status_code == 401

    async def test_email_inexistente_es_401(self, escenario: Escenario) -> None:
        """Un email que no existe no encuentra fila y no entra."""
        transport = ASGITransport(app=create_app())
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            respuesta = await client.post(
                "/api/v1/auth/login",
                json={"email": "nadie@example.com", "password": PASSWORD},
            )

        assert respuesta.status_code == 401

    async def test_el_login_anota_el_ultimo_acceso(self, escenario: Escenario) -> None:
        """La otra mitad de BUG-025: el UPDATE sobre `users` tambien iba bloqueado.

        Ahora corre dentro de `tenant_session()`, con la RLS aplicada. Se
        verifica de verdad porque el endpoint se traga los fallos de esta
        escritura a proposito (es contabilidad, no autenticacion), y sin este
        test una regresion ahi seria invisible.
        """
        async with tenant_session(escenario.client_id) as session:  # type: ignore[attr-defined]
            antes = await session.scalar(
                text("SELECT last_login_at FROM users WHERE id = :id"),
                {"id": str(escenario.user_id)},  # type: ignore[attr-defined]
            )
        assert antes is None

        transport = ASGITransport(app=create_app())
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(
                "/api/v1/auth/login",
                json={
                    "email": escenario.email,  # type: ignore[attr-defined]
                    "password": PASSWORD,
                },
            )

        async with tenant_session(escenario.client_id) as session:  # type: ignore[attr-defined]
            despues = await session.scalar(
                text("SELECT last_login_at FROM users WHERE id = :id"),
                {"id": str(escenario.user_id)},  # type: ignore[attr-defined]
            )

        assert despues is not None

    async def test_un_tenant_suspendido_no_deja_entrar_a_los_suyos(
        self, escenario: Escenario
    ) -> None:
        """El estado del tenant viaja en la misma fila que el usuario.

        `clients` tambien tiene RLS, asi que comprobarlo con una segunda
        consulta desde el login volveria a chocar con BUG-025.
        """
        async with tenant_session(escenario.client_id) as session:  # type: ignore[attr-defined]
            await session.execute(
                text("UPDATE clients SET is_active = false WHERE id = :id"),
                {"id": str(escenario.client_id)},  # type: ignore[attr-defined]
            )

        transport = ASGITransport(app=create_app())
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            respuesta = await client.post(
                "/api/v1/auth/login",
                json={
                    "email": escenario.email,  # type: ignore[attr-defined]
                    "password": PASSWORD,
                },
            )

        assert respuesta.status_code == 401
        assert respuesta.json()["error_code"] == "FORBIDDEN"


class TestFuncionDeBusqueda:
    """`auth_lookup_user()`: alcance y endurecimiento.

    Es la unica grieta deliberada en la RLS del sistema, asi que conviene que
    esa grieta tenga tests propios y no solo los del endpoint que la usa.
    """

    async def test_devuelve_solo_la_fila_del_email_exacto(
        self, dos_tenants: tuple[Escenario, Escenario]
    ) -> None:
        """No sirve para enumerar usuarios ni para cruzar tenants.

        Se llama desde el contexto del tenant A pidiendo el email de B: devuelve
        esa fila y nada mas, que es exactamente para lo que existe. Lo que NO
        puede hacer es devolver la tabla entera.
        """
        a, b = dos_tenants

        async with tenant_session(a.client_id) as session:  # type: ignore[attr-defined]
            filas = (
                await session.execute(
                    text("SELECT id, client_id FROM auth_lookup_user(:email)"),
                    {"email": b.email},  # type: ignore[attr-defined]
                )
            ).all()

        assert len(filas) == 1
        assert filas[0].id == b.user_id  # type: ignore[attr-defined]

    async def test_un_email_inexistente_no_devuelve_nada(self, escenario: Escenario) -> None:
        """Sin coincidencia exacta, cero filas."""
        async with tenant_session(escenario.client_id) as session:  # type: ignore[attr-defined]
            filas = (
                await session.execute(
                    text("SELECT id FROM auth_lookup_user(:email)"),
                    {"email": "nadie@example.com"},
                )
            ).all()

        assert filas == []

    async def test_es_security_definer_con_search_path_fijo(self, escenario: Escenario) -> None:
        """Las dos propiedades de las que depende que el arreglo sea seguro.

        Sin SECURITY DEFINER la funcion no esquiva la RLS y el login vuelve a
        fallar. Sin `search_path` fijo, quien la llama puede anteponer un schema
        propio con una tabla `users` falsa y hacer que la funcion privilegiada
        lea lo que el atacante quiera — la escalada clasica de SECURITY DEFINER.
        """
        async with tenant_session(escenario.client_id) as session:  # type: ignore[attr-defined]
            fila = (
                await session.execute(
                    text(
                        "SELECT prosecdef, proconfig FROM pg_proc "
                        "WHERE proname = 'auth_lookup_user'"
                    )
                )
            ).one()

        assert fila.prosecdef is True, "auth_lookup_user() no es SECURITY DEFINER"
        assert fila.proconfig is not None, "auth_lookup_user() no fija search_path"
        assert any(c.startswith("search_path=") for c in fila.proconfig)

    async def test_el_resto_de_la_rls_sigue_intacta(self, escenario: Escenario) -> None:
        """El arreglo no relaja `users`: sin la funcion, la politica sigue ahi.

        Es lo que separa esta salida de las otras dos que planteaba BUG-025
        (politica adicional, o rol con BYPASSRLS): la excepcion se queda acotada
        a una firma concreta y todo lo demas sigue como estaba.
        """
        async with tenant_session(escenario.client_id) as session:  # type: ignore[attr-defined]
            forzada = await session.scalar(
                text("SELECT relforcerowsecurity FROM pg_class WHERE relname = 'users'")
            )
            politicas = await session.scalar(
                text("SELECT count(*) FROM pg_policies WHERE tablename = 'users'")
            )

        assert forzada is True, "users dejo de tener FORCE ROW LEVEL SECURITY"
        assert politicas >= 1
