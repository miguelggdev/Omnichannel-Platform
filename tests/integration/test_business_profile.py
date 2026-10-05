# ruff: noqa: F811
"""Perfil del negocio contra PostgreSQL real con RLS (Sprint 15, fase 2).

Requiere base de datos: `pytest tests/ --run-db`. Lo que los dobles no prueban: que cada campo
acabe en su columna (`clients.name`, `theme_config`, `settings`, `agent_configs`), que el merge
JSONB conserve lo que no se envia y que RLS impida tocar el negocio de otro tenant.
"""

import copy
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from app.core.database import tenant_session
from tests.integration.test_crm_api import (  # noqa: F401  (los fixtures se registran por nombre)
    Escenario,
    _cliente,
    dos_tenants,
    escenario,
)

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

URL = "/api/v1/admin/business-profile"


async def _con_agente(esc: Escenario) -> None:
    async with tenant_session(esc.client_id) as s:
        await s.execute(
            text(
                "INSERT INTO agent_configs (client_id, name, config, welcome_message) "
                "VALUES (:cid, 'asistente', '{\"enabled_agents\": [\"rag\"]}', 'Hola original')"
            ),
            {"cid": str(esc.client_id)},
        )


async def _fila(esc: Escenario, sql: str) -> Any:
    async with tenant_session(esc.client_id) as s:
        return (await s.execute(text(sql), {"cid": str(esc.client_id)})).mappings().one()


async def _quitar_agente(esc: Escenario) -> None:
    async with tenant_session(esc.client_id) as s:
        await s.execute(
            text("DELETE FROM agent_configs WHERE client_id = :cid"), {"cid": str(esc.client_id)}
        )


async def test_un_negocio_recien_creado_devuelve_valores_por_defecto(escenario: Escenario) -> None:
    async with _cliente(escenario, role="admin") as c:
        r = await c.get(URL)

    assert r.status_code == 200, r.text
    p = r.json()
    assert p["business_name"].startswith("Tenant crm-")
    assert p["has_agent"] is False
    assert p["welcome_message"] is None
    for campo in ("description", "phone", "email", "timezone", "primary_color", "logo_url"):
        assert p[campo] is None, campo
    assert p["operating_hours"]["monday"] == {
        "is_open": True,
        "open_time": "08:00",
        "close_time": "18:00",
    }
    assert p["operating_hours"]["sunday"]["is_open"] is False


async def test_cada_campo_se_guarda_donde_corresponde(escenario: Escenario) -> None:
    await _con_agente(escenario)
    try:
        async with _cliente(escenario, role="admin") as c:
            r = await c.put(
                URL,
                json={
                    "business_name": "Clinica Sol",
                    "description": "Medicina general",
                    "phone": "+57 300 123 4567",
                    "email": "hola@clinicasol.example.com",
                    "website": "https://clinicasol.example.com",
                    "country": "co",
                    "timezone": "America/Bogota",
                    "primary_color": "#1D4ED8",
                    "logo_url": "https://cdn.example.com/logo.png",
                    "welcome_message": "Bienvenido a Clinica Sol",
                    "handoff_message": "Te paso con una persona",
                    "social_media": {
                        "instagram": "@clinicasol",
                        "facebook": "https://facebook.com/clinicasol",
                    },
                },
            )

        assert r.status_code == 200, r.text
        p = r.json()
        assert p["business_name"] == "Clinica Sol"
        assert p["country"] == "CO"  # normalizado a mayusculas
        assert p["primary_color"] == "#1d4ed8"
        assert p["social_media"] == {
            "instagram": "clinicasol",  # sin @
            "facebook": "https://facebook.com/clinicasol",
        }
        assert p["has_agent"] is True

        assert (await _fila(escenario, "SELECT name FROM clients WHERE id = :cid"))[
            "name"
        ] == "Clinica Sol"
        tema = (await _fila(escenario, "SELECT theme_config AS t FROM clients WHERE id = :cid"))[
            "t"
        ]
        assert tema == {"primary_color": "#1d4ed8", "logo_url": "https://cdn.example.com/logo.png"}
        perfil = (
            await _fila(
                escenario, "SELECT settings -> 'business_profile' AS p FROM clients WHERE id = :cid"
            )
        )["p"]
        assert perfil["timezone"] == "America/Bogota"
        assert perfil["website"] == "https://clinicasol.example.com/"
        agente = await _fila(
            escenario,
            "SELECT welcome_message, handoff_message FROM agent_configs WHERE client_id = :cid",
        )
        assert dict(agente) == {
            "welcome_message": "Bienvenido a Clinica Sol",
            "handoff_message": "Te paso con una persona",
        }
    finally:
        await _quitar_agente(escenario)


