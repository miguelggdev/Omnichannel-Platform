"""Tests de las etiquetas del tenant y de las notas internas de un contacto.

La base se sustituye por `CrmSession`: corren sin `--run-db`.
"""

import uuid
from typing import Any

import pytest

from app.api.v1 import notes as notes_module
from app.api.v1 import tags as tags_module
from tests.unit.agent_doubles import fake_tenant_session
from tests.unit.crm_doubles import CrmSession, FakeContact, FakeNote, FakeTag

TAGS_URL = "/api/v1/tags"
CONTACTS_URL = "/api/v1/contacts"


def _usa_sesion_tags(monkeypatch: pytest.MonkeyPatch, session: CrmSession) -> CrmSession:
    """Hace que los endpoints de etiquetas usen la sesion falsa indicada."""
    monkeypatch.setattr(tags_module, "tenant_session", fake_tenant_session(session))
    return session


def _usa_sesion_notas(monkeypatch: pytest.MonkeyPatch, session: CrmSession) -> CrmSession:
    """Hace que los endpoints de notas usen la sesion falsa indicada."""
    monkeypatch.setattr(notes_module, "tenant_session", fake_tenant_session(session))
    return session


# ─── Etiquetas del tenant ────────────────────────────────────────────────────


class TestListadoDeTags:
    """GET /tags."""

    async def test_devuelve_las_etiquetas_ordenadas(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La lista sale alfabetica y sin paginar."""
        session = _usa_sesion_tags(
            monkeypatch,
            CrmSession(resultados=[[FakeTag(name="alta"), FakeTag(name="vip")]]),
        )

        response = await authenticated_client.get(TAGS_URL)

        assert response.status_code == 200
        assert [t["name"] for t in response.json()] == ["alta", "vip"]
        assert "order by tags.name asc" in str(session.executed[0]).lower()


class TestAltaDeTags:
    """POST /tags."""

    async def test_crea_la_etiqueta(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un alta valida responde 201."""
        session = _usa_sesion_tags(monkeypatch, CrmSession(resultados=[None]))

        response = await authenticated_client.post(
            TAGS_URL, json={"name": "vip", "color": "#FF5733"}
        )

        assert response.status_code == 201
        assert response.json()["name"] == "vip"
        assert len(session.added) == 1

    async def test_nombre_repetido_es_409(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El nombre es unico por tenant: se avisa con un 409 legible."""
        _usa_sesion_tags(monkeypatch, CrmSession(resultados=[FakeTag(name="vip")]))

        response = await authenticated_client.post(TAGS_URL, json={"name": "vip"})

        assert response.status_code == 409
        assert response.json()["error_code"] == "DUPLICATE"

    @pytest.mark.parametrize("color", ["rojo", "#FFF", "#GGGGGG", "FF5733"])
    async def test_color_invalido_es_422(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch, color: str
    ) -> None:
        """El color tiene que ser hex de 6 digitos con almohadilla."""
        _usa_sesion_tags(monkeypatch, CrmSession())

        response = await authenticated_client.post(TAGS_URL, json={"name": "x", "color": color})

        assert response.status_code == 422

    async def test_nombre_vacio_es_422(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Una etiqueta sin nombre no sirve para nada."""
        _usa_sesion_tags(monkeypatch, CrmSession())

        response = await authenticated_client.post(TAGS_URL, json={"name": ""})

        assert response.status_code == 422


class TestBorradoDeTags:
    """DELETE /tags/{id}."""

    async def test_borra_primero_las_asignaciones(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sin CASCADE en la FK, hay que limpiar contact_tags o el DELETE revienta."""
        tag = FakeTag()
        # rowcounts se consume en orden: 0 para el SELECT del tag, 3 para el DELETE.
        session = _usa_sesion_tags(monkeypatch, CrmSession(resultados=[tag], rowcounts=[0, 3]))

        response = await authenticated_client.delete(f"{TAGS_URL}/{tag.id}")

        assert response.status_code == 200
        assert response.json()["assignments_removed"] == 3
        assert "delete from contact_tags" in str(session.executed[-1]).lower()
        assert session.deleted == [tag]

    async def test_etiqueta_inexistente_es_404(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Borrar lo que no existe responde 404."""
        _usa_sesion_tags(monkeypatch, CrmSession(resultados=[None]))

        response = await authenticated_client.delete(f"{TAGS_URL}/{uuid.uuid4()}")

        assert response.status_code == 404

    async def test_el_rol_agent_no_puede_borrar(
        self, authenticated_client_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Borrar una etiqueta la quita de todos los contactos: solo admin."""
        _usa_sesion_tags(monkeypatch, CrmSession())
        client = authenticated_client_factory(role="agent")

        response = await client.delete(f"{TAGS_URL}/{uuid.uuid4()}")

        assert response.status_code == 403


# ─── Etiquetas de un contacto ────────────────────────────────────────────────


class TestAsignacionDeTags:
    """POST y DELETE /contacts/{id}/tags/{tag_id}."""

    async def test_asigna_la_etiqueta(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Contacto y etiqueta existen y no estaban relacionados: 201."""
        contacto = FakeContact()
        tag = FakeTag()
        session = _usa_sesion_tags(monkeypatch, CrmSession(resultados=[contacto, tag, None]))

        response = await authenticated_client.post(f"{CONTACTS_URL}/{contacto.id}/tags/{tag.id}")

        assert response.status_code == 201
        assert len(session.added) == 1

    async def test_contacto_inexistente_es_404(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No se etiqueta un contacto que no existe."""
        _usa_sesion_tags(monkeypatch, CrmSession(resultados=[None]))

        response = await authenticated_client.post(
            f"{CONTACTS_URL}/{uuid.uuid4()}/tags/{uuid.uuid4()}"
        )

        assert response.status_code == 404

    async def test_etiqueta_inexistente_es_404(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tampoco se asigna una etiqueta que no existe en este tenant."""
        _usa_sesion_tags(monkeypatch, CrmSession(resultados=[FakeContact(), None]))

        response = await authenticated_client.post(
            f"{CONTACTS_URL}/{uuid.uuid4()}/tags/{uuid.uuid4()}"
        )

        assert response.status_code == 404

    async def test_asignar_dos_veces_es_409(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La relacion es unica: repetirla avisa con 409, no con un error de integridad."""
        relacion = object()
        _usa_sesion_tags(monkeypatch, CrmSession(resultados=[FakeContact(), FakeTag(), relacion]))

        response = await authenticated_client.post(
            f"{CONTACTS_URL}/{uuid.uuid4()}/tags/{uuid.uuid4()}"
        )

        assert response.status_code == 409

    async def test_quitar_la_etiqueta(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Quitar una etiqueta asignada borra solo esa relacion."""
        relacion = object()
        session = _usa_sesion_tags(monkeypatch, CrmSession(resultados=[relacion]))

        response = await authenticated_client.delete(
            f"{CONTACTS_URL}/{uuid.uuid4()}/tags/{uuid.uuid4()}"
        )

        assert response.status_code == 200
        assert session.deleted == [relacion]

    async def test_quitar_una_que_no_estaba_es_404(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Si el contacto no tenia esa etiqueta, no hay nada que quitar."""
        _usa_sesion_tags(monkeypatch, CrmSession(resultados=[None]))

        response = await authenticated_client.delete(
            f"{CONTACTS_URL}/{uuid.uuid4()}/tags/{uuid.uuid4()}"
        )

        assert response.status_code == 404


# ─── Notas internas ──────────────────────────────────────────────────────────


class TestNotas:
    """GET y POST /contacts/{id}/notes."""

    async def test_lista_las_notas_paginadas(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El listado trae la pagina, el total y el numero de paginas."""
        contacto = FakeContact()
        notas = [FakeNote(content="una"), FakeNote(content="otra")]
        _usa_sesion_notas(monkeypatch, CrmSession(resultados=[contacto, notas], escalares=[5]))

        response = await authenticated_client.get(f"{CONTACTS_URL}/{contacto.id}/notes")

        assert response.status_code == 200
        cuerpo = response.json()
        assert cuerpo["total"] == 5
        assert cuerpo["total_pages"] == 1
        assert [n["content"] for n in cuerpo["items"]] == ["una", "otra"]

    async def test_las_notas_salen_de_la_mas_nueva_a_la_mas_vieja(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lo ultimo que se anoto sobre el contacto es lo primero que se lee."""
        contacto = FakeContact()
        session = _usa_sesion_notas(
            monkeypatch, CrmSession(resultados=[contacto, []], escalares=[0])
        )

        await authenticated_client.get(f"{CONTACTS_URL}/{contacto.id}/notes")

        assert "order by internal_notes.created_at desc" in str(session.executed[-1]).lower()

    async def test_crea_la_nota_firmada_por_el_usuario_del_token(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El autor sale del JWT: nadie firma una nota a nombre de otro."""
        contacto = FakeContact()
        session = _usa_sesion_notas(monkeypatch, CrmSession(resultados=[contacto]))
        impostor = uuid.uuid4()

        response = await authenticated_client.post(
            f"{CONTACTS_URL}/{contacto.id}/notes",
            json={"content": "llamar el lunes", "author_id": str(impostor)},
        )

        assert response.status_code == 201
        nota = session.added[0]
        assert nota.content == "llamar el lunes"
        assert nota.author_id is not None
        assert nota.author_id != impostor, "el author_id del body no debe llegar a la fila"

    async def test_nota_vacia_es_422(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Una nota sin contenido no se guarda."""
        _usa_sesion_notas(monkeypatch, CrmSession())

        response = await authenticated_client.post(
            f"{CONTACTS_URL}/{uuid.uuid4()}/notes", json={"content": ""}
        )

        assert response.status_code == 422

    async def test_contacto_inexistente_es_404(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No se anota sobre un contacto que no existe."""
        _usa_sesion_notas(monkeypatch, CrmSession(resultados=[None]))

        response = await authenticated_client.post(
            f"{CONTACTS_URL}/{uuid.uuid4()}/notes", json={"content": "hola"}
        )

        assert response.status_code == 404

    async def test_sin_token_es_401(self, api_client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """Las notas internas no se leen sin identificarse."""
        _usa_sesion_notas(monkeypatch, CrmSession())

        response = await api_client.get(f"{CONTACTS_URL}/{uuid.uuid4()}/notes")

        assert response.status_code == 401
