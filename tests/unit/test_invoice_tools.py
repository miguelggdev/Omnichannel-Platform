"""Tests de las tools de facturacion del agente financiero (Sprint 12)."""

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-testing-only-minimum-32-chars")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key-minimum-32-characters-long")

from app.agents.tools import invoice_tools as it
from app.agents.tools.invoice_tools import INVOICE_TOOLS, calcular_totales
from app.models.invoice import INVOICE_APPROVED, INVOICE_PENDING_DIAN, INVOICE_REJECTED
from app.services.dian import DianError, DianNoConfiguradaError

CLIENT_ID = str(uuid4())
CONTACT_ID = str(uuid4())
CONVERSATION_ID = str(uuid4())

CONFIG = {
    "configurable": {
        "client_id": CLIENT_ID,
        "contact_id": CONTACT_ID,
        "conversation_id": CONVERSATION_ID,
    }
}


class _Resultado:
    def __init__(self, filas: list) -> None:
        self._filas = filas

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._filas))

    def scalar_one(self):
        return self._filas[0]

    def scalar_one_or_none(self):
        return self._filas[0] if self._filas else None


class _SesionFalsa:
    def __init__(self, resultados: list[_Resultado] | None = None) -> None:
        self._resultados = list(resultados or [])
        self.added: list = []
        self.ejecutadas: list = []

    async def execute(self, stmt=None, params=None):
        self.ejecutadas.append(stmt)
        if self._resultados:
            return self._resultados.pop(0)
        return _Resultado([])

    def add(self, obj) -> None:
        self.added.append(obj)


def _sesion(sesion: _SesionFalsa):
    @asynccontextmanager
    async def _cm(client_id, user_id=None):
        yield sesion

    return _cm