async def test_un_put_parcial_no_toca_lo_que_no_se_envia(escenario: Escenario) -> None:
    async with _cliente(escenario, role="admin") as c:
        await c.put(
            URL, json={"phone": "+57 300 1", "city": "Cali", "social_media": {"instagram": "uno"}}
        )
        r = await c.put(URL, json={"city": "Bogota", "social_media": {"twitter": "dos"}})

    p = r.json()
    assert p["city"] == "Bogota"
    assert p["phone"] == "+57 300 1"  # intacto
    # Las redes se mezclan por clave: la de Instagram sigue ahi.
    assert p["social_media"] == {"instagram": "uno", "twitter": "dos"}


async def test_el_horario_se_cambia_dia_a_dia(escenario: Escenario) -> None:
    async with _cliente(escenario, role="admin") as c:
        await c.put(
            URL,
            json={
                "operating_hours": {
                    "saturday": {"is_open": True, "open_time": "09:00", "close_time": "13:00"}
                }
            },
        )
        r = await c.put(URL, json={"operating_hours": {"monday": {"is_open": False}}})

    h = r.json()["operating_hours"]
    assert h["monday"]["is_open"] is False
    assert h["saturday"] == {"is_open": True, "open_time": "09:00", "close_time": "13:00"}  # sigue
    assert h["tuesday"]["open_time"] == "08:00"  # los demas, por defecto


async def test_null_borra_un_campo_pero_no_el_nombre(escenario: Escenario) -> None:
    async with _cliente(escenario, role="admin") as c:
        await c.put(URL, json={"description": "algo", "primary_color": "#112233"})
        r = await c.put(URL, json={"description": None, "primary_color": None})
        sin_nombre = await c.put(URL, json={"business_name": None})

    assert r.json()["description"] is None
    assert r.json()["primary_color"] is None
    assert sin_nombre.status_code == 422


async def test_guardar_mensajes_sin_agente_es_409_y_no_cambia_nada(escenario: Escenario) -> None:
    async with _cliente(escenario, role="admin") as c:
        r = await c.put(URL, json={"welcome_message": "Hola", "city": "Cali"})
        despues = await c.get(URL)

    assert r.status_code == 409
    assert r.json()["error_code"] == "CONFLICT"
    assert despues.json()["city"] is None  # la peticion entera se rechazo


@pytest.mark.parametrize(
    "cuerpo",
    [
        {},
        {"primary_color": "azul"},
        {"primary_color": "#12345"},
        {"timezone": "Mars/Olympus"},
        {"country": "COL"},
        {"phone": "abc"},
        {"email": "no-es-correo"},
        {"website": "javascript:alert(1)"},
        {"logo_url": "ftp://x.example.com/a.png"},
        {
            "operating_hours": {
                "monday": {"is_open": True, "open_time": "18:00", "close_time": "08:00"}
            }
        },
        {
            "operating_hours": {
                "monday": {"is_open": True, "open_time": "25:00", "close_time": "26:00"}
            }
        },
        {
            "operating_hours": {
                "monday": {"is_open": True, "open_time": None, "close_time": "18:00"}
            }
        },
        {"social_media": {"instagram": "con espacios"}},
        {"description": "x" * 1001},
    ],
)
async def test_valores_invalidos_son_422(escenario: Escenario, cuerpo: dict[str, Any]) -> None:
    async with _cliente(escenario, role="admin") as c:
        r = await c.put(URL, json=cuerpo)

    assert r.status_code == 422, cuerpo


@pytest.mark.parametrize("rol", ["agent", "supervisor", "medical"])
async def test_solo_administradores(escenario: Escenario, rol: str) -> None:
    async with _cliente(escenario, role=rol) as c:
        assert (await c.get(URL)).status_code == 403
        assert (await c.put(URL, json={"city": "X"})).status_code == 403


