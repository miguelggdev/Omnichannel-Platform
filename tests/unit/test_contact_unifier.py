"""Tests de `app/services/contact_unifier.py::ContactUnifier.merge()`.

El endpoint (`app/api/v1/contacts.py::merge_contacts()`) ya valida que
`source_id != target_id`, que ambos contactos existen y que ninguno está ya
fusionado antes de llamar a `merge()`; estos tests se concentran en lo que
`merge()` hace: mover filas y evitar etiquetas duplicadas.
"""

import uuid

import pytest

from app.services.contact_unifier import ContactNotFoundError, ContactUnifier
from tests.unit.crm_doubles import CrmSession, FakeContact


class _FakeContactTag:
    """Sustituto mínimo de `ContactTag`: mutable, como el real."""

    def __init__(self, tag_id: uuid.UUID, contact_id: uuid.UUID) -> None:
        self.tag_id = tag_id
        self.contact_id = contact_id


class TestMerge:
    """`ContactUnifier.merge()` mueve identificadores, conversaciones, notas y tags."""

    async def test_mueve_identificadores_conversaciones_y_notas(self) -> None:
        """Las tres tablas simples se mueven con un UPDATE masivo cada una."""
        source_id, target_id = uuid.uuid4(), uuid.uuid4()
        source = FakeContact(id=source_id)
        sesion = CrmSession(
            resultados=[None, None, None, [], []],  # 3 updates + 2 selects de tags
            objetos={source_id: source},
        )

        await ContactUnifier(sesion).merge(source_id=source_id, target_id=target_id)

        assert len(sesion.executed) == 5

    async def test_marca_el_origen_como_fusionado(self) -> None:
        """`source.merged_into_id` queda apuntando al destino."""
        source_id, target_id = uuid.uuid4(), uuid.uuid4()
        source = FakeContact(id=source_id)
        sesion = CrmSession(resultados=[None, None, None, [], []], objetos={source_id: source})

        await ContactUnifier(sesion).merge(source_id=source_id, target_id=target_id)

        assert source.merged_into_id == target_id

    async def test_origen_inexistente_lanza_contactnotfound(self) -> None:
        """Si el contacto origen desapareció entre la validación y esta llamada."""
        source_id, target_id = uuid.uuid4(), uuid.uuid4()
        sesion = CrmSession(resultados=[None, None, None, [], []], objetos={})

        with pytest.raises(ContactNotFoundError):
            await ContactUnifier(sesion).merge(source_id=source_id, target_id=target_id)

    async def test_etiqueta_duplicada_se_borra_no_se_mueve(self) -> None:
        """El destino ya tiene la etiqueta: la del origen se elimina, no se reasigna."""
        source_id, target_id, tag_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        source = FakeContact(id=source_id)
        etiqueta_origen = _FakeContactTag(tag_id=tag_id, contact_id=source_id)
        sesion = CrmSession(
            resultados=[None, None, None, [tag_id], [etiqueta_origen]],
            objetos={source_id: source},
        )

        await ContactUnifier(sesion).merge(source_id=source_id, target_id=target_id)

        assert etiqueta_origen in sesion.deleted
        assert etiqueta_origen.contact_id == source_id  # no se tocó: se borró en vez de moverse

    async def test_etiqueta_no_duplicada_se_reasigna_al_destino(self) -> None:
        """Sin conflicto, la etiqueta del origen pasa a pertenecer al destino."""
        source_id, target_id, tag_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        source = FakeContact(id=source_id)
        etiqueta_origen = _FakeContactTag(tag_id=tag_id, contact_id=source_id)
        sesion = CrmSession(
            resultados=[None, None, None, [], [etiqueta_origen]],  # destino sin tags
            objetos={source_id: source},
        )

        await ContactUnifier(sesion).merge(source_id=source_id, target_id=target_id)

        assert etiqueta_origen not in sesion.deleted
        assert etiqueta_origen.contact_id == target_id

    async def test_mezcla_de_etiquetas_duplicadas_y_nuevas(self) -> None:
        """Con varias etiquetas, cada una sigue su propio camino (borrar o mover)."""
        source_id, target_id = uuid.uuid4(), uuid.uuid4()
        tag_duplicado, tag_nuevo = uuid.uuid4(), uuid.uuid4()
        source = FakeContact(id=source_id)
        et_duplicada = _FakeContactTag(tag_id=tag_duplicado, contact_id=source_id)
        et_nueva = _FakeContactTag(tag_id=tag_nuevo, contact_id=source_id)
        sesion = CrmSession(
            resultados=[None, None, None, [tag_duplicado], [et_duplicada, et_nueva]],
            objetos={source_id: source},
        )

        await ContactUnifier(sesion).merge(source_id=source_id, target_id=target_id)

        assert et_duplicada in sesion.deleted
        assert et_nueva not in sesion.deleted
        assert et_nueva.contact_id == target_id
