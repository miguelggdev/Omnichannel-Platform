"""Tests de /api/v1/clinical: configuracion y derechos del titular (Sprint 13, Dev B)."""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.api.v1 import clinical as modulo
from tests.unit.agent_doubles import FakeSession, parchear_tenant_session

BASE = "/api/v1/clinical"
PACIENTE = {"document_type": "CC", "document_number": "1234567890"}


def _config(clinica: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(config={"enabled_agents": ["rag", "clinical"], "clinical": clinica})


class TestConfiguracion:
    async def test_devuelve_los_profesionales(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        profesional = uuid.uuid4()
        parchear_tenant_session(
            monkeypatch,
            modulo,
            FakeSession(resultados=[_config({"professional_contact_ids": [str(profesional)]})]),
        )

        response = await authenticated_client.get(f"{BASE}/settings")

        assert response.status_code == 200
        assert response.json() == {"professional_contact_ids": [str(profesional)]}

    async def test_basura_escrita_a_mano_no_rompe_el_get(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        valido = uuid.uuid4()
        parchear_tenant_session(
            monkeypatch,
            modulo,
            FakeSession(
                resultados=[_config({"professional_contact_ids": ["no-es-uuid", str(valido)]})]
            ),
        )

        response = await authenticated_client.get(f"{BASE}/settings")

        assert response.json() == {"professional_contact_ids": [str(valido)]}

    @pytest.mark.parametrize("rol", ["agent", "supervisor"])
    @pytest.mark.parametrize(
        ("metodo", "ruta"),
        [
            ("get", "/settings"),
            ("put", "/settings"),
            ("post", "/consents"),
            ("post", "/consents/revoke"),
            ("post", "/patients/export"),
            ("post", "/patients/anonymize"),
        ],
    )
    async def test_solo_administradores(
        self, authenticated_client_factory: Any, rol: str, metodo: str, ruta: str
    ) -> None:
        """Son datos de salud: ni un agente ni un supervisor los administran."""
        cliente = authenticated_client_factory(role=rol)
        kwargs = {} if metodo == "get" else {"json": {**PACIENTE, "consent_type": "verbal"}}

        response = await getattr(cliente, metodo)(f"{BASE}{ruta}", **kwargs)

        assert response.status_code == 403

    async def test_autoriza_profesionales_vigentes_del_tenant(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        profesional = uuid.uuid4()
        sesion = parchear_tenant_session(
            monkeypatch,
            modulo,
            FakeSession(
                resultados=[
                    uuid.uuid4(),  # agent_config activa
                    [profesional],  # contactos vigentes
                    None,  # UPDATE
                    _config({"professional_contact_ids": [str(profesional)]}),
                ]
            ),
        )

        response = await authenticated_client.put(
            f"{BASE}/settings",
            json={"professional_contact_ids": [str(profesional), str(profesional)]},
        )

        assert response.status_code == 200, response.text
        assert response.json()["professional_contact_ids"] == [str(profesional)]
        consulta = str(sesion.executed[1]).lower()
        assert "contacts.client_id" in consulta
        assert "is_gdpr_deleted is false" in consulta
        # Concatenacion JSONB sobre `config.clinical`, no reescritura del config (BUG-022).
        assert "config -> 'clinical'" in str(sesion.executed[2])

    async def test_un_profesional_de_otro_tenant_da_400(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ajeno = uuid.uuid4()
        sesion = parchear_tenant_session(
            monkeypatch, modulo, FakeSession(resultados=[uuid.uuid4(), []])
        )

        response = await authenticated_client.put(
            f"{BASE}/settings", json={"professional_contact_ids": [str(ajeno)]}
        )

        assert response.status_code == 400
        assert len(sesion.executed) == 2, "no llego a escribir"

    async def test_sin_agente_activo_da_400(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parchear_tenant_session(monkeypatch, modulo, FakeSession(resultados=[None]))

        response = await authenticated_client.put(
            f"{BASE}/settings", json={"professional_contact_ids": []}
        )

        assert response.status_code == 400

    async def test_uuid_invalido_da_422(self, authenticated_client: Any) -> None:
        response = await authenticated_client.put(
            f"{BASE}/settings", json={"professional_contact_ids": ["no-es-uuid"]}
        )

        assert response.status_code == 422


class TestDerechosDelTitular:
    async def test_registrar_consentimiento(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sesion = parchear_tenant_session(monkeypatch, modulo, FakeSession(resultados=[None]))

        response = await authenticated_client.post(
            f"{BASE}/consents", json={**PACIENTE, "consent_type": "written"}
        )

        assert response.status_code == 200, response.text
        assert response.json()["registered"] is True
        (consentimiento,) = sesion.added
        assert consentimiento.registered_by_user_id is not None
        assert consentimiento.consent_type == "written"

    async def test_tipo_de_consentimiento_invalido_da_422(self, authenticated_client: Any) -> None:
        response = await authenticated_client.post(
            f"{BASE}/consents", json={**PACIENTE, "consent_type": "telepatico"}
        )

        assert response.status_code == 422

    async def test_documento_invalido_da_400_sin_eco_del_documento(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parchear_tenant_session(monkeypatch, modulo, FakeSession())

        response = await authenticated_client.post(
            f"{BASE}/patients/export", json={"document_type": "ZZ", "document_number": "9999999"}
        )

        assert response.status_code == 400
        assert "9999999" not in response.text

    async def test_exportar(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parchear_tenant_session(monkeypatch, modulo, FakeSession(resultados=[[], []]))

        response = await authenticated_client.post(f"{BASE}/patients/export", json=PACIENTE)

        assert response.status_code == 200, response.text
        cuerpo = response.json()
        assert cuerpo["clinical_records"] == []
        assert "Ley 1581" in cuerpo["legal_basis"]

    async def test_el_documento_va_en_el_cuerpo_no_en_la_url(
        self, authenticated_client: Any
    ) -> None:
        """Las URLs quedan en los logs de acceso; el documento es dato personal."""
        response = await authenticated_client.get(f"{BASE}/patients/1234567890/export")

        assert response.status_code in (404, 405)

    async def test_anonimizar(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parchear_tenant_session(monkeypatch, modulo, FakeSession(resultados=[[]]))

        response = await authenticated_client.post(f"{BASE}/patients/anonymize", json=PACIENTE)

        assert response.status_code == 200, response.text
        assert response.json()["records_anonymized"] == 0

    async def test_revocar(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parchear_tenant_session(monkeypatch, modulo, FakeSession(resultados=[None]))

        response = await authenticated_client.post(f"{BASE}/consents/revoke", json=PACIENTE)

        assert response.status_code == 200
        assert response.json()["revoked"] is False


class TestRolMedical:
    """El rol `medical` (migracion 020, ADR-073) separa el acto clinico del administrativo.

    Antes de este rol, firmar un registro clinico exigia ser administrador del
    tenant: la decision de producto que ADR-072 dejo abierta y el criterio 14
    del spec pedia cerrar.
    """

    #: Actos clinicos: leer la historia, firmarla y dejar constancia de la
    #: autorizacion que el paciente otorgo en la consulta.
    CLINICOS: tuple[tuple[str, str], ...] = (
        ("get", "/records"),
        ("post", "/consents"),
        ("post", f"/records/{uuid.uuid4()}/review"),
        ("post", f"/records/{uuid.uuid4()}/sign"),
        ("post", f"/records/{uuid.uuid4()}/submit"),
    )

    #: Actos del responsable del tratamiento, no del profesional.
    ADMINISTRATIVOS: tuple[tuple[str, str], ...] = (
        ("get", "/settings"),
        ("put", "/settings"),
        ("post", "/consents/revoke"),
        ("post", "/patients/export"),
        ("post", "/patients/anonymize"),
    )

    @staticmethod
    def _kwargs(metodo: str) -> dict[str, Any]:
        """Cuerpo minimo que aceptan los schemas de cada verbo."""
        if metodo == "get":
            return {}
        return {"json": {**PACIENTE, "consent_type": "verbal"}}

    @pytest.mark.parametrize(("metodo", "ruta"), CLINICOS)
    async def test_un_medico_llega_a_los_actos_clinicos(
        self,
        authenticated_client_factory: Any,
        monkeypatch: pytest.MonkeyPatch,
        metodo: str,
        ruta: str,
    ) -> None:
        """No se comprueba el resultado, solo que el RBAC no lo corta con 403."""
        parchear_tenant_session(monkeypatch, modulo, FakeSession(resultados=[None] * 6))
        cliente = authenticated_client_factory(role="medical")

        response = await getattr(cliente, metodo)(f"{BASE}{ruta}", **self._kwargs(metodo))

        assert response.status_code != 403, response.text

    @pytest.mark.parametrize(("metodo", "ruta"), ADMINISTRATIVOS)
    async def test_un_medico_no_administra_ni_ejerce_los_derechos_del_titular(
        self, authenticated_client_factory: Any, metodo: str, ruta: str
    ) -> None:
        """Decidir quien dicta, exportar y suprimir son actos del responsable.

        Un profesional no decide quien mas puede dictar en el tenant, ni borra
        la historia clinica de un paciente.
        """
        cliente = authenticated_client_factory(role="medical")

        response = await getattr(cliente, metodo)(f"{BASE}{ruta}", **self._kwargs(metodo))

        assert response.status_code == 403

    @pytest.mark.parametrize("rol", ["agent", "supervisor"])
    @pytest.mark.parametrize(("metodo", "ruta"), CLINICOS)
    async def test_atencion_al_cliente_sigue_fuera_de_todo(
        self, authenticated_client_factory: Any, rol: str, metodo: str, ruta: str
    ) -> None:
        """Un dato de salud es categoria especial (Ley 1581, art. 5)."""
        cliente = authenticated_client_factory(role=rol)

        response = await getattr(cliente, metodo)(f"{BASE}{ruta}", **self._kwargs(metodo))

        assert response.status_code == 403

    @pytest.mark.parametrize(("metodo", "ruta"), CLINICOS + ADMINISTRATIVOS)
    async def test_un_admin_sigue_llegando_a_todo(
        self,
        authenticated_client_factory: Any,
        monkeypatch: pytest.MonkeyPatch,
        metodo: str,
        ruta: str,
    ) -> None:
        """El rol nuevo no le quita permisos a nadie."""
        parchear_tenant_session(monkeypatch, modulo, FakeSession(resultados=[None] * 6))
        cliente = authenticated_client_factory(role="admin")

        response = await getattr(cliente, metodo)(f"{BASE}{ruta}", **self._kwargs(metodo))

        assert response.status_code != 403, response.text

    def test_el_enum_de_usuario_conoce_el_rol(self) -> None:
        """Sin el valor en el enum, crear el usuario falla en la base (migracion 020)."""
        from app.models.user import User

        assert "medical" in User.__table__.c.role.type.enums