async def test_un_tenant_no_toca_el_negocio_de_otro(
    dos_tenants: tuple[Escenario, Escenario],
) -> None:
    a, b = dos_tenants
    async with _cliente(a, role="admin") as c:
        await c.put(URL, json={"business_name": "Solo de A", "city": "Cali"})

    async with _cliente(b, role="admin") as c:
        pb = (await c.get(URL)).json()

    assert pb["business_name"] != "Solo de A"
    assert pb["city"] is None


# ── logo subido ─────────────────────────────────────────────────────────────────────────────

PNG = b"\x89PNG\r\n\x1a\n" + b"logo-uno" * 10
JPG = b"\xff\xd8\xff\xe0" + b"logo-dos" * 10
LOGO = f"{URL}/logo"


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    """Storage en memoria: lo que hay en el 'bucket' tras cada operacion."""
    objetos: dict[str, bytes] = {}

    async def subir(ruta: str, contenido: bytes, content_type: str) -> str:
        if ruta in objetos:  # Storage real rechaza sobrescribir
            raise RuntimeError("ya existe")
        objetos[ruta] = contenido
        return ruta

    async def bajar(ruta: str) -> bytes:
        return objetos[ruta]

    async def borrar(ruta: str) -> bool:
        return objetos.pop(ruta, None) is not None

    base = "app.api.v1.business_profile"
    monkeypatch.setattr(f"{base}.upload_to_storage", subir)
    monkeypatch.setattr(f"{base}.download_from_storage", bajar)
    monkeypatch.setattr(f"{base}.delete_from_storage", borrar)
    return objetos


def _archivo(contenido: bytes, nombre: str = "logo.png", mime: str = "image/png") -> dict[str, Any]:
    return {"file": (nombre, contenido, mime)}


async def test_subir_logo_lo_guarda_bajo_el_prefijo_del_tenant_y_se_descarga(
    escenario: Escenario, storage: dict[str, bytes]
) -> None:
    async with _cliente(escenario, role="admin") as c:
        r = await c.post(LOGO, files=_archivo(PNG))
        assert r.status_code == 200, r.text
        assert r.json()["logo_uploaded"] is True

        (ruta,) = storage
        assert ruta.startswith(f"{escenario.client_id}/branding/logo-")
        assert ruta.endswith(".png")
        fila = await _fila(escenario, "SELECT theme_config AS t FROM clients WHERE id = :cid")
        assert fila["t"]["logo_path"] == ruta
        assert fila["t"]["logo_content_type"] == "image/png"

        d = await c.get(LOGO)
        assert d.status_code == 200
        assert d.content == PNG
        assert d.headers["content-type"] == "image/png"
        assert d.headers["x-content-type-options"] == "nosniff"


async def test_reemplazar_el_logo_borra_el_anterior(
    escenario: Escenario, storage: dict[str, bytes]
) -> None:
    async with _cliente(escenario, role="admin") as c:
        await c.post(LOGO, files=_archivo(PNG))
        (primera,) = storage
        r = await c.post(LOGO, files=_archivo(JPG, "x.jpg", "image/jpeg"))
        assert r.status_code == 200
        assert len(storage) == 1
        assert primera not in storage
        (segunda,) = storage
        assert segunda.endswith(".jpg")
        assert (await c.get(LOGO)).content == JPG


async def test_el_tipo_lo_decide_el_contenido_no_el_content_type(
    escenario: Escenario, storage: dict[str, bytes]
) -> None:
    async with _cliente(escenario, role="admin") as c:
        html = await c.post(
            LOGO, files=_archivo(b"<html><script>alert(1)</script></html>", "logo.png", "image/png")
        )
        svg = await c.post(
            LOGO, files=_archivo(b"<svg><script>1</script></svg>", "logo.svg", "image/svg+xml")
        )
        vacio = await c.post(LOGO, files=_archivo(b""))
    assert (html.status_code, svg.status_code, vacio.status_code) == (415, 415, 400)
    assert storage == {}


