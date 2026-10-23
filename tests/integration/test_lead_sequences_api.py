# ruff: noqa: F811
"""Sprint 18, Dev B: API de secuencias, inscripciones, Kanban de follow-up y mensaje manual.

Requiere base de datos y Redis: `pytest tests/ --run-db`. Lo que un doble no prueba: RLS real
entre tenants, el UNIQUE parcial de las inscripciones vivas, el borrado en cascada, y que el
Kanban agrupe de verdad con una sola consulta.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio

from app.core.config import get_settings
from app.services.lead_sequence_templates import PLANTILLAS
from tests.integration.lead_helpers import activar_modulo, crear_lead, limpiar_leads
from tests.integration.sequence_helpers import (
    AHORA,
    ProveedorFalso,
    crear_sec,
    definicion,
    espera,
    inscribir_lead,
    instalar_proveedor_falso,
    mensaje,
    sql,
    uno,
    usuario,
)
from tests.integration.test_crm_api import Escenario, _cliente, dos_tenants, escenario  # noqa: F401

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

SEQS = "/api/v1/lead-sequences"
LEADS = "/api/v1/leads"


@pytest_asyncio.fixture
async def crm(escenario: Escenario) -> AsyncGenerator[Escenario, None]:
    await activar_modulo(escenario.client_id)
    yield escenario
    await limpiar_leads(escenario.client_id)


@pytest_asyncio.fixture
async def crm_dos(
    dos_tenants: tuple[Escenario, Escenario],
) -> AsyncGenerator[tuple[Escenario, Escenario], None]:
    for e in dos_tenants:
        await activar_modulo(e.client_id)
    yield dos_tenants
    for e in dos_tenants:
        await limpiar_leads(e.client_id)


@pytest.fixture
def proveedor(monkeypatch: pytest.MonkeyPatch) -> ProveedorFalso:
    return instalar_proveedor_falso(monkeypatch)


def _cuerpo(nombre: str = "Bienvenida", **extra: object) -> dict[str, object]:
    """Una definicion valida como JSON de la API."""
    d = definicion(mensaje(), espera(2), mensaje("Seguimos aqui"), nombre=nombre)
    return {**d.model_dump(mode="json"), **extra}


# ── CRUD ─────────────────────────────────────────────────────────────────────────────────────


class TestCrud:
    async def test_crear_y_leer_con_sus_pasos(self, crm: Escenario) -> None:
        async with _cliente(crm, "admin") as c:
            r = await c.post(SEQS, json=_cuerpo())
            assert r.status_code == 201, r.text
            creada = r.json()
            detalle = (await c.get(f"{SEQS}/{creada['id']}")).json()
            pasos = (await c.get(f"{SEQS}/{creada['id']}/steps")).json()

        assert creada["is_active"] is True
        assert creada["steps_count"] == 3
        assert [p["position"] for p in pasos] == [1, 2, 3]
        assert [p["step"]["type"] for p in pasos] == ["message", "wait", "message"]
        assert detalle["steps"] == pasos

    async def test_lista_con_pasos_e_inscripciones_vivas(self, crm: Escenario) -> None:
        sec = await crear_sec(crm.client_id, mensaje(), espera())
        lead = await crear_lead(crm.client_id, first_name="Ana", email="ana@x.com")
        await inscribir_lead(crm.client_id, lead, sec)
        async with _cliente(crm, "agent") as c:
            filas = (await c.get(SEQS)).json()

        fila = next(f for f in filas if f["id"] == str(sec))
        assert fila["steps_count"] == 2
        assert fila["live_enrollments"] == 1

    async def test_filtra_por_activa(self, crm: Escenario) -> None:
        await crear_sec(crm.client_id, mensaje(), activa=True, nombre="Encendida")
        await crear_sec(crm.client_id, mensaje(), activa=False, nombre="Apagada")
        async with _cliente(crm, "admin") as c:
            encendidas = (await c.get(SEQS, params={"active": "true"})).json()
            apagadas = (await c.get(SEQS, params={"active": "false"})).json()

        assert [f["name"] for f in encendidas] == ["Encendida"]
        assert [f["name"] for f in apagadas] == ["Apagada"]

    async def test_nombre_repetido_es_409(self, crm: Escenario) -> None:
        async with _cliente(crm, "admin") as c:
            await c.post(SEQS, json=_cuerpo("Igual"))
            r = await c.post(SEQS, json=_cuerpo("Igual"))

        assert r.status_code == 409
        assert r.json()["error_code"] == "DUPLICATE"

    async def test_un_bucle_sin_espera_se_rechaza(self, crm: Escenario) -> None:
        """El grafo se valida: un salto hacia atras sin `wait` mandaria mensajes sin parar."""
        cuerpo = _cuerpo()
        cuerpo["steps"] = [
            {"type": "message", "body": "Hola"},
            {
                "type": "condition",
                "check": "no_reply",
                "if_true": {"action": "goto", "goto_position": 1},
            },
        ]
        async with _cliente(crm, "admin") as c:
            r = await c.post(SEQS, json=cuerpo)

        assert r.status_code == 422

    async def test_un_agente_no_crea_edita_ni_borra(self, crm: Escenario) -> None:
        sec = await crear_sec(crm.client_id, mensaje())
        async with _cliente(crm, "agent") as c:
            crear = await c.post(SEQS, json=_cuerpo())
            editar = await c.put(f"{SEQS}/{sec}", json=_cuerpo())
            borrar = await c.delete(f"{SEQS}/{sec}")
            plantilla = await c.post(f"{SEQS}/from-template", json={"template": "outbound_cold"})

        assert {
            crear.status_code,
            editar.status_code,
            borrar.status_code,
            plantilla.status_code,
        } == {403}

    async def test_un_supervisor_si_puede(self, crm: Escenario) -> None:
        async with _cliente(crm, "supervisor") as c:
            r = await c.post(SEQS, json=_cuerpo())
        assert r.status_code == 201

    async def test_con_el_modulo_apagado_todo_es_403(self, crm: Escenario) -> None:
        await activar_modulo(crm.client_id, False)
        async with _cliente(crm, "admin") as c:
            respuestas = [await c.get(SEQS), await c.post(SEQS, json=_cuerpo())]
        assert [r.status_code for r in respuestas] == [403, 403]

    async def test_editar_reemplaza_los_pasos_y_avisa_si_hay_inscripciones(
        self, crm: Escenario
    ) -> None:
        sec = await crear_sec(crm.client_id, mensaje(), espera(), mensaje("Dos"))
        lead = await crear_lead(crm.client_id, first_name="Ana", email="ana@x.com")
        await inscribir_lead(crm.client_id, lead, sec)
        nuevo = definicion(mensaje("Solo uno"), nombre="Renombrada").model_dump(mode="json")
        async with _cliente(crm, "admin") as c:
            r = await c.put(f"{SEQS}/{sec}", json={**nuevo, "is_active": False})

        assert r.status_code == 200, r.text
        cuerpo = r.json()
        assert cuerpo["name"] == "Renombrada"
        assert cuerpo["is_active"] is False
        assert cuerpo["steps_count"] == 1
        assert cuerpo["steps"][0]["step"]["body"] == "Solo uno"
        assert "posicion" in cuerpo["live_enrollments_warning"]

    async def test_editar_sin_inscripciones_no_avisa(self, crm: Escenario) -> None:
        sec = await crear_sec(crm.client_id, mensaje())
        async with _cliente(crm, "admin") as c:
            r = await c.put(f"{SEQS}/{sec}", json=_cuerpo("Otra"))
        assert r.json()["live_enrollments_warning"] is None

    async def test_editar_con_el_nombre_de_otra_es_409_y_no_cambia_nada(
        self, crm: Escenario
    ) -> None:
        await crear_sec(crm.client_id, mensaje(), nombre="Ocupado")
        sec = await crear_sec(crm.client_id, mensaje(), nombre="Mia")
        async with _cliente(crm, "admin") as c:
            r = await c.put(f"{SEQS}/{sec}", json=_cuerpo("Ocupado"))
            despues = (await c.get(f"{SEQS}/{sec}")).json()

        assert r.status_code == 409
        assert despues["name"] == "Mia"
        assert despues["steps_count"] == 1

    async def test_borrar_sin_inscripciones(self, crm: Escenario) -> None:
        sec = await crear_sec(crm.client_id, mensaje())
        async with _cliente(crm, "admin") as c:
            assert (await c.delete(f"{SEQS}/{sec}")).status_code == 204
            assert (await c.get(f"{SEQS}/{sec}")).status_code == 404

    async def test_borrar_con_historial_de_inscripciones_es_409(self, crm: Escenario) -> None:
        sec = await crear_sec(crm.client_id, mensaje())
        lead = await crear_lead(crm.client_id, first_name="Ana", email="ana@x.com")
        await inscribir_lead(crm.client_id, lead, sec)
        async with _cliente(crm, "admin") as c:
            r = await c.delete(f"{SEQS}/{sec}")

        assert r.status_code == 409
        assert "apagala" in r.json()["message"]

    async def test_secuencia_inexistente_es_404(self, crm: Escenario) -> None:
        async with _cliente(crm, "admin") as c:
            ids = uuid.uuid4()
            respuestas = [
                await c.get(f"{SEQS}/{ids}"),
                await c.get(f"{SEQS}/{ids}/steps"),
                await c.put(f"{SEQS}/{ids}", json=_cuerpo()),
                await c.delete(f"{SEQS}/{ids}"),
            ]
        assert [r.status_code for r in respuestas] == [404] * 4

    async def test_pasos_editados_a_mano_no_rompen_el_listado(self, crm: Escenario) -> None:
        """Un paso corrupto solo falla el detalle de esa secuencia, no el listado."""
        sec = await crear_sec(crm.client_id, mensaje())
        await sql(
            crm.client_id,
            'UPDATE lead_sequence_steps SET config = \'{"type": "wait"}\' WHERE sequence_id = :s',
            s=str(sec),
        )
        async with _cliente(crm, "admin") as c:
            lista = await c.get(SEQS)
            detalle = await c.get(f"{SEQS}/{sec}")

        assert lista.status_code == 200
        assert detalle.status_code == 409


class TestPlantillas:
    async def test_lista_las_predefinidas(self, crm: Escenario) -> None:
        async with _cliente(crm, "agent") as c:
            r = await c.get(f"{SEQS}/templates")

        claves = {t["key"] for t in r.json()}
        assert claves == set(PLANTILLAS)
        entrantes = next(t for t in r.json() if t["key"] == "inbound_nurturing")
        assert entrantes["auto_trigger"] is True

    async def test_copiar_una_plantilla_la_deja_apagada(self, crm: Escenario) -> None:
        """Una plantilla con disparador no debe empezar a escribir antes de que alguien la revise."""
        async with _cliente(crm, "admin") as c:
            r = await c.post(f"{SEQS}/from-template", json={"template": "inbound_nurturing"})

        assert r.status_code == 201, r.text
        assert r.json()["is_active"] is False
        assert r.json()["steps_count"] == len(PLANTILLAS["inbound_nurturing"].steps)

    async def test_copiar_con_nombre_propio_y_repetida_es_409(self, crm: Escenario) -> None:
        async with _cliente(crm, "admin") as c:
            uno_ = await c.post(
                f"{SEQS}/from-template", json={"template": "outbound_cold", "name": "Mi frio"}
            )
            otra = await c.post(
                f"{SEQS}/from-template", json={"template": "reengagement", "name": "Mi frio"}
            )

        assert uno_.json()["name"] == "Mi frio"
        assert otra.status_code == 409

    async def test_plantilla_desconocida_es_404(self, crm: Escenario) -> None:
        async with _cliente(crm, "admin") as c:
            r = await c.post(f"{SEQS}/from-template", json={"template": "no_existe"})
        assert r.status_code == 404

    async def test_copiar_no_modifica_la_plantilla_del_codigo(self, crm: Escenario) -> None:
        antes = PLANTILLAS["outbound_cold"].model_dump()
        async with _cliente(crm, "admin") as c:
            await c.post(f"{SEQS}/from-template", json={"template": "outbound_cold", "name": "X"})
        assert PLANTILLAS["outbound_cold"].model_dump() == antes


# ── Aislamiento entre tenants ────────────────────────────────────────────────────────────────


class TestAislamiento:
    async def test_un_tenant_no_ve_ni_toca_las_secuencias_de_otro(
        self, crm_dos: tuple[Escenario, Escenario]
    ) -> None:
        a, b = crm_dos
        sec_a = await crear_sec(a.client_id, mensaje(), nombre="De A")
        async with _cliente(b, "admin") as c:
            lista = (await c.get(SEQS)).json()
            respuestas = [
                await c.get(f"{SEQS}/{sec_a}"),
                await c.get(f"{SEQS}/{sec_a}/steps"),
                await c.put(f"{SEQS}/{sec_a}", json=_cuerpo()),
                await c.delete(f"{SEQS}/{sec_a}"),
            ]

        assert lista == []
        assert [r.status_code for r in respuestas] == [404] * 4
        assert await uno(a.client_id, "SELECT count(*) FROM lead_sequences") == (1,)

    async def test_no_se_inscribe_un_lead_de_otro_tenant(
        self, crm_dos: tuple[Escenario, Escenario]
    ) -> None:
        a, b = crm_dos
        lead_a = await crear_lead(a.client_id, first_name="Ana", email="ana@x.com")
        sec_b = await crear_sec(b.client_id, mensaje())
        sec_a = await crear_sec(a.client_id, mensaje())
        async with _cliente(b, "admin") as c:
            lead_ajeno = await c.post(f"{LEADS}/{lead_a}/enroll/{sec_b}")
        async with _cliente(a, "admin") as c:
            sec_ajena = await c.post(f"{LEADS}/{lead_a}/enroll/{sec_b}")
            ok = await c.post(f"{LEADS}/{lead_a}/enroll/{sec_a}")

        assert lead_ajeno.status_code == 404
        assert sec_ajena.status_code == 404
        assert ok.status_code == 201

    async def test_el_kanban_solo_muestra_lo_del_tenant(
        self, crm_dos: tuple[Escenario, Escenario]
    ) -> None:
        a, b = crm_dos
        lead = await crear_lead(a.client_id, first_name="Ana", email="ana@x.com")
        await inscribir_lead(a.client_id, lead, await crear_sec(a.client_id, mensaje()))
        async with _cliente(b, "admin") as c:
            columnas = (await c.get(f"{LEADS}/follow-up/kanban")).json()
        assert sum(col["total"] for col in columnas) == 0

    async def test_no_se_escribe_al_lead_de_otro_tenant(
        self, crm_dos: tuple[Escenario, Escenario], proveedor: ProveedorFalso
    ) -> None:
        a, b = crm_dos
        lead_a = await crear_lead(a.client_id, first_name="Ana", email="ana@x.com")
        async with _cliente(b, "admin") as c:
            r = await c.post(f"{LEADS}/{lead_a}/message", json={"channel": "email", "text": "Hola"})

        assert r.status_code == 404
        assert proveedor.enviados == []


# ── Inscripciones ────────────────────────────────────────────────────────────────────────────


class TestInscripciones:
    async def test_inscribir_y_sacar(self, crm: Escenario) -> None:
        sec = await crear_sec(crm.client_id, mensaje())
        lead = await crear_lead(crm.client_id, first_name="Ana", email="ana@x.com")
        async with _cliente(crm, "agent") as c:
            alta = await c.post(f"{LEADS}/{lead}/enroll/{sec}")
            baja = await c.delete(f"{LEADS}/{lead}/enroll/{sec}")
            historial = (await c.get(f"{LEADS}/{lead}/enrollments")).json()

        assert alta.status_code == 201, alta.text
        assert alta.json()["status"] == "active"
        assert alta.json()["current_step"] == 1
        assert baja.status_code == 204
        assert historial[0]["status"] == "exited"
        assert historial[0]["exit_reason"] == "manual"
        # Queda en el historial del lead: quien lo inscribio y quien lo saco.
        tipos = await sql(
            crm.client_id,
            "SELECT activity_type FROM lead_activities WHERE lead_id = :l ORDER BY created_at",
            l=str(lead),
        )
        assert [t[0] for t in tipos][-2:] == ["sequence_enrolled", "sequence_exited"]

    async def test_el_primer_paso_toca_en_horario_del_negocio(self, crm: Escenario) -> None:
        sec = await crear_sec(crm.client_id, mensaje())
        lead = await crear_lead(crm.client_id, first_name="Ana", email="ana@x.com")
        async with _cliente(crm, "admin") as c:
            r = await c.post(f"{LEADS}/{lead}/enroll/{sec}")

        toca = datetime.fromisoformat(r.json()["next_step_at"])
        assert toca >= datetime.now(timezone.utc) - timedelta(seconds=5)

    async def test_inscribir_dos_veces_es_409(self, crm: Escenario) -> None:
        sec = await crear_sec(crm.client_id, mensaje())
        lead = await crear_lead(crm.client_id, first_name="Ana", email="ana@x.com")
        async with _cliente(crm, "admin") as c:
            await c.post(f"{LEADS}/{lead}/enroll/{sec}")
            r = await c.post(f"{LEADS}/{lead}/enroll/{sec}")

        assert r.status_code == 409
        assert "ya esta" in r.json()["message"]

    async def test_se_puede_reinscribir_tras_salir(self, crm: Escenario) -> None:
        sec = await crear_sec(crm.client_id, mensaje())
        lead = await crear_lead(crm.client_id, first_name="Ana", email="ana@x.com")
        async with _cliente(crm, "admin") as c:
            await c.post(f"{LEADS}/{lead}/enroll/{sec}")
            await c.delete(f"{LEADS}/{lead}/enroll/{sec}")
            r = await c.post(f"{LEADS}/{lead}/enroll/{sec}")
        assert r.status_code == 201

    async def test_secuencia_apagada_no_inscribe(self, crm: Escenario) -> None:
        sec = await crear_sec(crm.client_id, mensaje(), activa=False)
        lead = await crear_lead(crm.client_id, first_name="Ana", email="ana@x.com")
        async with _cliente(crm, "admin") as c:
            r = await c.post(f"{LEADS}/{lead}/enroll/{sec}")
        assert r.status_code == 409

    async def test_lead_cerrado_no_se_inscribe(self, crm: Escenario) -> None:
        sec = await crear_sec(crm.client_id, mensaje())
        lead = await crear_lead(crm.client_id, first_name="Ana", email="ana@x.com", status="lost")
        async with _cliente(crm, "admin") as c:
            r = await c.post(f"{LEADS}/{lead}/enroll/{sec}")
        assert r.status_code == 409

    async def test_quien_pidio_la_baja_no_se_inscribe_ni_a_mano(self, crm: Escenario) -> None:
        sec = await crear_sec(crm.client_id, mensaje())
        otra = await crear_sec(crm.client_id, mensaje())
        lead = await crear_lead(crm.client_id, first_name="Ana", email="ana@x.com")
        id_ins = await inscribir_lead(crm.client_id, lead, sec)
        await sql(
            crm.client_id,
            "UPDATE lead_sequence_enrollments SET status='exited', exit_reason='unsubscribed', "
            "completed_at = now(), next_step_at = NULL WHERE id = :i",
            i=str(id_ins),
        )
        async with _cliente(crm, "admin") as c:
            r = await c.post(f"{LEADS}/{lead}/enroll/{otra}")

        assert r.status_code == 409
        assert "no recibir" in r.json()["message"]

    async def test_sacar_a_quien_no_esta_es_404(self, crm: Escenario) -> None:
        sec = await crear_sec(crm.client_id, mensaje())
        lead = await crear_lead(crm.client_id, first_name="Ana", email="ana@x.com")
        async with _cliente(crm, "admin") as c:
            r = await c.delete(f"{LEADS}/{lead}/enroll/{sec}")
        assert r.status_code == 404

    async def test_pausar_y_reanudar(self, crm: Escenario) -> None:
        sec = await crear_sec(crm.client_id, mensaje())
        lead = await crear_lead(crm.client_id, first_name="Ana", email="ana@x.com")
        async with _cliente(crm, "admin") as c:
            await c.post(f"{LEADS}/{lead}/enroll/{sec}")
            pausada = await c.post(f"{LEADS}/{lead}/enroll/{sec}/pause")
            otra_pausa = await c.post(f"{LEADS}/{lead}/enroll/{sec}/pause")
            reanudada = await c.post(f"{LEADS}/{lead}/enroll/{sec}/resume")
            otra_reanudacion = await c.post(f"{LEADS}/{lead}/enroll/{sec}/resume")

        assert pausada.json()["status"] == "paused"
        assert pausada.json()["next_step_at"] is None
        assert otra_pausa.status_code == 409
        assert reanudada.json()["status"] == "active"
        assert reanudada.json()["next_step_at"] is not None
        assert otra_reanudacion.status_code == 409

    async def test_pausar_a_quien_no_esta_es_404(self, crm: Escenario) -> None:
        sec = await crear_sec(crm.client_id, mensaje())
        lead = await crear_lead(crm.client_id, first_name="Ana", email="ana@x.com")
        async with _cliente(crm, "admin") as c:
            r = await c.post(f"{LEADS}/{lead}/enroll/{sec}/pause")
        assert r.status_code == 404

    async def test_un_agente_solo_inscribe_leads_sin_responsable_o_suyos(
        self, crm: Escenario
    ) -> None:
        sec = await crear_sec(crm.client_id, mensaje())
        otro = await usuario(crm.client_id)
        libre = await crear_lead(crm.client_id, first_name="Libre", email="libre@x.com")
        suyo = await crear_lead(
            crm.client_id, first_name="Suyo", email="suyo@x.com", assigned_user_id=crm.user_id
        )
        ajeno = await crear_lead(
            crm.client_id, first_name="Ajeno", email="ajeno@x.com", assigned_user_id=otro
        )
        async with _cliente(crm, "agent") as c:
            estados = [
                (await c.post(f"{LEADS}/{lead}/enroll/{sec}")).status_code
                for lead in (libre, suyo, ajeno)
            ]
            sacar_ajeno = await c.delete(f"{LEADS}/{ajeno}/enroll/{sec}")

        assert estados == [201, 201, 403]
        assert sacar_ajeno.status_code == 403
        async with _cliente(crm, "supervisor") as c:
            assert (await c.post(f"{LEADS}/{ajeno}/enroll/{sec}")).status_code == 201

    async def test_lead_borrado_es_404(self, crm: Escenario) -> None:
        sec = await crear_sec(crm.client_id, mensaje())
        lead = await crear_lead(
            crm.client_id,
            first_name="Ana",
            email="ana@x.com",
            deleted_at=datetime.now(timezone.utc),
        )
        async with _cliente(crm, "admin") as c:
            r = await c.post(f"{LEADS}/{lead}/enroll/{sec}")
        assert r.status_code == 404

    async def test_historial_de_un_lead_inexistente_es_404(self, crm: Escenario) -> None:
        async with _cliente(crm, "admin") as c:
            r = await c.get(f"{LEADS}/{uuid.uuid4()}/enrollments")
        assert r.status_code == 404


# ── Kanban de follow-up ──────────────────────────────────────────────────────────────────────


async def _estado(
    client_id: uuid.UUID,
    inscripcion: uuid.UUID,
    estado: str,
    *,
    motivo: str | None = None,
    toca_en: datetime | None = None,
    cerrada_hace: timedelta | None = None,
) -> None:
    cerrada = estado in ("completed", "exited")
    await sql(
        client_id,
        "UPDATE lead_sequence_enrollments SET status = :s, exit_reason = :m, next_step_at = :n, "
        "completed_at = :c WHERE id = :i",
        s=estado,
        m=motivo,
        n=toca_en,
        c=(datetime.now(timezone.utc) - (cerrada_hace or timedelta())) if cerrada else None,
        i=str(inscripcion),
    )


class TestKanban:
    async def _escenario(self, crm: Escenario) -> dict[str, uuid.UUID]:
        sec = await crear_sec(crm.client_id, mensaje(), nombre="Principal")
        ahora = datetime.now(timezone.utc)
        ids: dict[str, uuid.UUID] = {}
        casos = {
            "overdue": ("active", None, ahora - timedelta(hours=1), None),
            "upcoming": ("active", None, ahora + timedelta(days=1), None),
            "paused": ("paused", None, None, None),
            "replied": ("exited", "replied", None, timedelta(days=2)),
            "baja": ("exited", "unsubscribed", None, timedelta(days=2)),
            "finished": ("completed", None, None, timedelta(days=2)),
            "bounced": ("exited", "bounced", None, timedelta(days=2)),
            "vieja": ("exited", "replied", None, timedelta(days=45)),
        }
        for i, (nombre, (estado, motivo, toca, hace)) in enumerate(casos.items()):
            lead = await crear_lead(
                crm.client_id, first_name=nombre.title(), email=f"{nombre}@x.com", total_score=i
            )
            ins = await inscribir_lead(crm.client_id, lead, sec)
            await _estado(
                crm.client_id, ins, estado, motivo=motivo, toca_en=toca, cerrada_hace=hace
            )
            ids[nombre] = lead
        return ids

    async def test_agrupa_en_las_cinco_columnas(self, crm: Escenario) -> None:
        ids = await self._escenario(crm)
        async with _cliente(crm, "agent") as c:
            columnas = (await c.get(f"{LEADS}/follow-up/kanban")).json()

        por_clave = {col["key"]: col for col in columnas}
        assert [col["key"] for col in columnas] == [
            "overdue",
            "upcoming",
            "paused",
            "replied",
            "finished",
        ]
        leads = {k: {t["lead_id"] for t in v["cards"]} for k, v in por_clave.items()}
        assert leads["overdue"] == {str(ids["overdue"])}
        assert leads["upcoming"] == {str(ids["upcoming"])}
        assert leads["paused"] == {str(ids["paused"])}
        # Contestar y pedir la baja van juntos; terminar o rebotar, a "finished"; lo de hace 45
        # dias ya no aparece.
        assert leads["replied"] == {str(ids["replied"]), str(ids["baja"])}
        assert leads["finished"] == {str(ids["finished"]), str(ids["bounced"])}
        assert {k: v["total"] for k, v in por_clave.items()} == {
            "overdue": 1,
            "upcoming": 1,
            "paused": 1,
            "replied": 2,
            "finished": 2,
        }

    async def test_per_column_acota_las_tarjetas_pero_no_el_total(self, crm: Escenario) -> None:
        sec = await crear_sec(crm.client_id, mensaje())
        for i in range(5):
            lead = await crear_lead(crm.client_id, first_name=f"L{i}", email=f"l{i}@x.com")
            await inscribir_lead(crm.client_id, lead, sec, toca_en=AHORA - timedelta(hours=i))
        async with _cliente(crm, "agent") as c:
            columnas = (await c.get(f"{LEADS}/follow-up/kanban", params={"per_column": 2})).json()

        vencidas = next(col for col in columnas if col["key"] == "overdue")
        assert vencidas["total"] == 5
        assert len(vencidas["cards"]) == 2

    async def test_las_vencidas_van_de_la_mas_atrasada_a_la_menos(self, crm: Escenario) -> None:
        sec = await crear_sec(crm.client_id, mensaje())
        orden = []
        for horas in (1, 5, 3):
            lead = await crear_lead(crm.client_id, first_name=f"H{horas}", email=f"h{horas}@x.com")
            await inscribir_lead(
                crm.client_id,
                lead,
                sec,
                toca_en=datetime.now(timezone.utc) - timedelta(hours=horas),
            )
            orden.append(horas)
        async with _cliente(crm, "agent") as c:
            columnas = (await c.get(f"{LEADS}/follow-up/kanban")).json()

        vencidas = next(col for col in columnas if col["key"] == "overdue")
        assert [t["first_name"] for t in vencidas["cards"]] == ["H5", "H3", "H1"]

    async def test_filtra_por_secuencia_y_por_responsable(self, crm: Escenario) -> None:
        s1 = await crear_sec(crm.client_id, mensaje(), nombre="Uno")
        s2 = await crear_sec(crm.client_id, mensaje(), nombre="Dos")
        mio = await crear_lead(
            crm.client_id, first_name="Mio", email="mio@x.com", assigned_user_id=crm.user_id
        )
        otro = await crear_lead(crm.client_id, first_name="Otro", email="otro@x.com")
        await inscribir_lead(crm.client_id, mio, s1)
        await inscribir_lead(crm.client_id, otro, s2)
        async with _cliente(crm, "admin") as c:
            por_secuencia = (
                await c.get(f"{LEADS}/follow-up/kanban", params={"sequence_id": str(s2)})
            ).json()
            por_usuario = (
                await c.get(
                    f"{LEADS}/follow-up/kanban", params={"assigned_user_id": str(crm.user_id)}
                )
            ).json()

        def nombres(columnas: list[dict[str, object]]) -> set[str]:
            return {t["first_name"] for col in columnas for t in col["cards"]}  # type: ignore[union-attr, misc]

        assert nombres(por_secuencia) == {"Otro"}
        assert nombres(por_usuario) == {"Mio"}

    async def test_un_lead_borrado_no_aparece(self, crm: Escenario) -> None:
        sec = await crear_sec(crm.client_id, mensaje())
        lead = await crear_lead(crm.client_id, first_name="Ana", email="ana@x.com")
        await inscribir_lead(crm.client_id, lead, sec)
        await sql(crm.client_id, "UPDATE leads SET deleted_at = now() WHERE id = :l", l=str(lead))
        async with _cliente(crm, "agent") as c:
            columnas = (await c.get(f"{LEADS}/follow-up/kanban")).json()
        assert sum(col["total"] for col in columnas) == 0

    async def test_per_column_fuera_de_rango_es_422(self, crm: Escenario) -> None:
        async with _cliente(crm, "agent") as c:
            malos = [
                await c.get(f"{LEADS}/follow-up/kanban", params={"per_column": 0}),
                await c.get(f"{LEADS}/follow-up/kanban", params={"per_column": 101}),
            ]
        assert [r.status_code for r in malos] == [422, 422]

    async def test_sin_inscripciones_devuelve_las_cinco_columnas_vacias(
        self, crm: Escenario
    ) -> None:
        async with _cliente(crm, "agent") as c:
            columnas = (await c.get(f"{LEADS}/follow-up/kanban")).json()
        assert len(columnas) == 5
        assert all(col["total"] == 0 and col["cards"] == [] for col in columnas)


# ── Mensaje manual ───────────────────────────────────────────────────────────────────────────


class TestMensajeManual:
    async def test_envia_sin_pie_de_baja_y_deja_rastro_sin_texto(
        self, crm: Escenario, proveedor: ProveedorFalso
    ) -> None:
        lead = await crear_lead(crm.client_id, first_name="Ana", email="ana@x.com")
        async with _cliente(crm, "agent") as c:
            r = await c.post(
                f"{LEADS}/{lead}/message",
                json={"channel": "email", "text": "Hola Ana, ¿hablamos?", "subject": "Propuesta"},
            )

        assert r.status_code == 200, r.text
        canal, destino, texto, meta = proveedor.enviados[0]
        assert (canal, destino) == ("email", "ana@x.com")
        assert texto == "Hola Ana, ¿hablamos?"  # sin pie de baja: habla una persona
        assert meta == {"subject": "Propuesta", "new_thread": True}
        fila = await uno(
            crm.client_id,
            "SELECT user_id, metadata::text FROM lead_activities "
            "WHERE lead_id = :l AND activity_type = 'message_sent'",
            l=str(lead),
        )
        assert fila[0] == crm.user_id
        assert "Hola Ana" not in fila[1]
        assert "ana@x.com" not in fila[1]

    async def test_whatsapp_usa_el_telefono_en_digitos(
        self, crm: Escenario, proveedor: ProveedorFalso
    ) -> None:
        lead = await crear_lead(crm.client_id, first_name="Ana", phone="+57 300 111-2233")
        async with _cliente(crm, "admin") as c:
            r = await c.post(
                f"{LEADS}/{lead}/message", json={"channel": "whatsapp", "text": "Hola"}
            )

        assert r.status_code == 200
        assert proveedor.enviados[0][:2] == ("whatsapp", "573001112233")

    async def test_sin_destino_en_el_canal_es_400(
        self, crm: Escenario, proveedor: ProveedorFalso
    ) -> None:
        lead = await crear_lead(crm.client_id, first_name="Ana", email="ana@x.com")
        async with _cliente(crm, "admin") as c:
            r = await c.post(
                f"{LEADS}/{lead}/message", json={"channel": "whatsapp", "text": "Hola"}
            )

        assert r.status_code == 400
        assert proveedor.enviados == []

    async def test_quien_pidio_la_baja_no_recibe_mensajes_ni_manuales(
        self, crm: Escenario, proveedor: ProveedorFalso
    ) -> None:
        lead = await crear_lead(crm.client_id, first_name="Ana", email="ana@x.com")
        await sql(
            crm.client_id,
            "INSERT INTO lead_activities (client_id, lead_id, activity_type, metadata) "
            "VALUES (:c, :l, 'sequence_reply', CAST('{\"reply_kind\": \"unsubscribe\"}' AS jsonb))",
            c=str(crm.client_id),
            l=str(lead),
        )
        async with _cliente(crm, "admin") as c:
            r = await c.post(f"{LEADS}/{lead}/message", json={"channel": "email", "text": "Hola"})

        assert r.status_code == 409
        assert proveedor.enviados == []

    async def test_un_agente_no_escribe_a_leads_de_otro(
        self, crm: Escenario, proveedor: ProveedorFalso
    ) -> None:
        otro = await usuario(crm.client_id)
        ajeno = await crear_lead(
            crm.client_id, first_name="Ana", email="ana@x.com", assigned_user_id=otro
        )
        async with _cliente(crm, "agent") as c:
            r = await c.post(f"{LEADS}/{ajeno}/message", json={"channel": "email", "text": "Hola"})

        assert r.status_code == 403
        assert proveedor.enviados == []

    @pytest.mark.parametrize(
        ("estado_proveedor", "esperado", "codigo"),
        [(400, 422, "DELIVERY_REJECTED"), (503, 503, "PROVIDER_UNAVAILABLE")],
    )
    async def test_fallos_del_proveedor(
        self,
        crm: Escenario,
        proveedor: ProveedorFalso,
        estado_proveedor: int,
        esperado: int,
        codigo: str,
    ) -> None:
        peticion = httpx.Request("POST", "https://api.example.com")
        proveedor.falla = httpx.HTTPStatusError(
            "x", request=peticion, response=httpx.Response(estado_proveedor, request=peticion)
        )
        lead = await crear_lead(crm.client_id, first_name="Ana", email="ana@x.com")
        async with _cliente(crm, "admin") as c:
            r = await c.post(f"{LEADS}/{lead}/message", json={"channel": "email", "text": "Hola"})

        assert r.status_code == esperado
        assert r.json()["error_code"] == codigo
        # Un envio que fallo no queda como enviado en el historial.
        assert (
            await uno(
                crm.client_id,
                "SELECT count(*) FROM lead_activities WHERE lead_id = :l AND activity_type = 'message_sent'",
                l=str(lead),
            )
        ) == (0,)

    async def test_el_detalle_del_proveedor_no_llega_al_cliente(
        self, crm: Escenario, proveedor: ProveedorFalso
    ) -> None:
        proveedor.falla = ValueError("Destinatario invalido: ana@x.com")
        lead = await crear_lead(crm.client_id, first_name="Ana", email="ana@x.com")
        async with _cliente(crm, "admin") as c:
            r = await c.post(f"{LEADS}/{lead}/message", json={"channel": "email", "text": "Hola"})

        assert r.status_code == 422
        assert "ana@x.com" not in r.text

    async def test_limite_de_mensajes_por_usuario_y_hora(
        self, crm: Escenario, proveedor: ProveedorFalso, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(get_settings(), "LEAD_MANUAL_MESSAGES_PER_USER_PER_HOUR", 2)
        lead = await crear_lead(crm.client_id, first_name="Ana", email="ana@x.com")
        async with _cliente(crm, "admin") as c:
            estados = [
                (
                    await c.post(
                        f"{LEADS}/{lead}/message", json={"channel": "email", "text": "Hola"}
                    )
                ).status_code
                for _ in range(3)
            ]

        assert estados == [200, 200, 429]
        assert len(proveedor.enviados) == 2

    async def test_cuerpos_invalidos_son_422(
        self, crm: Escenario, proveedor: ProveedorFalso
    ) -> None:
        lead = await crear_lead(crm.client_id, first_name="Ana", email="ana@x.com")
        async with _cliente(crm, "admin") as c:
            malos = [
                await c.post(f"{LEADS}/{lead}/message", json={"channel": "sms", "text": "Hola"}),
                await c.post(f"{LEADS}/{lead}/message", json={"channel": "email", "text": ""}),
                await c.post(f"{LEADS}/{lead}/message", json={"channel": "email"}),
            ]
        assert [r.status_code for r in malos] == [422, 422, 422]
        assert proveedor.enviados == []
