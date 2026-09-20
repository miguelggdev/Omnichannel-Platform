"""Tests de `app/services/contact_unifier.py::ContactUnifier.merge()`.

El endpoint (`app/api/v1/contacts.py::merge_contacts()`) ya valida que
`source_id != target_id`, que ambos contactos existen y que ninguno está ya
fusionado antes de llamar a `merge()`; estos tests se concentran en lo que
`merge()` hace: mover filas y evitar etiquetas duplicadas.

`merge()` busca origen y destino con `select()` filtrado por `client_id` (no con
`session.get()`), así que en `CrmSession` salen de `resultados`: los dos primeros
`execute()` son esas búsquedas, después los 3 UPDATE y los 2 SELECT de etiquetas.
"""

import uuid

import pytest

from app.services.contact_unifier import ContactNotFoundError, ContactUnifier
from tests.unit.crm_doubles import CrmSession, FakeContact

CLIENT_ID = uuid.uuid4()


class _FakeContactTag:
    """Sustituto mínimo de `ContactTag`: mutable, como el real."""

    def __init__(self, tag_id: uuid.UUID, contact_id: uuid.UUID) -> None:
        self.tag_id = tag_id
        self.contact_id = contact_id


class TestMerge:
    """`ContactUnifier.merge()` mueve identificadores, conversaciones, notas y tags."""

    @staticmethod
    def _sesion(source: FakeContact, target: FakeContact, tags: list[object]) -> CrmSession:
        """Sesion falsa con el orden real de `execute()` dentro de `merge()`.

        Args:
            source: Contacto origen que devuelve la busqueda del tenant.
            target: Contacto destino que devuelve la busqueda del tenant.
            tags: Los dos resultados de las consultas de etiquetas
                (`[tag_ids_del_destino, etiquetas_del_origen]`).

        Returns:
            La sesion lista para pasarla a `ContactUnifier`.
        """
        return CrmSession(resultados=[source, target, None, None, None, *tags])

    async def test_mueve_identificadores_conversaciones_y_notas(self) -> None:
        """Las tres tablas simples se mueven con un UPDATE masivo cada una."""
        source, target = FakeContact(client_id=CLIENT_ID), FakeContact(client_id=CLIENT_ID)
        sesion = self._sesion(source, target, [[], []])

        await ContactUnifier(sesion).merge(source.id, target.id, CLIENT_ID)

        # 2 busquedas (origen y destino) + 3 updates + 2 selects de etiquetas.
        assert len(sesion.executed) == 7

    async def test_marca_el_origen_como_fusionado(self) -> None:
        """`source.merged_into_id` queda apuntando al destino."""
        source, target = FakeContact(client_id=CLIENT_ID), FakeContact(client_id=CLIENT_ID)
        sesion = self._sesion(source, target, [[], []])

        await ContactUnifier(sesion).merge(source.id, target.id, CLIENT_ID)

        assert source.merged_into_id == target.id

    async def test_origen_inexistente_lanza_contactnotfound(self) -> None:
        """Si el contacto origen desapareció entre la validación y esta llamada."""
        sesion = CrmSession(resultados=[None])

        with pytest.raises(ContactNotFoundError, match="origen"):
            await ContactUnifier(sesion).merge(uuid.uuid4(), uuid.uuid4(), CLIENT_ID)

    async def test_destino_inexistente_no_toca_nada(self) -> None:
        """Un destino que no es del tenant aborta antes de mover ninguna fila.

        Sin esta comprobacion, `merged_into_id` podria apuntar a un contacto de
        otro tenant si algun llamador se saltara la validacion del endpoint.
        """
        source = FakeContact(client_id=CLIENT_ID)
        sesion = CrmSession(resultados=[source, None])

        with pytest.raises(ContactNotFoundError, match="destino"):
            await ContactUnifier(sesion).merge(source.id, uuid.uuid4(), CLIENT_ID)

        assert len(sesion.executed) == 2, "no debe haber ningun UPDATE tras el fallo"
        assert source.merged_into_id is None

    async def test_las_busquedas_filtran_por_el_client_id_recibido(self) -> None:
        """El tenant lo dicta el llamador autenticado, no la fila encontrada."""
        source, target = FakeContact(client_id=CLIENT_ID), FakeContact(client_id=CLIENT_ID)
        sesion = self._sesion(source, target, [[], []])

        await ContactUnifier(sesion).merge(source.id, target.id, CLIENT_ID)

        for busqueda in sesion.executed[:2]:
            sql = str(busqueda.compile())
            assert "contacts.client_id =" in sql
            assert busqueda.compile().params.get("client_id_1") == CLIENT_ID

    async def test_etiqueta_duplicada_se_borra_no_se_mueve(self) -> None:
        """El destino ya tiene la etiqueta: la del origen se elimina, no se reasigna."""
        tag_id = uuid.uuid4()
        source, target = FakeContact(client_id=CLIENT_ID), FakeContact(client_id=CLIENT_ID)
        etiqueta_origen = _FakeContactTag(tag_id=tag_id, contact_id=source.id)
        sesion = self._sesion(source, target, [[tag_id], [etiqueta_origen]])

        await ContactUnifier(sesion).merge(source.id, target.id, CLIENT_ID)

        assert etiqueta_origen in sesion.deleted
        assert etiqueta_origen.contact_id == source.id  # no se tocó: se borró en vez de moverse

    async def test_etiqueta_no_duplicada_se_reasigna_al_destino(self) -> None:
        """Sin conflicto, la etiqueta del origen pasa a pertenecer al destino."""
        source, target = FakeContact(client_id=CLIENT_ID), FakeContact(client_id=CLIENT_ID)
        etiqueta_origen = _FakeContactTag(tag_id=uuid.uuid4(), contact_id=source.id)
        sesion = self._sesion(source, target, [[], [etiqueta_origen]])  # destino sin tags

        await ContactUnifier(sesion).merge(source.id, target.id, CLIENT_ID)

        assert etiqueta_origen not in sesion.deleted
        assert etiqueta_origen.contact_id == target.id

    async def test_mezcla_de_etiquetas_duplicadas_y_nuevas(self) -> None:
        """Con varias etiquetas, cada una sigue su propio camino (borrar o mover)."""
        tag_duplicado, tag_nuevo = uuid.uuid4(), uuid.uuid4()
        source, target = FakeContact(client_id=CLIENT_ID), FakeContact(client_id=CLIENT_ID)
        et_duplicada = _FakeContactTag(tag_id=tag_duplicado, contact_id=source.id)
        et_nueva = _FakeContactTag(tag_id=tag_nuevo, contact_id=source.id)
        sesion = self._sesion(source, target, [[tag_duplicado], [et_duplicada, et_nueva]])

        await ContactUnifier(sesion).merge(source.id, target.id, CLIENT_ID)

        assert et_duplicada in sesion.deleted
        assert et_nueva not in sesion.deleted
        assert et_nueva.contact_id == target.id
