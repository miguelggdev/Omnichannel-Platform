"""Pendientes del Sprint 13 (Dev B): catalogo oficial, firma, retencion y privacidad."""

import os
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-testing-only-minimum-32-chars")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key-minimum-32-characters-long")

from app.agents.middleware import logging_middleware as lm
from app.agents.nodes import _delivery
from app.agents.nodes import clinical as nodo
from app.agents.nodes import respond as respond_mod
from app.api.v1 import clinical as api
from app.core.encryption import EncryptedJSON
from app.models.clinical_record import (
    RECORD_TRANSITIONS,
    RETENTION_YEARS,
    ClinicalRecord,
    fin_de_retencion,
)
from app.services import clinical as svc
from app.services import clinical_catalog as cat
from app.tasks import ai_processor as tarea
from tests.unit.agent_doubles import FakeSession, estado, parchear_tenant_session

CLIENT_ID = uuid.uuid4()
RECORD_ID = uuid.uuid4()
USER_ID = uuid.uuid4()


class TestRetencion:
    def test_son_20_anos(self) -> None:
        """Resolucion 839 de 2017: 5 en gestion + 15 en el archivo central."""
        assert RETENTION_YEARS == 20

    def test_fin_de_retencion(self) -> None:
        assert fin_de_retencion(date(2025, 1, 15)) == date(2045, 1, 15)

    def test_29_de_febrero(self) -> None:
        assert fin_de_retencion(date(2024, 2, 29)) == date(2044, 2, 29)
        assert fin_de_retencion(date(2004, 2, 29)) == date(2024, 2, 29)

    def test_el_estado_solo_avanza_en_orden(self) -> None:
        assert RECORD_TRANSITIONS == {
            "draft": "reviewed",
            "reviewed": "signed",
            "signed": "submitted",
        }


class TestCatalogoOficial:
    async def test_sin_cargar_usa_el_subconjunto(self) -> None:
        resultado = await cat.buscar_en_catalogo(FakeSession(resultados=[None]), "cie10", "I10")  # type: ignore[arg-type]

        assert resultado[0]["code"] == "I10"

    async def test_cargado_manda_la_base(self) -> None:
        # existe alguna fila, codigo exacto
        sesion = FakeSession(resultados=["I10", None])

        await cat.buscar_en_catalogo(sesion, "cie10", "i10")  # type: ignore[arg-type]

        assert "cie10_catalog" in str(sesion.executed[1])

    async def test_las_palabras_van_escapadas(self) -> None:
        assert cat._escapar_like("50%_x\\") == "50\\%\\_x\\\\"

    async def test_verificar_codigos(self) -> None:
        sesion = FakeSession(resultados=["X", [("I10", "Hipertension")]])

        cargado, encontrados = await cat.verificar_codigos(sesion, "cie10", ["I10", "Z99"])  # type: ignore[arg-type]

        assert cargado is True
        assert encontrados == {"I10": "Hipertension"}

    async def test_verificar_sin_catalogo(self) -> None:
        assert await cat.verificar_codigos(FakeSession(resultados=[None]), "cups", ["890201"]) == (  # type: ignore[arg-type]
            False,
            {},
        )

    async def test_un_codigo_que_no_existe_en_el_catalogo_oficial_se_rechaza(self) -> None:
        """Con el catalogo cargado, un codigo inexistente es un error de dictado."""
        datos = svc.validar_registro_rips(
            rips_type="AC",
            service_type="consulta",
            service_date="2025-01-15",
            diagnosis_codes=[{"code": "Z99.9", "type": "principal"}],
            procedure_codes=None,
            purpose_code="05",
            external_cause=None,
            diagnosis_type=None,
            notes=None,
        )
        sesion = FakeSession(resultados=["X", []])  # cargado, pero Z99.9 no esta

        with pytest.raises(svc.ClinicalValidationError, match="catalogo oficial CIE10"):
            await svc._aplicar_catalogo_oficial(sesion, datos)  # type: ignore[arg-type]

    async def test_un_codigo_oficial_queda_verificado_y_hereda_la_descripcion(self) -> None:
        datos = svc.validar_registro_rips(
            rips_type="AC",
            service_type="consulta",
            service_date="2025-01-15",
            diagnosis_codes=[{"code": "S72.00", "type": "principal"}],
            procedure_codes=None,
            purpose_code="05",
            external_cause=None,
            diagnosis_type=None,
            notes=None,
        )
        assert datos["diagnosis_codes"][0]["catalog_verified"] is False

        await svc._aplicar_catalogo_oficial(
            FakeSession(resultados=["X", [("S72.00", "Fractura del cuello del femur")]]),  # type: ignore[arg-type]
            datos,
        )

        assert datos["diagnosis_codes"][0]["catalog_verified"] is True
        assert datos["diagnosis_codes"][0]["description"] == "Fractura del cuello del femur"

    async def test_cargar_descarta_lo_invalido_y_no_repite(self) -> None:
        sesion = FakeSession()

        resumen = await cat.cargar_catalogo(
            sesion,  # type: ignore[arg-type]
            "cie10",
            [("J06.9", "Infeccion"), ("j06.9", "Repetida"), ("XX", "malo"), ("I10", " ")],
        )

        assert resumen == {"loaded": 1, "invalid": 2}
        assert len(sesion.executed) == 1

    async def test_cargar_es_un_upsert(self) -> None:
        sesion = FakeSession()

        await cat.cargar_catalogo(sesion, "cups", [("890201", "Consulta")])  # type: ignore[arg-type]

        sql = str(sesion.executed[0].compile(dialect=postgresql.dialect())).lower()
        assert "on conflict (code) do update" in sql


