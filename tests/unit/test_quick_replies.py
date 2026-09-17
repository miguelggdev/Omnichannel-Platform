"""Tests del CRUD de respuestas rapidas y de la resolucion de sus variables.

La base se sustituye por `CrmSession`: corren sin `--run-db`.
"""

import uuid
from datetime import datetime, timezone
from typing import Any

import pytest

from app.api.v1 import quick_replies as quick_replies_module
from app.services.quick_reply import VARIABLE_RESOLVERS, resolve_quick_reply
from tests.unit.agent_doubles import fake_tenant_session
from tests.unit.crm_doubles import AHORA, CrmSession, FakeContact, FakeConversation, FakeUser

URL = "/api/v1/quick-replies"


class FakeQuickReply:
    """Sustituto de QuickReply con lo que leen los endpoints."""

    def __init__(self, **kwargs: Any) -> None:
        """Construye la respuesta rapida con valores por defecto razonables."""
        self.id = kwargs.get("id") or uuid.uuid4()
        self.client_id = kwargs.get("client_id") or uuid.uuid4()
        self.shortcut = kwargs.get("shortcut", "/saludo")
        self.title = kwargs.get("title", "Saludo inicial")
        self.content = kwargs.get("content", "Hola {{contact_name}}")
        self.category = kwargs.get("category", "saludos")
        self.created_by = kwargs.get("created_by")
        self.created_at = kwargs.get("created_at", AHORA)


def _usa_sesion(monkeypatch: pytest.MonkeyPatch, session: CrmSession) -> CrmSession:
    """Hace que los endpoints de respuestas rapidas usen la sesion falsa."""
    monkeypatch.setattr(quick_replies_module, "tenant_session", fake_tenant_session(session))
    return session


# ─── Resolucion de variables ─────────────────────────────────────────────────