def _factura(**over):
    base = {
        "invoice_number": "FE-000007",
        "buyer_name": "ACME SAS",
        "status": INVOICE_APPROVED,
        "total_cents": 119000,
        "currency": "COP",
        "issued_at": datetime(2026, 9, 24, tzinfo=timezone.utc),
        "dian_cufe": "cufe-123",
        "error_message": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


class TestCalcularTotales:
    def test_iva_del_19_por_ciento(self) -> None:
        """Criterio 3: items + IVA dan subtotal, impuestos y total verificables."""
        lineas, subtotal, impuestos, total = calcular_totales(
            [{"description": "Consultoria", "quantity": 2, "unit_price": 500000, "tax_rate": 19}]
        )

        assert subtotal == 100_000_000
        assert impuestos == 19_000_000
        assert total == 119_000_000
        assert lineas[0]["unit_price_cents"] == 50_000_000

    def test_mezcla_de_tasas(self) -> None:
        _, subtotal, impuestos, total = calcular_totales(
            [
                {"description": "Servicio", "quantity": 1, "unit_price": 100, "tax_rate": 19},
                {"description": "Libro", "quantity": 3, "unit_price": 50, "tax_rate": 0},
                {"description": "Medicina", "quantity": 2, "unit_price": 200, "tax_rate": 5},
            ]
        )

        assert subtotal == 10_000 + 15_000 + 40_000
        assert impuestos == 1_900 + 0 + 2_000
        assert total == subtotal + impuestos

    def test_el_total_cierra_al_centavo_con_decimales(self) -> None:
        """En flotante, 0.1 + 0.2 no es 0.3; en centavos enteros si cierra."""
        _, subtotal, impuestos, total = calcular_totales(
            [{"description": "Item", "quantity": 3, "unit_price": 0.1, "tax_rate": 19}]
        )

        assert subtotal == 30
        assert impuestos == 6
        assert total == 36

    def test_sin_tasa_de_iva_falla_en_vez_de_asumir_19(self) -> None:
        """El spec caia a 0.19 por defecto: una linea exenta salia con IVA."""
        with pytest.raises(ValueError, match="tasa de IVA"):
            calcular_totales([{"description": "Item", "quantity": 1, "unit_price": 100}])

    @pytest.mark.parametrize("tasa", [3, 16, 0.19, 21])
    def test_rechaza_tasas_que_la_dian_no_reconoce(self, tasa: float) -> None:
        with pytest.raises(ValueError, match="Tasa de IVA invalida"):
            calcular_totales(
                [{"description": "Item", "quantity": 1, "unit_price": 100, "tax_rate": tasa}]
            )

    @pytest.mark.parametrize(
        "item",
        [
            {"description": "", "quantity": 1, "unit_price": 10, "tax_rate": 0},
            {"description": "Item", "quantity": 0, "unit_price": 10, "tax_rate": 0},
            {"description": "Item", "quantity": 1, "unit_price": -10, "tax_rate": 0},
        ],
    )
    def test_rechaza_lineas_invalidas(self, item: dict) -> None:
        with pytest.raises(ValueError, match=r"descripcion|Cantidad|negativos"):
            calcular_totales([item])

    def test_sin_lineas_no_hay_factura(self) -> None:
        with pytest.raises(ValueError, match="al menos una linea"):
            calcular_totales([])


class TestContratoDeLasTools:
    def test_el_llm_no_ve_client_id_ni_contact_id(self) -> None:
        """Mismo contrato que calendar_tools: esos UUID los inyecta el nodo."""
        for herramienta in INVOICE_TOOLS:
            argumentos = set(herramienta.args)
            assert "config" not in argumentos
            assert "client_id" not in argumentos
            assert "contact_id" not in argumentos


class TestValidateNit:
    @pytest.mark.asyncio
    async def test_nit_valido_sin_dian_configurada(self, monkeypatch) -> None:
        """Criterio 2: NIT valido -> datos; sin DIAN, al menos el digito."""
        monkeypatch.setattr(it, "consultar_contribuyente", AsyncMock(return_value=None))

        respuesta = await it.validate_nit.ainvoke({"nit": "890903938"}, config=CONFIG)

        assert "890903938-8" in respuesta
        assert "No se pudo verificar en linea" in respuesta

    @pytest.mark.asyncio
    async def test_nit_invalido_da_un_error_claro(self) -> None:
        respuesta = await it.validate_nit.ainvoke({"nit": "abc"}, config=CONFIG)

        assert "NIT invalido" in respuesta

    @pytest.mark.asyncio
    async def test_con_dian_devuelve_la_razon_social(self, monkeypatch) -> None:
        monkeypatch.setattr(
            it,
            "consultar_contribuyente",
            AsyncMock(return_value={"razon_social": "ACME SAS", "regimen": "comun"}),
        )

        respuesta = await it.validate_nit.ainvoke({"nit": "890903938"}, config=CONFIG)

        assert "ACME SAS" in respuesta

    @pytest.mark.asyncio
    async def test_un_nit_que_la_dian_no_encuentra(self, monkeypatch) -> None:
        monkeypatch.setattr(it, "consultar_contribuyente", AsyncMock(return_value={}))

        respuesta = await it.validate_nit.ainvoke({"nit": "890903938"}, config=CONFIG)

        assert "no encuentra" in respuesta


class TestCreateInvoice:
    ITEMS: ClassVar[list[dict]] = [
        {"description": "Consultoria", "quantity": 1, "unit_price": 1000, "tax_rate": 19}
    ]

    @pytest.mark.asyncio
    async def test_aprobada_guarda_el_cufe(self, monkeypatch) -> None:
        sesion = _SesionFalsa([_Resultado([]), _Resultado([6])])
        monkeypatch.setattr(it, "tenant_session", _sesion(sesion))
        monkeypatch.setattr(it, "enviar_factura", AsyncMock(return_value={"cufe": "cufe-abc"}))

        respuesta = await it.create_invoice.ainvoke(
            {"buyer_nit": "890903938", "buyer_name": "ACME SAS", "items": self.ITEMS},
            config=CONFIG,
        )

        factura = sesion.added[0]
        assert factura.status == INVOICE_APPROVED
        assert factura.dian_cufe == "cufe-abc"
        assert factura.invoice_number == "FE-000007"
        assert factura.total_cents == 119_000
        assert factura.buyer_nit == "890903938-8"
        assert factura.contact_id == CONTACT_ID or str(factura.contact_id) == CONTACT_ID
        assert "FE-000007" in respuesta

    @pytest.mark.asyncio
    async def test_sin_dian_queda_pendiente_y_no_inventa_cufe(self, monkeypatch) -> None:
        sesion = _SesionFalsa([_Resultado([]), _Resultado([0])])
        monkeypatch.setattr(it, "tenant_session", _sesion(sesion))
        monkeypatch.setattr(
            it, "enviar_factura", AsyncMock(side_effect=DianNoConfiguradaError("sin credenciales"))
        )

        respuesta = await it.create_invoice.ainvoke(
            {"buyer_nit": "890903938", "buyer_name": "ACME SAS", "items": self.ITEMS},
            config=CONFIG,
        )

        factura = sesion.added[0]
        assert factura.status == INVOICE_PENDING_DIAN
        assert factura.dian_cufe is None
        assert "pendiente de validacion" in respuesta

    @pytest.mark.asyncio
    async def test_un_fallo_de_la_dian_deja_la_factura_pendiente(self, monkeypatch) -> None:
        sesion = _SesionFalsa([_Resultado([]), _Resultado([0])])
        monkeypatch.setattr(it, "tenant_session", _sesion(sesion))
        monkeypatch.setattr(it, "enviar_factura", AsyncMock(side_effect=DianError("timeout")))

        await it.create_invoice.ainvoke(
            {"buyer_nit": "890903938", "buyer_name": "ACME SAS", "items": self.ITEMS},
            config=CONFIG,
        )

        assert sesion.added[0].status == INVOICE_PENDING_DIAN

    @pytest.mark.asyncio
    async def test_sin_cufe_la_dian_la_rechazo(self, monkeypatch) -> None:
        sesion = _SesionFalsa([_Resultado([]), _Resultado([0])])
        monkeypatch.setattr(it, "tenant_session", _sesion(sesion))
        monkeypatch.setattr(
            it, "enviar_factura", AsyncMock(return_value={"error": "consecutivo agotado"})
        )

        respuesta = await it.create_invoice.ainvoke(
            {"buyer_nit": "890903938", "buyer_name": "ACME SAS", "items": self.ITEMS},
            config=CONFIG,
        )

        assert sesion.added[0].status == INVOICE_REJECTED
        assert "rechazo" in respuesta

    @pytest.mark.asyncio
    async def test_un_item_invalido_no_llega_a_la_dian(self, monkeypatch) -> None:
        enviada = AsyncMock()
        monkeypatch.setattr(it, "enviar_factura", enviada)
        sesion = _SesionFalsa()
        monkeypatch.setattr(it, "tenant_session", _sesion(sesion))

        respuesta = await it.create_invoice.ainvoke(
            {
                "buyer_nit": "890903938",
                "buyer_name": "ACME SAS",
                "items": [{"description": "X", "quantity": 1, "unit_price": 10, "tax_rate": 7}],
            },
            config=CONFIG,
        )

        assert "No se emitio la factura" in respuesta
        enviada.assert_not_awaited()
        assert sesion.added == []

    @pytest.mark.asyncio
    async def test_la_dian_se_consulta_antes_de_abrir_la_transaccion(self, monkeypatch) -> None:
        """Una llamada de red de hasta 10s no puede retener una conexion del pooler."""
        orden: list[str] = []
        sesion = _SesionFalsa([_Resultado([]), _Resultado([0])])

        @asynccontextmanager
        async def _cm(client_id, user_id=None):
            orden.append("transaccion")
            yield sesion

        async def _enviar(documento):
            orden.append("dian")
            return {"cufe": "x"}

        monkeypatch.setattr(it, "tenant_session", _cm)
        monkeypatch.setattr(it, "enviar_factura", _enviar)

        await it.create_invoice.ainvoke(
            {"buyer_nit": "890903938", "buyer_name": "ACME SAS", "items": self.ITEMS},
            config=CONFIG,
        )

        assert orden == ["dian", "transaccion"]


class TestConsultas:
    @pytest.mark.asyncio
    async def test_estado_de_una_factura(self, monkeypatch) -> None:
        sesion = _SesionFalsa([_Resultado([_factura()])])
        monkeypatch.setattr(it, "tenant_session", _sesion(sesion))

        respuesta = await it.get_invoice_status.ainvoke(
            {"invoice_number": "fe-000007"}, config=CONFIG
        )

        assert "FE-000007" in respuesta
        assert "cufe-123" in respuesta

    @pytest.mark.asyncio
    async def test_factura_inexistente(self, monkeypatch) -> None:
        monkeypatch.setattr(it, "tenant_session", _sesion(_SesionFalsa([_Resultado([])])))

        respuesta = await it.get_invoice_status.ainvoke(
            {"invoice_number": "FE-000099"}, config=CONFIG
        )

        assert "No encontre" in respuesta

    @pytest.mark.asyncio
    async def test_la_consulta_se_acota_al_contacto_de_la_conversacion(self, monkeypatch) -> None:
        """Un contacto no puede consultar las facturas de otro del mismo tenant."""
        sesion = _SesionFalsa([_Resultado([_factura()])])
        monkeypatch.setattr(it, "tenant_session", _sesion(sesion))

        await it.get_invoice_status.ainvoke({"invoice_number": "FE-000007"}, config=CONFIG)

        sql = str(sesion.ejecutadas[0].compile(compile_kwargs={"literal_binds": False}))
        assert "invoices.contact_id" in sql

    @pytest.mark.asyncio
    async def test_listado_vacio(self, monkeypatch) -> None:
        monkeypatch.setattr(it, "tenant_session", _sesion(_SesionFalsa([_Resultado([])])))

        respuesta = await it.list_invoices.ainvoke({}, config=CONFIG)

        assert "No hay facturas" in respuesta

    @pytest.mark.asyncio
    async def test_listado_con_facturas(self, monkeypatch) -> None:
        sesion = _SesionFalsa([_Resultado([_factura(), _factura(invoice_number="FE-000008")])])
        monkeypatch.setattr(it, "tenant_session", _sesion(sesion))

        respuesta = await it.list_invoices.ainvoke({}, config=CONFIG)

        assert "FE-000007" in respuesta
        assert "FE-000008" in respuesta

    @pytest.mark.asyncio
    async def test_estado_invalido_en_el_filtro(self, monkeypatch) -> None:
        monkeypatch.setattr(it, "tenant_session", _sesion(_SesionFalsa()))

        respuesta = await it.list_invoices.ainvoke({"status": "pagada"}, config=CONFIG)

        assert "Estado invalido" in respuesta

    @pytest.mark.asyncio
    async def test_fecha_invalida_en_el_filtro(self, monkeypatch) -> None:
        monkeypatch.setattr(it, "tenant_session", _sesion(_SesionFalsa()))

        respuesta = await it.list_invoices.ainvoke({"date_from": "24/09/2026"}, config=CONFIG)

        assert "invalida" in respuesta