class TestEncryptedJSON:
    def test_cifra_al_escribir_y_descifra_al_leer(self) -> None:
        tabla = ClinicalRecord.__table__
        insercion = str(
            tabla.insert()
            .values(structured_notes={"plan": "x"})
            .compile(dialect=postgresql.dialect())
        )
        seleccion = str(
            select(ClinicalRecord.structured_notes).compile(dialect=postgresql.dialect())
        )

        assert "pgp_sym_encrypt" in insercion
        assert "pgp_sym_decrypt" in seleccion

    def test_ida_y_vuelta_del_json(self) -> None:
        tipo = EncryptedJSON()

        assert tipo.process_result_value('{"plan": "café"}', None) == {"plan": "café"}
        assert tipo.process_result_value(None, None) is None

    def test_las_notas_ya_no_van_en_jsonb_en_claro(self) -> None:
        assert isinstance(ClinicalRecord.__table__.c.structured_notes.type, EncryptedJSON)
        assert isinstance(ClinicalRecord.__table__.c.medical_entities.type, EncryptedJSON)


def _registro(estado_: str = "draft", **over: Any) -> SimpleNamespace:
    base = {
        "id": RECORD_ID,
        "status": estado_,
        "patient_document_type": "CC",
        "patient_document_number": "1234567890",
        "patient_document_hash": "h",
        "patient_name": "Ana",
        "service_date": date(2025, 1, 15),
        "service_type": "consulta",
        "specialty": None,
        "rips_type": "AC",
        "purpose_code": "05",
        "external_cause": None,
        "diagnosis_type": None,
        "diagnosis_codes": [{"code": "I10", "type": "principal"}],
        "procedure_codes": [],
        "structured_notes": {"plan": "reposo"},
        "dictated_by_contact_id": None,
        "reviewed_by": None,
        "signed_at": None,
        "anonymized_at": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


class TestFirma:
    async def test_revisar_un_borrador(self) -> None:
        # UPDATE..RETURNING, SELECT del registro, MAX(service_date)
        sesion = FakeSession(resultados=[RECORD_ID, _registro("reviewed"), date(2025, 1, 15)])

        detalle = await svc.avanzar_registro(
            sesion,  # type: ignore[arg-type]
            client_id=CLIENT_ID,
            record_id=RECORD_ID,
            destino="reviewed",
            user_id=USER_ID,
        )

        assert detalle["status"] == "reviewed"
        assert detalle["retention_until"] == "2045-01-15"
        # El documento va enmascarado en la vista de revision.
        assert detalle["patient_document"] == "******7890"
        sql = str(sesion.executed[0]).lower()
        assert "clinical_records.status" in sql  # el origen entra en el WHERE (atomico)
        assert "returning" in sql

    async def test_no_se_puede_saltar_un_estado(self) -> None:
        with pytest.raises(svc.TransicionInvalidaError, match="destino invalido"):
            await svc.avanzar_registro(
                FakeSession(),  # type: ignore[arg-type]
                client_id=CLIENT_ID,
                record_id=RECORD_ID,
                destino="draft",
                user_id=USER_ID,
            )

    async def test_firmar_algo_que_no_esta_revisado_da_conflicto(self) -> None:
        # UPDATE sin filas; el registro existe en 'draft'
        sesion = FakeSession(resultados=[None, "draft"])

        with pytest.raises(svc.TransicionInvalidaError, match="tiene que estar en 'reviewed'"):
            await svc.avanzar_registro(
                sesion,  # type: ignore[arg-type]
                client_id=CLIENT_ID,
                record_id=RECORD_ID,
                destino="signed",
                user_id=USER_ID,
            )

    async def test_registro_inexistente(self) -> None:
        with pytest.raises(svc.RegistroNoEncontradoError):
            await svc.avanzar_registro(
                FakeSession(resultados=[None, None]),  # type: ignore[arg-type]
                client_id=CLIENT_ID,
                record_id=RECORD_ID,
                destino="reviewed",
                user_id=USER_ID,
            )

    async def test_listar_filtra_por_estado_valido(self) -> None:
        with pytest.raises(svc.ClinicalValidationError, match="Estado invalido"):
            await svc.listar_registros(FakeSession(), client_id=CLIENT_ID, estado="inventado")  # type: ignore[arg-type]

    async def test_el_listado_no_lleva_notas_ni_documento(self) -> None:
        filas = await svc.listar_registros(
            FakeSession(resultados=[[_registro()]]),  # type: ignore[arg-type]
            client_id=CLIENT_ID,
        )

        assert filas[0]["main_diagnosis"] == "I10"
        assert "reposo" not in str(filas)
        assert "1234567890" not in str(filas)


class TestApiRegistros:
    @pytest.mark.parametrize("rol", ["agent", "supervisor"])
    @pytest.mark.parametrize("ruta", ["review", "sign", "submit"])
    async def test_solo_administradores_firman(
        self, authenticated_client_factory: Any, rol: str, ruta: str
    ) -> None:
        cliente = authenticated_client_factory(role=rol)

        response = await cliente.post(f"/api/v1/clinical/records/{RECORD_ID}/{ruta}")

        assert response.status_code == 403

    async def test_firmar_devuelve_el_registro(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parchear_tenant_session(
            monkeypatch,
            api,
            FakeSession(
                resultados=[
                    RECORD_ID,
                    _registro("signed", signed_at=datetime(2025, 1, 16, tzinfo=timezone.utc)),
                    date(2025, 1, 15),
                ]
            ),
        )

        response = await authenticated_client.post(f"/api/v1/clinical/records/{RECORD_ID}/sign")

        assert response.status_code == 200, response.text
        assert response.json()["status"] == "signed"

    async def test_un_registro_fuera_de_estado_da_409(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parchear_tenant_session(monkeypatch, api, FakeSession(resultados=[None, "draft"]))

        response = await authenticated_client.post(f"/api/v1/clinical/records/{RECORD_ID}/sign")

        assert response.status_code == 409

    async def test_un_registro_inexistente_da_404(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parchear_tenant_session(monkeypatch, api, FakeSession(resultados=[None]))

        response = await authenticated_client.get(f"/api/v1/clinical/records/{RECORD_ID}")

        assert response.status_code == 404

    async def test_estado_invalido_en_el_listado_da_400(self, authenticated_client: Any) -> None:
        response = await authenticated_client.get("/api/v1/clinical/records?status=inventado")

        assert response.status_code == 400

    async def test_detalle(
        self, authenticated_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parchear_tenant_session(
            monkeypatch, api, FakeSession(resultados=[_registro(), date(2025, 1, 15)])
        )

        response = await authenticated_client.get(f"/api/v1/clinical/records/{RECORD_ID}")

        assert response.status_code == 200
        assert response.json()["patient_document"] == "******7890"
        assert "1234567890" not in response.text


class TestPrivacidadDelContenidoClinico:
    def test_el_router_de_intents_no_registra_el_texto_si_descubre_que_es_clinico(self) -> None:
        """`intent_routing` corre ANTES de conocer el intent: se redacta al terminar."""
        resumen = lm._build_input_summary(
            estado(message={"text": "paciente Ana Perez, hipertension"}),
            "intent_routing",
            {"intent": "clinical"},
        )

        assert "Ana Perez" not in resumen

    async def test_el_wrapper_de_intent_routing_no_persiste_el_dictado(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registrado: dict[str, Any] = {}

        async def _registrar(**kwargs: Any) -> None:
            registrado.update(kwargs)

        monkeypatch.setattr(lm, "_registrar", _registrar)

        async def _router(state: Any) -> dict[str, Any]:
            return {"intent": "clinical", "intent_confidence": 0.9}

        await lm.logged_node("intent_routing", "decision")(_router)(
            estado(
                client_id=str(CLIENT_ID),
                conversation_id=str(uuid.uuid4()),
                message={"text": "dictado de Ana Perez"},
            )
        )

        assert "Ana Perez" not in registrado["input_summary"]

    async def test_el_mensaje_entrante_se_reemplaza_por_un_marcador(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sesion = parchear_tenant_session(monkeypatch, nodo, FakeSession())

        await nodo._proteger_mensaje_entrante(
            estado(
                client_id=str(CLIENT_ID),
                conversation_id=str(uuid.uuid4()),
                message={"text": "dictado", "external_message_id": "wamid.123"},
            )
        )

        sql = str(sesion.executed[0]).lower()
        assert sql.startswith("update messages")
        assert "messages.direction" in sql
        assert "messages.client_id" in sql

    async def test_sin_id_externo_no_toca_nada(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sesion = parchear_tenant_session(monkeypatch, nodo, FakeSession())

        await nodo._proteger_mensaje_entrante(
            estado(client_id=str(CLIENT_ID), conversation_id=str(uuid.uuid4()), message={})
        )

        assert sesion.executed == []

    async def test_un_fallo_al_proteger_no_tumba_la_respuesta(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        @asynccontextmanager
        async def _roto(client_id: Any) -> Any:
            raise RuntimeError("db caida")
            yield  # pragma: no cover

        monkeypatch.setattr(nodo, "tenant_session", _roto)

        await nodo._proteger_mensaje_entrante(
            estado(
                client_id=str(CLIENT_ID),
                conversation_id=str(uuid.uuid4()),
                message={"external_message_id": "x"},
            )
        )

    async def test_el_nodo_protege_el_mensaje_aunque_el_llm_falle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El reintento de Celery usa el argumento de la tarea, no la base."""
        from app.agents.nodes._tenant import AgentSettings
        from tests.unit.agent_doubles import parchear_agent_settings

        parchear_agent_settings(
            monkeypatch, nodo, AgentSettings(model="gpt-4o", enabled_agents=("rag", "clinical"))
        )
        parchear_tenant_session(monkeypatch, nodo, FakeSession())

        async def _autorizado(*_a: Any) -> bool:
            return True

        monkeypatch.setattr(nodo, "es_profesional_clinico", _autorizado)
        protegido: list[bool] = []

        async def _proteger(state: Any) -> None:
            protegido.append(True)

        monkeypatch.setattr(nodo, "_proteger_mensaje_entrante", _proteger)

        async def _falla(**_k: Any) -> str:
            raise RuntimeError("openai caido")

        monkeypatch.setattr(nodo, "responder_con_tools", _falla)

        with pytest.raises(RuntimeError):
            await nodo.clinical_agent_node(estado(client_id=str(CLIENT_ID), contact_id="c"))

        assert protegido == [True]

    async def test_respond_guarda_un_marcador_y_envia_el_texto_completo(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        enviado: dict[str, Any] = {}

        async def _deliver(**kwargs: Any) -> None:
            enviado.update(kwargs)

        monkeypatch.setattr(respond_mod, "deliver_message", _deliver)

        await respond_mod.respond_node(
            estado(
                client_id=str(CLIENT_ID),
                conversation_id=str(uuid.uuid4()),
                contact_id=str(uuid.uuid4()),
                channel="whatsapp",
                intent="clinical",
                response_text="Registro de Ana Perez creado",
            )
        )

        assert enviado["text"] == "Registro de Ana Perez creado"
        assert enviado["stored_text"] == respond_mod.CONTENIDO_CLINICO_PROTEGIDO

    async def test_respond_de_otros_intents_guarda_el_texto_tal_cual(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        enviado: dict[str, Any] = {}

        async def _deliver(**kwargs: Any) -> None:
            enviado.update(kwargs)

        monkeypatch.setattr(respond_mod, "deliver_message", _deliver)

        await respond_mod.respond_node(
            estado(
                client_id=str(CLIENT_ID),
                conversation_id=str(uuid.uuid4()),
                contact_id=str(uuid.uuid4()),
                channel="whatsapp",
                intent="greeting",
                response_text="Hola",
            )
        )

        assert enviado["stored_text"] is None

    def test_deliver_message_acepta_el_texto_a_guardar(self) -> None:
        import inspect

        assert "stored_text" in inspect.signature(_delivery.deliver_message).parameters

    async def test_un_turno_clinico_purga_los_checkpoints(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Los checkpoints de LangGraph guardan el estado (el dictado) sin cifrar ni RLS."""
        sesion = FakeSession()
        parchear_tenant_session(monkeypatch, tarea, sesion) if hasattr(
            tarea, "tenant_session"
        ) else None
        import app.core.database as db

        monkeypatch.setattr(db, "tenant_session", lambda cid: _cm(sesion))

        await tarea._purgar_checkpoints(str(CLIENT_ID), "conv-1")

        tablas = [str(s) for s in sesion.executed]
        assert any("checkpoint_writes" in t for t in tablas)
        assert any("checkpoint_blobs" in t for t in tablas)
        assert any("FROM checkpoints" in t for t in tablas)

    async def test_un_fallo_al_purgar_no_tumba_la_tarea(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app.core.database as db

        def _roto(cid: Any) -> Any:
            raise RuntimeError("db caida")

        monkeypatch.setattr(db, "tenant_session", _roto)

        await tarea._purgar_checkpoints(str(CLIENT_ID), "conv-1")


@asynccontextmanager
async def _cm(sesion: FakeSession) -> Any:
    yield sesion


class TestCargadorDeCatalogos:
    def test_lee_csv_con_punto_y_coma_bom_y_cabecera_en_espanol(self, tmp_path: Any) -> None:
        import sys

        sys.path.insert(
            0, str(__import__("pathlib").Path(__file__).resolve().parents[2] / "scripts")
        )
        import load_clinical_catalogs as cargador

        archivo = tmp_path / "cie10.csv"
        archivo.write_text(
            "Código;Descripción\nI10;Hipertensión esencial\nJ06.9;Infección\n", encoding="utf-8-sig"
        )

        assert list(cargador.leer_filas(archivo)) == [
            ("I10", "Hipertensión esencial"),
            ("J06.9", "Infección"),
        ]

    def test_una_cabecera_sin_las_dos_columnas_aborta(self, tmp_path: Any) -> None:
        import sys

        sys.path.insert(
            0, str(__import__("pathlib").Path(__file__).resolve().parents[2] / "scripts")
        )
        import load_clinical_catalogs as cargador

        archivo = tmp_path / "malo.csv"
        archivo.write_text("a,b\n1,2\n", encoding="utf-8")

        with pytest.raises(SystemExit):
            list(cargador.leer_filas(archivo))