class TestResolucionDeVariables:
    """`resolve_quick_reply()`, sin tocar base ni API."""

    def test_resuelve_las_cuatro_variables_conocidas(self) -> None:
        """Las cuatro que documenta la spec §11.3 tienen resolver."""
        assert set(VARIABLE_RESOLVERS) == {"contact_name", "agent_name", "ticket_id", "date"}

    def test_sustituye_el_nombre_del_contacto(self) -> None:
        """`display_name` gana: es como el propio contacto se presenta en el canal."""
        contacto = FakeContact(display_name="Ada L.", first_name="Ada", last_name="Lovelace")

        texto, sin_resolver = resolve_quick_reply("Hola {{contact_name}}", {"contact": contacto})

        assert texto == "Hola Ada L."
        assert sin_resolver == []

    def test_sin_display_name_arma_nombre_y_apellido(self) -> None:
        """Si no hay nombre para mostrar, se compone con lo que haya."""
        contacto = FakeContact(display_name=None, first_name="Ada", last_name="Lovelace")

        texto, _ = resolve_quick_reply("Hola {{contact_name}}", {"contact": contacto})

        assert texto == "Hola Ada Lovelace"

    def test_admite_espacios_dentro_de_las_llaves(self) -> None:
        """`{{ contact_name }}` es lo mismo que `{{contact_name}}`."""
        contacto = FakeContact(display_name="Ada")

        texto, _ = resolve_quick_reply("Hola {{ contact_name }}", {"contact": contacto})

        assert texto == "Hola Ada"

    def test_el_ticket_son_los_ocho_primeros_del_uuid(self) -> None:
        """Un identificador que el cliente pueda leer por telefono."""
        conv = FakeConversation()

        texto, _ = resolve_quick_reply("Ticket {{ticket_id}}", {"conversation": conv})

        assert texto == f"Ticket {str(conv.id)[:8]}"

    def test_la_fecha_es_la_de_hoy(self) -> None:
        """`{{date}}` no necesita contexto."""
        texto, sin_resolver = resolve_quick_reply("Hoy es {{date}}", {})

        assert texto == f"Hoy es {datetime.now(timezone.utc).strftime('%d/%m/%Y')}"
        assert sin_resolver == []

    def test_una_variable_desconocida_queda_intacta_y_se_reporta(self) -> None:
        """Mejor que el agente vea el marcador y lo corrija, a mandar un hueco.

        Sustituirla por vacio dejaria "Hola ," camino al cliente final sin que
        nadie se entere.
        """
        texto, sin_resolver = resolve_quick_reply("Hola {{inventada}},", {})

        assert texto == "Hola {{inventada}},"
        assert sin_resolver == ["inventada"]

    def test_sin_contexto_la_variable_tampoco_se_sustituye(self) -> None:
        """Falta el contacto: la variable se conoce pero no se puede resolver."""
        texto, sin_resolver = resolve_quick_reply("Hola {{contact_name}}", {})

        assert texto == "Hola {{contact_name}}"
        assert sin_resolver == ["contact_name"]

    def test_un_contacto_sin_nombre_cuenta_como_sin_resolver(self) -> None:
        """Un contacto recien creado por webhook puede no tener ningun nombre."""
        contacto = FakeContact(display_name=None, first_name=None, last_name=None)

        texto, sin_resolver = resolve_quick_reply("Hola {{contact_name}}", {"contact": contacto})

        assert texto == "Hola {{contact_name}}"
        assert sin_resolver == ["contact_name"]

    def test_no_repite_una_variable_que_aparece_dos_veces(self) -> None:
        """El reporte lista cada variable una sola vez."""
        _, sin_resolver = resolve_quick_reply("{{x}} y {{x}} y {{y}}", {})

        assert sin_resolver == ["x", "y"]

    def test_un_texto_sin_variables_sale_igual(self) -> None:
        """El caso comun: una respuesta fija."""
        texto, sin_resolver = resolve_quick_reply("Gracias por escribirnos.", {})

        assert texto == "Gracias por escribirnos."
        assert sin_resolver == []

    def test_resuelve_varias_variables_a_la_vez(self) -> None:
        """Un saludo real combina contacto, agente y ticket."""
        contexto = {
            "contact": FakeContact(display_name="Ada"),
            "agent": FakeUser(first_name="Grace", last_name="Hopper"),
            "conversation": FakeConversation(),
        }

        texto, sin_resolver = resolve_quick_reply(
            "Hola {{contact_name}}, soy {{agent_name}} (ticket {{ticket_id}})", contexto
        )

        assert texto.startswith("Hola Ada, soy Grace Hopper (ticket ")
        assert sin_resolver == []


# ─── GET /quick-replies ──────────────────────────────────────────────────────