async def test_un_logo_de_mas_de_512_kb_es_413_y_no_llega_a_storage(
    escenario: Escenario, storage: dict[str, bytes]
) -> None:
    async with _cliente(escenario, role="admin") as c:
        r = await c.post(LOGO, files=_archivo(PNG + b"\x00" * (512 * 1024)))
        justo = await c.post(LOGO, files=_archivo(PNG + b"\x00" * (512 * 1024 - len(PNG))))
    assert r.status_code == 413
    assert justo.status_code == 200
    assert len(storage) == 1


async def test_poner_una_url_externa_descarta_el_archivo_subido(
    escenario: Escenario, storage: dict[str, bytes]
) -> None:
    async with _cliente(escenario, role="admin") as c:
        await c.post(LOGO, files=_archivo(PNG))
        r = await c.put(URL, json={"logo_url": "https://cdn.example.com/logo.png"})
        assert r.status_code == 200, r.text
        assert r.json()["logo_uploaded"] is False
        assert r.json()["logo_url"] == "https://cdn.example.com/logo.png"
        assert storage == {}
        assert (await c.get(LOGO)).status_code == 404


async def test_subir_un_logo_sustituye_a_la_url_externa(
    escenario: Escenario, storage: dict[str, bytes]
) -> None:
    async with _cliente(escenario, role="admin") as c:
        await c.put(URL, json={"logo_url": "https://cdn.example.com/logo.png"})
        r = await c.post(LOGO, files=_archivo(PNG))
    assert r.json()["logo_url"] is None
    assert r.json()["logo_uploaded"] is True


async def test_otro_campo_del_perfil_no_borra_el_logo_subido(
    escenario: Escenario, storage: dict[str, bytes]
) -> None:
    async with _cliente(escenario, role="admin") as c:
        await c.post(LOGO, files=_archivo(PNG))
        r = await c.put(URL, json={"primary_color": "#112233"})
        assert r.json()["logo_uploaded"] is True
        assert (await c.get(LOGO)).content == PNG


async def test_quitar_el_logo_lo_borra_de_storage_y_es_idempotente(
    escenario: Escenario, storage: dict[str, bytes]
) -> None:
    async with _cliente(escenario, role="admin") as c:
        await c.post(LOGO, files=_archivo(PNG))
        r = await c.delete(LOGO)
        assert r.status_code == 200
        assert r.json()["logo_uploaded"] is False
        assert storage == {}
        assert (await c.delete(LOGO)).status_code == 200
        assert (await c.get(LOGO)).status_code == 404


async def test_cada_tenant_solo_ve_su_logo(
    dos_tenants: tuple[Escenario, Escenario], storage: dict[str, bytes]
) -> None:
    a, b = dos_tenants
    async with _cliente(a, role="admin") as ca, _cliente(b, role="admin") as cb:
        await ca.post(LOGO, files=_archivo(PNG))
        assert (await cb.get(LOGO)).status_code == 404
        assert (await cb.get(URL)).json()["logo_uploaded"] is False
        await cb.post(LOGO, files=_archivo(JPG, "b.jpg", "image/jpeg"))
        assert (await ca.get(LOGO)).content == PNG
        assert (await cb.get(LOGO)).content == JPG
        rutas = sorted(storage)
        assert rutas[0].startswith(str(min(a.client_id, b.client_id)))  # un prefijo por tenant


@pytest.mark.parametrize("role", ["supervisor", "agent", "medical"])
async def test_solo_admin_toca_el_logo(
    escenario: Escenario, storage: dict[str, bytes], role: str
) -> None:
    async with _cliente(escenario, role=role) as c:
        assert (await c.post(LOGO, files=_archivo(PNG))).status_code == 403
        assert (await c.get(LOGO)).status_code == 403
        assert (await c.delete(LOGO)).status_code == 403
    assert storage == {}


async def test_si_falla_guardar_la_referencia_no_queda_un_archivo_huerfano(
    escenario: Escenario, storage: dict[str, bytes]
) -> None:
    # Token de un tenant que no existe: la subida a Storage ocurre antes de descubrirlo.
    fantasma = copy.copy(escenario)
    fantasma.client_id = uuid.uuid4()
    async with _cliente(fantasma, role="admin") as c:
        r = await c.post(LOGO, files=_archivo(PNG))
    assert r.status_code == 404
    assert storage == {}
