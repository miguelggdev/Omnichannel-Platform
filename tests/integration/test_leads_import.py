# ruff: noqa: F811
"""Importacion de leads por CSV contra PostgreSQL real con RLS (Sprint 16, ADR-083).

Requiere base de datos: `pytest tests/ --run-db`. Lo que un doble no prueba: que las filas buenas
entren aunque haya malas, que duplicados contra la base y dentro del archivo se omitan sin tocar
al lead existente, que `dry_run` no deje rastro (ni la fuente), que la carrera con otra alta no
pierda las filas buenas, y que ni el informe ni el log repitan datos personales.
"""

from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.database import tenant_session
from tests.integration.lead_helpers import (
    activar_modulo,
    crear_etapa,
    crear_fuente,
    crear_lead,
    limpiar_leads,
)
from tests.integration.test_crm_api import Escenario, _cliente, dos_tenants, escenario  # noqa: F401

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

IMPORTAR = "/api/v1/leads/import"


@pytest_asyncio.fixture
async def crm(escenario: Escenario) -> AsyncGenerator[Escenario, None]:
    await activar_modulo(escenario.client_id)
    await crear_etapa(escenario.client_id, "new", 0)
    yield escenario
    await limpiar_leads(escenario.client_id)


@pytest_asyncio.fixture
async def crm_dos(
    dos_tenants: tuple[Escenario, Escenario],
) -> AsyncGenerator[tuple[Escenario, Escenario], None]:
    for e in dos_tenants:
        await activar_modulo(e.client_id)
        await crear_etapa(e.client_id, "new", 0)
    yield dos_tenants
    for e in dos_tenants:
        await limpiar_leads(e.client_id)


def _archivo(texto: str | bytes, codificacion: str = "utf-8") -> dict[str, Any]:
    contenido = texto if isinstance(texto, bytes) else texto.encode(codificacion)
    return {"file": ("leads.csv", contenido, "text/csv")}


async def _contar(esc: Escenario, sql: str = "SELECT count(*) AS n FROM leads") -> int:
    async with tenant_session(esc.client_id) as s:
        return int((await s.execute(text(sql))).scalar_one())


CSV_BUENO = (
    "Nombre,Apellido,Correo,Celular,Empresa,Cargo\n"
    "Ana,Gomez,ana@example.com,+57 300 111-2233,ACME,CTO\n"
    "Beto,Ruiz,beto@example.com,,Beta,CEO\n"
    "Cata,Diaz,,+57 311 000-0000,Gamma,\n"
)


