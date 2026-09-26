"""Facturas y segmentacion contra PostgreSQL real (Sprint 12, BUG-044).

Lo que los dobles de sesion no pueden probar: que un importe de mas de
21,5 millones de pesos cabe en la columna (era `INTEGER`), que el consecutivo
sigue al mayor numero emitido aunque falten numeros, y que el filtro
`metadata` compara valores JSONB con su tipo.
"""

import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def tenant() -> AsyncGenerator[uuid.UUID, None]:
    """Crea un tenant commiteado y borra lo que siembren los tests."""
    from app.core.database import engine, tenant_session

    # Ver `webhook_tenant` en conftest.py: el pool puede traer conexiones
    # atadas al loop de un test anterior.
    await engine.dispose()

    client_id = uuid.uuid4()
    async with tenant_session(client_id) as session:
        await session.execute(
            text(
                "INSERT INTO clients (id, name, slug, plan, is_active) "
                "VALUES (:id, 'Tenant Facturas', :slug, 'free', true)"
            ),
            {"id": str(client_id), "slug": f"facturas-{client_id.hex[:8]}"},
        )

    yield client_id

    async with tenant_session(client_id) as session:
        for tabla in ("invoices", "contacts"):
            await session.execute(
                text(f"DELETE FROM {tabla} WHERE client_id = :cid"),  # noqa: S608
                {"cid": str(client_id)},
            )
        await session.execute(text("DELETE FROM clients WHERE id = :cid"), {"cid": str(client_id)})


def _config(client_id: uuid.UUID) -> dict[str, Any]:
    return {"configurable": {"client_id": str(client_id), "contact_id": None}}


async def _facturas(client_id: uuid.UUID) -> list[Any]:
    from app.core.database import tenant_session

    async with tenant_session(client_id) as session:
        return list(
            (
                await session.execute(
                    text(
                        "SELECT invoice_number, status, total_cents FROM invoices "
                        "WHERE client_id = :cid ORDER BY invoice_number"
                    ),
                    {"cid": str(client_id)},
                )
            ).all()
        )


async def test_una_factura_de_mas_de_21_millones_se_guarda(tenant: uuid.UUID) -> None:
    """En `INTEGER` de centavos el tope eran ~21,5 M COP."""
    from app.agents.tools import invoice_tools as it

    respuesta = await it.create_invoice.ainvoke(
        {
            "buyer_nit": "890903938",
            "buyer_name": "ACME SAS",
            "items": [
                {
                    "description": "Licencia anual",
                    "quantity": 1,
                    "unit_price": 50_000_000,
                    "tax_rate": 19,
                },
            ],
        },
        config=_config(tenant),
    )

    filas = await _facturas(tenant)
    assert len(filas) == 1, respuesta
    numero, estado, total = filas[0]
    assert numero == "FE-000001"
    # Sin credenciales de la DIAN en el entorno de tests: queda pendiente.
    assert estado == "pending_dian"
    assert total == 5_950_000_000


async def test_el_consecutivo_sigue_al_mayor_aunque_falten_numeros(tenant: uuid.UUID) -> None:
    """Con COUNT(*) + 1 saldria FE-000003, que ya existe: UNIQUE para siempre."""
    from app.agents.tools import invoice_tools as it
    from app.core.database import tenant_session
    from app.models.invoice import Invoice

    async with tenant_session(tenant) as session:
        for numero in ("FE-000001", "FE-000003"):
            session.add(
                Invoice(
                    client_id=tenant,
                    invoice_number=numero,
                    buyer_nit="890903938-8",
                    buyer_name="ACME SAS",
                    items=[],
                    subtotal_cents=100,
                    tax_total_cents=0,
                    total_cents=100,
                    status="approved",
                )
            )

    async with tenant_session(tenant) as session:
        assert await it._siguiente_consecutivo(session, tenant) == "FE-000004"


@pytest.mark.parametrize(
    ("guardado", "criterio", "entra"),
    [
        ({"vip": True}, {"vip": True}, True),
        ({"vip": True}, {"vip": False}, False),
        ({"nivel": 1}, {"nivel": 1.0}, True),
        ({"plan": "gold"}, {"plan": "gold"}, True),
        ({"plan": "gold"}, {"plan": "silver"}, False),
        # Escrito como texto por la API del CRM: el criterio escalar lo encuentra.
        ({"nivel": "5"}, {"nivel": 5}, True),
        ({"vip": "true"}, {"vip": True}, True),
        ({"vip": "false"}, {"vip": True}, False),
    ],
)
async def test_el_filtro_metadata_respeta_los_tipos_json(
    tenant: uuid.UUID, guardado: dict[str, Any], criterio: dict[str, Any], entra: bool
) -> None:
    """Antes `str(True)` = "True" no encontraba el `true` guardado en JSONB."""
    from app.core.database import tenant_session
    from app.models.contact import Contact
    from app.services.segmentation import contar_segmento

    async with tenant_session(tenant) as session:
        session.add(Contact(client_id=tenant, display_name="Ana", metadata_=guardado))

    async with tenant_session(tenant) as session:
        total = await contar_segmento(session, tenant, {"metadata": criterio})

    assert total == (1 if entra else 0)
