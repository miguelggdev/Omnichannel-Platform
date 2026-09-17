"""Tests del CRUD de contactos.

La base se sustituye por `CrmSession`: estos tests corren sin `--run-db`. El
recorrido contra PostgreSQL real con RLS vive en
`tests/integration/test_crm_api.py`.
"""

import uuid
from typing import Any

import pytest

from app.api.v1 import contacts as contacts_module
from tests.unit.agent_doubles import fake_tenant_session
from tests.unit.crm_doubles import (
    CrmSession,
    FakeContact,
    FakeConversation,
    FakeNote,
    FakeTag,
)

URL = "/api/v1/contacts"


def _usa_sesion(monkeypatch: pytest.MonkeyPatch, session: CrmSession) -> CrmSession:
    """Hace que los endpoints de contactos usen la sesion falsa indicada."""
    monkeypatch.setattr(contacts_module, "tenant_session", fake_tenant_session(session))
    return session


class _FilaTag:
    """Fila (id, name, color) tal como la devuelve el join contra tags."""

    def __init__(self, tag: FakeTag) -> None:
        """Copia los tres campos que lee el endpoint."""
        self.id = tag.id
        self.name = tag.name
        self.color = tag.color


# ─── GET /contacts ───────────────────────────────────────────────────────────


class TestListado:
    """Listado paginado de contactos."""

    async def test_devuelve_la_pagina_y_el_total(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La respuesta trae los contactos y el total de coincidencias."""
        contactos = [FakeContact(first_name="Ada"), FakeContact(first_name="Grace")]
        _usa_sesion(monkeypatch, CrmSession(resultados=[contactos], escalares=[2]))

        response = await authenticated_client.get(URL)

        assert response.status_code == 200
        cuerpo = response.json()
        assert cuerpo["total"] == 2
        assert len(cuerpo["items"]) == 2
        assert {c["first_name"] for c in cuerpo["items"]} == {"Ada", "Grace"}

    async def test_excluye_los_contactos_ya_fusionados(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El WHERE filtra merged_into_id IS NULL: un duplicado no sale en la agenda."""
        session = _usa_sesion(monkeypatch, CrmSession(resultados=[[]], escalares=[0]))

        await authenticated_client.get(URL)

        sql = str(session.executed[0]).lower()
        assert "merged_into_id is null" in sql

    async def test_la_busqueda_va_contra_los_tres_nombres(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`search` busca en first_name, last_name y display_name."""
        session = _usa_sesion(monkeypatch, CrmSession(resultados=[[]], escalares=[0]))

        await authenticated_client.get(URL, params={"search": "ada"})

        # El dialecto por defecto compila ILIKE como `lower(x) LIKE lower(y)`.
        sql = str(session.executed[0]).lower()
        assert sql.count("like lower(") == 3

    async def test_el_filtro_por_tag_tambien_afecta_al_total(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El join por tag va en la query de datos y en la del count.

        Si solo fuera en la de datos, el total contaria contactos que la pagina
        no muestra y la paginacion quedaria descuadrada.
        """
        session = _usa_sesion(monkeypatch, CrmSession(resultados=[[]], escalares=[0]))

        await authenticated_client.get(URL, params={"tag_id": str(uuid.uuid4())})

        sentencias = [str(s).lower() for s in session.executed]
        assert all("join contact_tags" in s for s in sentencias)

    async def test_el_client_id_va_explicito_en_el_where(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El filtro de tenant no queda delegado solo a la RLS (CLAUDE.md §2)."""
        session = _usa_sesion(monkeypatch, CrmSession(resultados=[[]], escalares=[0]))

        await authenticated_client.get(URL)

        assert "contacts.client_id =" in str(session.executed[0])

    @pytest.mark.parametrize("page_size", [0, 101])
    async def test_page_size_fuera_de_rango_es_422(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch, page_size: int
    ) -> None:
        """La paginacion esta acotada entre 1 y 100."""
        _usa_sesion(monkeypatch, CrmSession())

        response = await authenticated_client.get(URL, params={"page_size": page_size})

        assert response.status_code == 422


# ─── POST /contacts ──────────────────────────────────────────────────────────


class TestAlta:
    """Alta manual de contactos."""

    async def test_crea_el_contacto(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un alta valida responde 201 y agrega la fila."""
        session = _usa_sesion(monkeypatch, CrmSession())

        response = await authenticated_client.post(
            URL, json={"first_name": "Ada", "last_name": "Lovelace"}
        )

        assert response.status_code == 201
        assert response.json()["first_name"] == "Ada"
        assert len(session.added) == 1

    async def test_el_tenant_sale_del_token_no_del_body(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch, tenant_a_id: uuid.UUID
    ) -> None:
        """El client_id lo pone el JWT; mandarlo en el body no cambia nada."""
        session = _usa_sesion(monkeypatch, CrmSession())
        otro_tenant = str(uuid.uuid4())

        response = await authenticated_client.post(
            URL, json={"first_name": "Ada", "client_id": otro_tenant}
        )

        assert response.json()["client_id"] == str(tenant_a_id)
        assert session.added[0].client_id == tenant_a_id

    async def test_agent_puede_dar_de_alta(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cargar un contacto es operacion basica: el rol agent la tiene."""
        _usa_sesion(monkeypatch, CrmSession())
        client = authenticated_client_factory(role="agent")

        response = await client.post(URL, json={"first_name": "Ada"})

        assert response.status_code == 201


# ─── GET /contacts/{id} ──────────────────────────────────────────────────────


class TestDetalle:
    """Detalle de un contacto."""

    async def test_devuelve_identificadores_tags_y_notas(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El detalle junta las tres colecciones relacionadas."""
        contacto = FakeContact(metadata_={"origen": "webhook"})
        identificador = type(
            "Ident",
            (),
            {
                "id": uuid.uuid4(),
                "channel": "whatsapp",
                "identifier_value": "+5215500000001",
                "created_at": contacto.created_at,
            },
        )()
        _usa_sesion(
            monkeypatch,
            CrmSession(
                resultados=[
                    contacto,
                    [identificador],
                    [_FilaTag(FakeTag(name="vip"))],
                    [FakeNote(content="llamar el lunes")],
                ]
            ),
        )

        response = await authenticated_client.get(f"{URL}/{contacto.id}")

        assert response.status_code == 200
        cuerpo = response.json()
        assert cuerpo["metadata"] == {"origen": "webhook"}
        assert cuerpo["identifiers"][0]["channel"] == "whatsapp"
        assert cuerpo["tags"][0]["name"] == "vip"
        assert cuerpo["notes"][0]["content"] == "llamar el lunes"

    async def test_el_detalle_corta_las_notas_en_veinte(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El detalle trae solo las ultimas notas; el resto va por su endpoint."""
        contacto = FakeContact()
        session = _usa_sesion(monkeypatch, CrmSession(resultados=[contacto, [], [], []]))

        await authenticated_client.get(f"{URL}/{contacto.id}")

        sql_notas = str(session.executed[-1]).lower()
        assert "limit" in sql_notas
        assert contacts_module.DETAIL_NOTES_LIMIT == 20

    async def test_contacto_inexistente_es_404(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un id que no existe para este tenant responde 404."""
        _usa_sesion(monkeypatch, CrmSession(resultados=[None]))

        response = await authenticated_client.get(f"{URL}/{uuid.uuid4()}")

        assert response.status_code == 404
        assert response.json()["error_code"] == "NOT_FOUND"

    async def test_id_mal_formado_es_422(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un id que no es UUID lo rechaza FastAPI antes de tocar la base."""
        _usa_sesion(monkeypatch, CrmSession())

        response = await authenticated_client.get(f"{URL}/no-es-uuid")

        assert response.status_code == 422


# ─── PUT /contacts/{id} ──────────────────────────────────────────────────────


class TestEdicion:
    """Actualizacion de contactos."""

    async def test_actualiza_solo_los_campos_enviados(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mandar un campo no borra los demas."""
        contacto = FakeContact(first_name="Ada", last_name="Lovelace")
        _usa_sesion(monkeypatch, CrmSession(resultados=[contacto]))

        response = await authenticated_client.put(
            f"{URL}/{contacto.id}", json={"display_name": "Condesa"}
        )

        assert response.status_code == 200
        assert contacto.display_name == "Condesa"
        assert contacto.last_name == "Lovelace"

    async def test_metadata_escribe_en_la_columna_del_modelo(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El campo publico `metadata` mapea a `metadata_` del modelo."""
        contacto = FakeContact(metadata_={"a": 1})
        _usa_sesion(monkeypatch, CrmSession(resultados=[contacto]))

        await authenticated_client.put(f"{URL}/{contacto.id}", json={"metadata": {"b": 2}})

        assert contacto.metadata_ == {"b": 2}
        assert not hasattr(contacto, "metadata")

    async def test_no_deja_editar_un_contacto_fusionado(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Editar un duplicado absorbido es un 400 que apunta al destino."""
        destino = uuid.uuid4()
        contacto = FakeContact(merged_into_id=destino)
        _usa_sesion(monkeypatch, CrmSession(resultados=[contacto]))

        response = await authenticated_client.put(
            f"{URL}/{contacto.id}", json={"first_name": "Ada"}
        )

        assert response.status_code == 400
        assert str(destino) in response.json()["message"]

    async def test_contacto_inexistente_es_404(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Actualizar lo que no existe responde 404."""
        _usa_sesion(monkeypatch, CrmSession(resultados=[None]))

        response = await authenticated_client.put(
            f"{URL}/{uuid.uuid4()}", json={"first_name": "Ada"}
        )

        assert response.status_code == 404


# ─── POST /contacts/{id}/merge/{target_id} ───────────────────────────────────


class TestMerge:
    """Fusion de contactos duplicados."""

    async def test_fusionar_consigo_mismo_es_400(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El caso degenerado se corta antes de tocar la base."""
        _usa_sesion(monkeypatch, CrmSession())
        mismo = uuid.uuid4()

        response = await authenticated_client.post(f"{URL}/{mismo}/merge/{mismo}")

        assert response.status_code == 400

    async def test_fusiona_y_devuelve_el_destino(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Con `ContactUnifier` entregado (Sprint 7, Dev A), la fusion completa responde 200.

        Reemplaza al viejo `test_sin_el_unificador_responde_503_no_500`: su
        premisa (`ContactUnifier` no existe todavia) dejo de ser cierta en
        cuanto se entrego `app/services/contact_unifier.py`. La logica interna
        de `merge()` (mover filas, no duplicar tags) se prueba a fondo en
        `tests/unit/test_contact_unifier.py`; esto solo verifica que el
        endpoint conecta las piezas: valida, llama al unificador y devuelve el
        destino ya actualizado.
        """
        source = FakeContact()
        target = FakeContact()
        sesion = CrmSession(
            resultados=[source, target, None, None, None, [], []],
            objetos={source.id: source},
        )
        _usa_sesion(monkeypatch, sesion)

        response = await authenticated_client.post(f"{URL}/{source.id}/merge/{target.id}")

        assert response.status_code == 200
        assert response.json()["id"] == str(target.id)
        assert source.merged_into_id == target.id
        assert target in sesion.refreshed

    async def test_el_rol_agent_no_puede_fusionar(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La fusion es destructiva: solo admin y super_admin."""
        _usa_sesion(monkeypatch, CrmSession())
        client = authenticated_client_factory(role="agent")

        response = await client.post(f"{URL}/{uuid.uuid4()}/merge/{uuid.uuid4()}")

        assert response.status_code == 403

    async def test_el_permiso_se_verifica_antes_que_la_disponibilidad(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un supervisor recibe 403, no el 503 que delataria el estado interno."""
        _usa_sesion(monkeypatch, CrmSession())
        client = authenticated_client_factory(role="supervisor")

        response = await client.post(f"{URL}/{uuid.uuid4()}/merge/{uuid.uuid4()}")

        assert response.status_code == 403


# ─── GET /contacts/{id}/conversations ────────────────────────────────────────


class TestHistorial:
    """Historial de conversaciones de un contacto."""

    async def test_devuelve_las_conversaciones_del_contacto(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El historial pagina igual que el resto de listados."""
        contacto = FakeContact()
        conversaciones = [FakeConversation(contact_id=contacto.id) for _ in range(2)]
        _usa_sesion(
            monkeypatch,
            CrmSession(resultados=[contacto, conversaciones], escalares=[2]),
        )

        response = await authenticated_client.get(f"{URL}/{contacto.id}/conversations")

        assert response.status_code == 200
        assert response.json()["total"] == 2
        assert len(response.json()["items"]) == 2

    async def test_contacto_inexistente_es_404(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pedir el historial de un contacto que no existe responde 404."""
        _usa_sesion(monkeypatch, CrmSession(resultados=[None]))

        response = await authenticated_client.get(f"{URL}/{uuid.uuid4()}/conversations")

        assert response.status_code == 404


# ─── Autenticacion ───────────────────────────────────────────────────────────


class TestAutenticacion:
    """Todos los endpoints exigen token."""

    @pytest.mark.parametrize(
        ("metodo", "ruta"),
        [
            ("get", URL),
            ("post", URL),
            ("get", f"{URL}/{uuid.uuid4()}"),
            ("put", f"{URL}/{uuid.uuid4()}"),
            ("get", f"{URL}/{uuid.uuid4()}/conversations"),
        ],
    )
    async def test_sin_token_es_401(
        self, api_client: Any, monkeypatch: pytest.MonkeyPatch, metodo: str, ruta: str
    ) -> None:
        """Sin Authorization no se llega ni a abrir la sesion."""
        _usa_sesion(monkeypatch, CrmSession())

        peticion = getattr(api_client, metodo)
        response = await (peticion(ruta, json={}) if metodo in ("post", "put") else peticion(ruta))

        assert response.status_code == 401