class TestImportar:
    async def test_importa_las_filas_con_fuente_etapa_y_lote(self, crm: Escenario) -> None:
        async with _cliente(crm, "admin") as c:
            r = await c.post(IMPORTAR, files=_archivo(CSV_BUENO))
        assert r.status_code == 200, r.text
        cuerpo = r.json()
        assert (
            cuerpo["total_rows"],
            cuerpo["imported"],
            cuerpo["duplicates"],
            cuerpo["invalid"],
        ) == (3, 3, 0, 0)
        assert (cuerpo["errors"], cuerpo["ignored_columns"], cuerpo["dry_run"]) == ([], [], False)
        async with tenant_session(crm.client_id) as s:
            filas = (
                await s.execute(
                    text(
                        "SELECT l.first_name, l.company_name, l.enrichment_data, l.email::text AS e, "
                        "s.source_type, s.id AS fuente, st.slug "
                        "FROM leads l JOIN lead_sources s ON s.id = l.source_id "
                        "JOIN lead_pipeline_stages st ON st.id = l.pipeline_stage_id "
                        "ORDER BY l.first_name"
                    )
                )
            ).all()
        assert [f.first_name for f in filas] == ["Ana", "Beto", "Cata"]
        assert {f.source_type for f in filas} == {"import"}
        assert str(filas[0].fuente) == cuerpo["source_id"]
        assert {f.slug for f in filas} == {"new"}
        lotes = {f.enrichment_data["import"]["batch_id"] for f in filas}
        assert len(lotes) == 1  # un lote por importacion
        assert "ana@example.com" not in (filas[0].e or "")  # cifrado

    async def test_la_fuente_de_importacion_se_reutiliza(self, crm: Escenario) -> None:
        async with _cliente(crm, "admin") as c:
            a = (await c.post(IMPORTAR, files=_archivo("email\na@example.com\n"))).json()
            b = (await c.post(IMPORTAR, files=_archivo("email\nb@example.com\n"))).json()
        assert a["source_id"] == b["source_id"]
        assert (
            await _contar(crm, "SELECT count(*) FROM lead_sources WHERE source_type = 'import'")
            == 1
        )

    async def test_las_filas_malas_se_informan_y_las_buenas_entran(self, crm: Escenario) -> None:
        await crear_lead(crm.client_id, email="existente@example.com", first_name="Original")
        csv_texto = (
            "nombre,email,telefono,color\n"
            "Ana,ana@example.com,,rojo\n"  # 2: buena
            "Beto,DATOS-PRIVADOS-DE-BETO,,rojo\n"  # 3: email invalido
            "Cata,existente@example.com,,rojo\n"  # 4: ya existe en la base
            "Dani,ana@example.com,,rojo\n"  # 5: repetida en el archivo
            "Eva,,TELEFONO-SECRETO,rojo\n"  # 6: telefono invalido
            "Fer,,,rojo\n"  # 7: sin contacto
            "Gus,gus@example.com,+57 300 999-0000,rojo\n"  # 8: buena
        )
        async with _cliente(crm, "supervisor") as c:
            r = await c.post(IMPORTAR, files=_archivo(csv_texto))
        assert r.status_code == 200, r.text
        cuerpo = r.json()
        assert (
            cuerpo["total_rows"],
            cuerpo["imported"],
            cuerpo["duplicates"],
            cuerpo["invalid"],
        ) == (7, 2, 2, 3)
        assert cuerpo["ignored_columns"] == ["color"]
        por_fila = {e["row"]: e["error"] for e in cuerpo["errors"]}
        assert por_fila == {
            3: "email no es valido",
            4: "ya existe un lead con ese email, telefono o LinkedIn",
            5: "repetida en el archivo",
            6: "phone no es valido",
            7: "falta email, telefono o LinkedIn",
        }
        assert [e["row"] for e in cuerpo["errors"]] == sorted(por_fila)  # ordenadas
        assert "DATOS-PRIVADOS" not in r.text
        assert "SECRETO" not in r.text
        assert await _contar(crm) == 3  # el existente + 2 nuevos
        async with tenant_session(crm.client_id) as s:
            nombre = (
                await s.execute(text("SELECT first_name FROM leads WHERE first_name = 'Original'"))
            ).scalar_one()
        assert nombre == "Original"  # el existente no se toco

    async def test_dry_run_no_deja_rastro_ni_crea_la_fuente(self, crm: Escenario) -> None:
        await crear_lead(crm.client_id, email="ana@example.com")
        csv_texto = "email\nana@example.com\nnuevo@example.com\nmalo\n"
        async with _cliente(crm, "admin") as c:
            prueba = (
                await c.post(IMPORTAR, files=_archivo(csv_texto), data={"dry_run": "true"})
            ).json()
            assert await _contar(crm) == 1
            assert await _contar(crm, "SELECT count(*) FROM lead_sources") == 0
            real = (await c.post(IMPORTAR, files=_archivo(csv_texto))).json()
        assert prueba["dry_run"] is True
        assert prueba["source_id"] is None
        for campo in ("total_rows", "imported", "duplicates", "invalid", "errors"):
            assert prueba[campo] == real[campo], campo
        assert await _contar(crm) == 2

    async def test_importar_el_mismo_archivo_dos_veces_es_idempotente(self, crm: Escenario) -> None:
        async with _cliente(crm, "admin") as c:
            await c.post(IMPORTAR, files=_archivo(CSV_BUENO))
            otra = (await c.post(IMPORTAR, files=_archivo(CSV_BUENO))).json()
        assert (otra["imported"], otra["duplicates"]) == (0, 3)
        assert await _contar(crm) == 3

    async def test_fuente_etapa_y_responsable_indicados_se_aplican_y_se_validan(
        self, crm_dos: tuple[Escenario, Escenario]
    ) -> None:
        a, b = crm_dos
        fuente = await crear_fuente(a.client_id, name="Feria", source_type="referral")
        etapa = await crear_etapa(a.client_id, "calificado", 5)
        async with _cliente(a, "admin") as c:
            r = await c.post(
                IMPORTAR,
                files=_archivo("email\nana@example.com\n"),
                data={
                    "source_id": str(fuente),
                    "stage_id": str(etapa),
                    "assigned_user_id": str(a.user_id),
                },
            )
            assert r.status_code == 200, r.text
            assert r.json()["source_id"] == str(fuente)
            f = await _contar(
                a,
                f"SELECT count(*) FROM leads WHERE source_id = '{fuente}' "  # noqa: S608
                f"AND pipeline_stage_id = '{etapa}' AND assigned_user_id = '{a.user_id}'",
            )
            assert f == 1
            for campo, valor in (
                ("source_id", await crear_fuente(b.client_id)),
                ("stage_id", await crear_etapa(b.client_id, "ajena", 9)),
                ("assigned_user_id", b.user_id),
            ):
                r = await c.post(
                    IMPORTAR, files=_archivo("email\notro@example.com\n"), data={campo: str(valor)}
                )
                assert r.status_code == 400, campo
        assert await _contar(a) == 1

    async def test_un_email_de_otro_tenant_no_cuenta_como_duplicado(
        self, crm_dos: tuple[Escenario, Escenario]
    ) -> None:
        a, b = crm_dos
        await crear_lead(b.client_id, email="ana@example.com")
        async with _cliente(a, "admin") as c:
            r = await c.post(IMPORTAR, files=_archivo("email\nana@example.com\n"))
        assert r.json()["imported"] == 1
        assert await _contar(b) == 1

    async def test_utf8_latin1_y_punto_y_coma(self, crm: Escenario) -> None:
        async with _cliente(crm, "admin") as c:
            r = await c.post(
                IMPORTAR, files=_archivo("nombre;email\nJosé;jose@example.com\n", "latin-1")
            )
        assert r.json()["imported"] == 1
        async with tenant_session(crm.client_id) as s:
            assert (await s.execute(text("SELECT first_name FROM leads"))).scalar_one() == "José"

    async def test_sin_pipeline_es_409(self, escenario: Escenario) -> None:
        await activar_modulo(escenario.client_id)
        try:
            async with _cliente(escenario, "admin") as c:
                r = await c.post(IMPORTAR, files=_archivo("email\na@example.com\n"))
            assert r.status_code == 409
        finally:
            await limpiar_leads(escenario.client_id)

    async def test_si_otra_alta_se_cuela_entre_la_comprobacion_y_el_insert_no_se_pierden_las_buenas(
        self, crm: Escenario, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await crear_lead(crm.client_id, email="x@example.com", first_name="Original")

        async def ciega(*_a: Any, **_k: Any) -> tuple[set[str], set[str], set[str]]:
            return set(), set(), set()

        monkeypatch.setattr("app.services.lead_batch._existentes", ciega)
        async with _cliente(crm, "admin") as c:
            r = await c.post(
                IMPORTAR, files=_archivo("email\nx@example.com\ny@example.com\nz@example.com\n")
            )
        assert r.status_code == 200, r.text
        cuerpo = r.json()
        assert (cuerpo["imported"], cuerpo["duplicates"]) == (2, 1)
        assert [e["row"] for e in cuerpo["errors"]] == [2]
        assert await _contar(crm) == 3


class TestLimitesYRoles:
    @pytest.mark.parametrize(
        "contenido",
        [b"", b"email\n", b"foo,bar\n1,2\n", b"nombre,empresa\nAna,ACME\n"],
    )
    async def test_archivos_inutilizables_son_400(self, crm: Escenario, contenido: bytes) -> None:
        async with _cliente(crm, "admin") as c:
            r = await c.post(IMPORTAR, files=_archivo(contenido))
        assert r.status_code == 400, r.text

    async def test_mas_de_2_mb_es_413_y_mas_de_5000_filas_400(self, crm: Escenario) -> None:
        async with _cliente(crm, "admin") as c:
            grande = await c.post(
                IMPORTAR, files=_archivo(b"email\n" + b"a" * (2 * 1024 * 1024 + 1))
            )
            muchas = await c.post(
                IMPORTAR,
                files=_archivo("email\n" + "".join(f"u{i}@example.com\n" for i in range(5001))),
            )
            sin_archivo = await c.post(IMPORTAR, data={"dry_run": "true"})
        assert (grande.status_code, muchas.status_code, sin_archivo.status_code) == (413, 400, 422)
        assert await _contar(crm) == 0

    async def test_el_informe_acota_los_errores_a_100(self, crm: Escenario) -> None:
        malas = "email\n" + "".join(f"malo{i}\n" for i in range(150))
        async with _cliente(crm, "admin") as c:
            r = await c.post(IMPORTAR, files=_archivo(malas + "bueno@example.com\n"))
        cuerpo = r.json()
        assert (cuerpo["invalid"], cuerpo["imported"]) == (150, 1)
        assert len(cuerpo["errors"]) == 100
        assert cuerpo["errors_truncated"] is True

    async def test_una_importacion_grande_entra_entera(self, crm: Escenario) -> None:
        cuerpo = "email,nombre\n" + "".join(f"u{i}@example.com,U{i}\n" for i in range(1500))
        async with _cliente(crm, "admin") as c:
            r = await c.post(IMPORTAR, files=_archivo(cuerpo))
        assert r.json()["imported"] == 1500
        assert await _contar(crm) == 1500

    @pytest.mark.parametrize("rol", ["agent", "medical"])
    async def test_solo_admin_y_supervisor_importan(self, crm: Escenario, rol: str) -> None:
        async with _cliente(crm, rol) as c:
            r = await c.post(IMPORTAR, files=_archivo("email\na@example.com\n"))
        assert r.status_code == 403
        assert await _contar(crm) == 0

    async def test_con_el_modulo_apagado_es_403(self, crm: Escenario) -> None:
        await activar_modulo(crm.client_id, False)
        async with _cliente(crm, "admin") as c:
            r = await c.post(IMPORTAR, files=_archivo("email\na@example.com\n"))
        assert r.status_code == 403
        assert r.json()["error_code"] == "MODULE_DISABLED"

    async def test_el_log_no_repite_datos_personales(
        self, crm: Escenario, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        with caplog.at_level(logging.DEBUG):
            async with _cliente(crm, "admin") as c:
                await c.post(
                    IMPORTAR,
                    files=_archivo(
                        "nombre,email\nPersona Secreta,secreto-x@example.com\nmal,no-email\n"
                    ),
                )
        registro = caplog.text
        assert "secreto-x@example.com" not in registro
        assert "Persona Secreta" not in registro
        assert "no-email" not in registro