class TestListado:
    """Listado de respuestas rapidas."""

    async def test_devuelve_las_del_tenant_ordenadas_por_atajo(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La UI las ofrece mientras el agente teclea: el orden es por atajo."""
        session = _usa_sesion(
            monkeypatch,
            CrmSession(
                resultados=[[FakeQuickReply(shortcut="/adios"), FakeQuickReply(shortcut="/hola")]]
            ),
        )

        response = await authenticated_client.get(URL)

        assert response.status_code == 200
        assert [q["shortcut"] for q in response.json()] == ["/adios", "/hola"]
        assert "order by quick_replies.shortcut asc" in str(session.executed[0]).lower()

    async def test_filtra_por_categoria(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El filtro llega al WHERE."""
        session = _usa_sesion(monkeypatch, CrmSession(resultados=[[]]))

        await authenticated_client.get(URL, params={"category": "saludos"})

        assert "quick_replies.category =" in str(session.executed[0])

    async def test_el_client_id_va_explicito_en_el_where(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El filtro de tenant no queda delegado solo a la RLS (CLAUDE.md §2)."""
        session = _usa_sesion(monkeypatch, CrmSession(resultados=[[]]))

        await authenticated_client.get(URL)

        assert "quick_replies.client_id =" in str(session.executed[0])


# ─── POST /quick-replies ─────────────────────────────────────────────────────


class TestAlta:
    """Creacion de respuestas rapidas."""

    async def test_crea_y_firma_con_el_usuario_del_token(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El autor sale del JWT, no del body."""
        session = _usa_sesion(monkeypatch, CrmSession(resultados=[None]))

        response = await authenticated_client.post(
            URL,
            json={"shortcut": "/saludo", "title": "Saludo", "content": "Hola"},
        )

        assert response.status_code == 201
        assert session.added[0].created_by is not None

    async def test_atajo_repetido_es_409(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El atajo es unico por tenant; se avisa antes de chocar con el unique."""
        _usa_sesion(monkeypatch, CrmSession(resultados=[FakeQuickReply()]))

        response = await authenticated_client.post(
            URL, json={"shortcut": "/saludo", "title": "Otro", "content": "Hola"}
        )

        assert response.status_code == 409
        assert response.json()["error_code"] == "DUPLICATE"

    @pytest.mark.parametrize(
        "shortcut",
        ["saludo", "/Saludo", "/con espacio", "/", "/acentuación", "/saludo!"],
    )
    async def test_atajo_mal_formado_es_422(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch, shortcut: str
    ) -> None:
        """Un atajo se teclea de un tiron: barra, minusculas, sin espacios."""
        _usa_sesion(monkeypatch, CrmSession())

        response = await authenticated_client.post(
            URL, json={"shortcut": shortcut, "title": "x", "content": "y"}
        )

        assert response.status_code == 422

    @pytest.mark.parametrize("shortcut", ["/saludo", "/saludo-inicial", "/saludo_2", "/a1"])
    async def test_atajos_validos(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch, shortcut: str
    ) -> None:
        """Lo que si se acepta."""
        _usa_sesion(monkeypatch, CrmSession(resultados=[None]))

        response = await authenticated_client.post(
            URL, json={"shortcut": shortcut, "title": "x", "content": "y"}
        )

        assert response.status_code == 201

    async def test_contenido_vacio_es_422(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Una respuesta rapida sin contenido no sirve de nada."""
        _usa_sesion(monkeypatch, CrmSession())

        response = await authenticated_client.post(
            URL, json={"shortcut": "/x", "title": "x", "content": ""}
        )

        assert response.status_code == 422


# ─── PUT y DELETE ────────────────────────────────────────────────────────────


class TestEdicionYBorrado:
    """Actualizacion y borrado."""

    async def test_actualiza_solo_lo_enviado(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mandar el contenido no borra el titulo."""
        qr = FakeQuickReply(title="Saludo", content="Hola")
        _usa_sesion(monkeypatch, CrmSession(resultados=[qr]))

        response = await authenticated_client.put(
            f"{URL}/{qr.id}", json={"content": "Buenas tardes"}
        )

        assert response.status_code == 200
        assert qr.content == "Buenas tardes"
        assert qr.title == "Saludo"

    async def test_cambiar_el_atajo_a_uno_ocupado_es_409(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No se puede pisar el atajo de otra respuesta del mismo tenant."""
        qr = FakeQuickReply(shortcut="/saludo")
        _usa_sesion(monkeypatch, CrmSession(resultados=[qr, FakeQuickReply(shortcut="/adios")]))

        response = await authenticated_client.put(f"{URL}/{qr.id}", json={"shortcut": "/adios"})

        assert response.status_code == 409

    async def test_reenviar_el_mismo_atajo_no_es_conflicto(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Editar el titulo mandando el atajo actual no debe chocar consigo misma."""
        qr = FakeQuickReply(shortcut="/saludo")
        _usa_sesion(monkeypatch, CrmSession(resultados=[qr]))

        response = await authenticated_client.put(
            f"{URL}/{qr.id}", json={"shortcut": "/saludo", "title": "Nuevo titulo"}
        )

        assert response.status_code == 200
        assert qr.title == "Nuevo titulo"

    async def test_inexistente_es_404(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Editar lo que no existe responde 404."""
        _usa_sesion(monkeypatch, CrmSession(resultados=[None]))

        response = await authenticated_client.put(f"{URL}/{uuid.uuid4()}", json={"title": "x"})

        assert response.status_code == 404

    async def test_borra(self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """El borrado elimina la fila."""
        qr = FakeQuickReply()
        session = _usa_sesion(monkeypatch, CrmSession(resultados=[qr]))

        response = await authenticated_client.delete(f"{URL}/{qr.id}")

        assert response.status_code == 200
        assert session.deleted == [qr]

    async def test_el_rol_agent_no_puede_borrar(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Borrar afecta a todo el equipo: solo admin y super_admin."""
        _usa_sesion(monkeypatch, CrmSession())
        client = authenticated_client_factory(role="agent")

        response = await client.delete(f"{URL}/{uuid.uuid4()}")

        assert response.status_code == 403

    async def test_el_rol_agent_si_puede_crear(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Escribir plantillas es parte del trabajo de un agente."""
        _usa_sesion(monkeypatch, CrmSession(resultados=[None]))
        client = authenticated_client_factory(role="agent")

        response = await client.post(URL, json={"shortcut": "/x", "title": "x", "content": "y"})

        assert response.status_code == 201


# ─── POST /quick-replies/{id}/render ─────────────────────────────────────────


class TestRender:
    """Resolucion de variables contra una conversacion concreta."""

    async def test_devuelve_el_contenido_resuelto(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch, tenant_a_id: uuid.UUID
    ) -> None:
        """El agente recibe el texto listo para enviar."""
        qr = FakeQuickReply(content="Hola {{contact_name}}, ticket {{ticket_id}}")
        conv = FakeConversation(client_id=tenant_a_id)
        contacto = FakeContact(id=conv.contact_id, display_name="Ada")
        agente = FakeUser(client_id=tenant_a_id, first_name="Grace", last_name="Hopper")
        _usa_sesion(monkeypatch, CrmSession(resultados=[qr, conv, contacto, agente]))

        response = await authenticated_client.post(
            f"{URL}/{qr.id}/render", json={"conversation_id": str(conv.id)}
        )

        assert response.status_code == 200
        cuerpo = response.json()
        assert cuerpo["content"] == f"Hola Ada, ticket {str(conv.id)[:8]}"
        assert cuerpo["unresolved"] == []

    async def test_reporta_lo_que_no_pudo_resolver(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sin contacto cargado, la variable queda marcada en `unresolved`."""
        qr = FakeQuickReply(content="Hola {{contact_name}}")
        conv = FakeConversation()
        _usa_sesion(monkeypatch, CrmSession(resultados=[qr, conv, None, None]))

        response = await authenticated_client.post(
            f"{URL}/{qr.id}/render", json={"conversation_id": str(conv.id)}
        )

        assert response.status_code == 200
        assert response.json()["unresolved"] == ["contact_name"]
        assert "{{contact_name}}" in response.json()["content"]

    async def test_conversacion_inexistente_es_404(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No se resuelve contra una conversacion que no es de este tenant."""
        qr = FakeQuickReply()
        _usa_sesion(monkeypatch, CrmSession(resultados=[qr, None]))

        response = await authenticated_client.post(
            f"{URL}/{qr.id}/render", json={"conversation_id": str(uuid.uuid4())}
        )

        assert response.status_code == 404

    async def test_respuesta_inexistente_es_404(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tampoco se resuelve una respuesta rapida que no existe."""
        _usa_sesion(monkeypatch, CrmSession(resultados=[None]))

        response = await authenticated_client.post(
            f"{URL}/{uuid.uuid4()}/render", json={"conversation_id": str(uuid.uuid4())}
        )

        assert response.status_code == 404


# ─── Autenticacion ───────────────────────────────────────────────────────────


async def test_sin_token_es_401(api_client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Las respuestas rapidas son del tenant: hay que identificarse."""
    _usa_sesion(monkeypatch, CrmSession())

    response = await api_client.get(URL)

    assert response.status_code == 401
