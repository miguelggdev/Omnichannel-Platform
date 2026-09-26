"""Tools de facturacion electronica que usa el agente financiero (Sprint 12).

Mismo contrato de seguridad que `calendar_tools.py`: `client_id`,
`conversation_id` y `contact_id` llegan por `config["configurable"]` y nunca
como argumentos que el LLM pueda rellenar — el modelo no tiene ninguna fuente
legitima de esos UUID internos, y dejarlos como argumentos abriria la puerta a
que el bot facturara a nombre de otro contacto del mismo tenant.

Cambios sobre el pseudocodigo del spec (§1.2), todos en ADR-067:

- **Los importes se calculan en centavos enteros.** El spec suma flotantes
  (`quantity * unit_price`) y con IVA del 19% sobre varias lineas el total no
  cierra al centavo.
- **El IVA no cae a 0.19 por defecto.** El spec usa `item.get("tax_rate", 0.19)`
  al sumar impuestos pero no al validar: una linea exenta sin `tax_rate`
  terminaba con 19% de IVA. Aca la tasa es obligatoria y solo admite las tres
  que reconoce la DIAN (0, 5, 19).
- **El consecutivo es del tenant.** El spec deja `generate_invoice_number()`
  sin definir; aca es `FE-000001` por tenant, calculado dentro de la misma
  transaccion que inserta, con el `UNIQUE (client_id, invoice_number)` de la
  migracion 012 como red.
- **El numero se reserva antes de ir a la DIAN.** La DIAN exige prefijo y
  consecutivo en el documento (el CUFE se calcula con ellos), asi que
  `create_invoice()` primero inserta la factura en `pending_dian` con su
  numero, despues la envia y por ultimo guarda la respuesta (BUG-044).
- **Sin CUFE inventado.** Si la DIAN no esta configurada o falla, la factura
  queda en `pending_dian` con el motivo; nunca se marca `approved`.
"""

import logging
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from sqlalchemy import BigInteger, func, select, text
from sqlalchemy import update as sa_update

from app.core.database import tenant_session
from app.models.invoice import (
    INVOICE_APPROVED,
    INVOICE_PENDING_DIAN,
    INVOICE_REJECTED,
    INVOICE_STATUSES,
    Invoice,
)
from app.services.dian import (
    DianError,
    NitInvalidoError,
    calcular_digito_verificacion,
    consultar_contribuyente,
    enviar_factura,
    normalizar_nit,
)

logger = logging.getLogger(__name__)

# Tasas de IVA que reconoce la DIAN, en porcentaje.
TASAS_IVA_VALIDAS: tuple[float, ...] = (0.0, 5.0, 19.0)

#: Respuesta cuando la conversacion no tiene contacto identificado.
#:
#: `get_invoice_status()`/`list_invoices()` filtraban por contacto solo si
#: `contact_id` venia en el config (`if contact_id is not None`), asi que una
#: conversacion sin contacto — un canal anonimo, un webchat antes de
#: identificarse — dejaba las dos tools con alcance de TENANT: el agente
#: listaba razon social, NIT y totales de los clientes de la empresa a quien
#: estuviera del otro lado. El modulo dice seguir "el mismo contrato de
#: seguridad que calendar_tools.py", pero alli `_contact_id()` es obligatorio
#: y falla si no esta. Ahora el filtro es incondicional y sin contacto no se
#: consulta nada. Hallazgo de /code-review sobre el PR #42.
SIN_CONTACTO = (
    "No puedo consultar facturas en esta conversacion porque todavia no se "
    "identifico al contacto. Pidele sus datos para poder ubicarlas."
)

PREFIJO_CONSECUTIVO = "FE-"
DIGITOS_CONSECUTIVO = 6


def _client_id(config: RunnableConfig) -> UUID:
    """Extrae el `client_id` inyectado por el nodo, nunca provisto por el LLM.

    Args:
        config: Config que inyecta LangChain desde `.ainvoke(..., config=...)`.

    Returns:
        UUID del tenant dueno de la conversacion.
    """
    return UUID(config["configurable"]["client_id"])


def _contact_id(config: RunnableConfig) -> UUID | None:
    """Extrae el `contact_id` de la conversacion en curso.

    Args:
        config: Config que inyecta LangChain.

    Returns:
        UUID del contacto, o None si la conversacion no lo tiene.
    """
    valor = config.get("configurable", {}).get("contact_id")
    return UUID(valor) if valor else None


def _contact_id_o_mensaje(config: RunnableConfig) -> UUID | str:
    """Como `_contact_id()`, pero exige que haya contacto o corta con `SIN_CONTACTO`.

    Punto unico del guard "sin contacto identificado, no se consulta nada"
    para las tools que solo pueden operar sobre un contacto conocido
    (`get_invoice_status()`, `list_invoices()`) — a diferencia de
    `create_invoice()`, donde el contacto es opcional (una factura puede
    emitirse sin contacto vinculado en el CRM), asi que `_contact_id()` no
    puede exigirlo incondicionalmente como en `calendar_tools.py`. Antes el
    `if contact_id is None: return SIN_CONTACTO` vivia duplicado en cada tool;
    una tool nueva que reusara `_contact_id()` sin copiar ese guard
    reintroduciria el mismo alcance-de-tenant que este modulo cerro. Hallazgo
    de /code-review sobre el PR #46.

    Args:
        config: Config que inyecta LangChain.

    Returns:
        El UUID del contacto, o el string `SIN_CONTACTO` si no hay ninguno.
    """
    contact_id = _contact_id(config)
    return contact_id if contact_id is not None else SIN_CONTACTO


def _conversation_id(config: RunnableConfig) -> UUID | None:
    """Extrae el `conversation_id` de la conversacion en curso.

    Args:
        config: Config que inyecta LangChain.

    Returns:
        UUID de la conversacion, o None si no viene.
    """
    valor = config.get("configurable", {}).get("conversation_id")
    return UUID(valor) if valor else None


def _a_centavos(valor: Any) -> int:
    """Convierte un importe en pesos a centavos enteros.

    Args:
        valor: Importe en la moneda de la factura.

    Returns:
        El importe en centavos, redondeado al centavo mas cercano.

    Raises:
        ValueError: Si no es un numero finito o es negativo.
    """
    if isinstance(valor, bool):
        raise ValueError("El precio tiene que ser un numero")
    numero = float(valor)
    # `float()` acepta "inf" y "nan": `round(inf)` levanta OverflowError, que
    # no es ValueError y tumbaba la tool en vez de devolver un mensaje.
    if not math.isfinite(numero):
        raise ValueError("El precio tiene que ser un numero finito")
    if numero < 0:
        raise ValueError("Los importes no pueden ser negativos")
    return round(numero * 100)


def _cantidad(valor: Any, descripcion: str) -> int:
    """Valida la cantidad de una linea: un entero de 1 o mas.

    `int(2.9)` da 2 sin avisar, asi que una cantidad fraccionaria facturaba
    menos unidades de las pedidas. Se acepta `2`, `2.0` o `"2"`, y se rechaza
    `2.9` en vez de truncarla.

    Args:
        valor: Cantidad tal como llego del LLM.
        descripcion: Descripcion de la linea, para el mensaje de error.

    Returns:
        La cantidad como entero.

    Raises:
        ValueError: Si no es un entero de 1 o mas.
    """
    error = ValueError(f"Cantidad invalida en '{descripcion}': debe ser un entero de 1 o mas")
    if isinstance(valor, bool):
        raise error
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        raise error from None
    if not math.isfinite(numero) or not numero.is_integer() or numero < 1:
        raise error
    return int(numero)


def calcular_totales(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, int, int]:
    """Calcula las lineas y los totales de una factura, en centavos.

    Args:
        items: Lineas `{description, quantity, unit_price, tax_rate}`. La tasa
            va en porcentaje (0, 5 o 19), no en fraccion.

    Returns:
        `(lineas_calculadas, subtotal_cents, tax_total_cents, total_cents)`.

    Raises:
        ValueError: Si falta un campo, la cantidad no es positiva o la tasa de
            IVA no es una de las que reconoce la DIAN.
    """
    if not items:
        raise ValueError("La factura necesita al menos una linea")

    lineas: list[dict[str, Any]] = []
    subtotal = 0
    impuestos = 0

    for item in items:
        descripcion = str(item.get("description") or "").strip()
        if not descripcion:
            raise ValueError("Cada linea necesita una descripcion")

        cantidad = _cantidad(item.get("quantity", 0), descripcion)

        if "tax_rate" not in item or item["tax_rate"] is None:
            raise ValueError(
                f"Falta la tasa de IVA en '{descripcion}'. Validas: 0, 5 o 19 por ciento"
            )
        tasa = float(item["tax_rate"])
        if tasa not in TASAS_IVA_VALIDAS:
            raise ValueError(
                f"Tasa de IVA invalida en '{descripcion}': {tasa}. Validas: 0, 5 o 19 por ciento"
            )

        precio_unitario = _a_centavos(item.get("unit_price"))
        base = precio_unitario * cantidad
        iva = round(base * tasa / 100)

        subtotal += base
        impuestos += iva
        lineas.append(
            {
                "description": descripcion,
                "quantity": cantidad,
                "unit_price_cents": precio_unitario,
                "tax_rate": tasa,
                "subtotal_cents": base,
                "tax_cents": iva,
            }
        )

    return lineas, subtotal, impuestos, subtotal + impuestos


def _formatear_pesos(centavos: int) -> str:
    """Formatea un importe en centavos para mostrarselo a una persona.

    Args:
        centavos: Importe en centavos.

    Returns:
        El importe con separador de miles y dos decimales.
    """
    return f"{centavos / 100:,.2f}"


async def _siguiente_consecutivo(session: Any, client_id: UUID) -> str:
    """Calcula el siguiente numero de factura del tenant.

    `pg_advisory_xact_lock` antes de leer el ultimo numero, no solo el `UNIQUE`
    de la migracion 012 como red: sin el lock, dos `create_invoice` casi
    simultaneas del mismo tenant leen el mismo ultimo numero antes de que
    ninguna inserte y la segunda revienta contra el `UNIQUE`. El lock es por
    tenant (`hashtext(client_id)`) y de alcance transaccional: se libera solo
    al terminar la transaccion, mismo patron que el doble-booking de citas
    (`calendar_tools.create_appointment`). Hallazgo de /code-review sobre el
    PR #42.

    El siguiente es el **mayor numero emitido + 1**, no `COUNT(*) + 1`: con el
    conteo, una sola factura borrada (o importada con otro numero) hacia que
    el calculado ya existiera, y desde ahi cada factura nueva del tenant
    chocaba contra el `UNIQUE` para siempre (BUG-044). El numero se extrae y
    se compara como entero, no como texto: `FE-1000000` ordena antes que
    `FE-999999` lexicograficamente.

    Args:
        session: Sesion con el contexto de tenant ya aplicado.
        client_id: Tenant que emite.

    Returns:
        Consecutivo con el formato `FE-000001`.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:client_id))"), {"client_id": str(client_id)}
    )
    numero = func.substring(Invoice.invoice_number, r"^FE-([0-9]+)$").cast(BigInteger)
    ultimo = (
        await session.execute(
            select(func.coalesce(func.max(numero), 0)).where(
                Invoice.client_id == client_id,
                Invoice.invoice_number.like(f"{PREFIJO_CONSECUTIVO}%"),
            )
        )
    ).scalar_one()
    return f"{PREFIJO_CONSECUTIVO}{int(ultimo) + 1:0{DIGITOS_CONSECUTIVO}d}"


@tool(parse_docstring=True)
async def validate_nit(nit: str, config: RunnableConfig) -> str:
    """Valida un NIT o cedula colombiana y devuelve los datos del contribuyente.

    Args:
        nit: Numero de identificacion tributaria, con o sin puntos y sin el
            digito de verificacion.
    """
    try:
        limpio = normalizar_nit(nit)
    except NitInvalidoError as exc:
        return f"NIT invalido: {exc}"

    digito = calcular_digito_verificacion(limpio)
    contribuyente = await consultar_contribuyente(limpio)

    if contribuyente is None:
        return (
            f"NIT {limpio}-{digito} con formato valido (digito de verificacion {digito}). "
            "No se pudo verificar en linea contra la DIAN; confirma la razon social con el cliente."
        )
    if not contribuyente:
        return f"La DIAN no encuentra el NIT {limpio}-{digito}. Verifica el numero con el cliente."

    partes = [f"NIT {limpio}-{digito} valido."]
    if contribuyente.get("razon_social"):
        partes.append(f"Razon social: {contribuyente['razon_social']}.")
    if contribuyente.get("regimen"):
        partes.append(f"Regimen: {contribuyente['regimen']}.")
    if contribuyente.get("ciudad"):
        partes.append(f"Ciudad: {contribuyente['ciudad']}.")
    return " ".join(partes)


@tool(parse_docstring=True)
async def create_invoice(
    buyer_nit: str,
    buyer_name: str,
    items: list[dict[str, Any]],
    config: RunnableConfig,
) -> str:
    """Emite una factura electronica para el contacto de esta conversacion.

    Args:
        buyer_nit: NIT o cedula del comprador.
        buyer_name: Razon social o nombre completo del comprador.
        items: Lineas de la factura. Cada linea necesita description con el
            texto del producto o servicio, quantity como entero de 1 o mas,
            unit_price con el precio unitario en pesos, y tax_rate con el IVA
            en porcentaje, que solo puede ser 0, 5 o 19.
    """
    client_id = _client_id(config)

    try:
        nit_limpio = normalizar_nit(buyer_nit)
    except NitInvalidoError as exc:
        return f"No se emitio la factura: NIT invalido ({exc})."

    try:
        lineas, subtotal, impuestos, total = calcular_totales(items)
    except (ValueError, TypeError) as exc:
        return f"No se emitio la factura: {exc}."

    digito = calcular_digito_verificacion(nit_limpio)
    invoice_id = uuid4()
    emitida = datetime.now(timezone.utc)

    # Paso 1: reservar el consecutivo. La factura se inserta en `pending_dian`
    # en una transaccion corta, ANTES de hablar con la DIAN: el documento que
    # se le envia tiene que llevar su numero (el CUFE se calcula con el), y
    # si algo falla despues queda una fila con numero que conciliar, no una
    # factura aprobada sin rastro local.
    try:
        async with tenant_session(client_id) as session:
            numero = await _siguiente_consecutivo(session, client_id)
            session.add(
                Invoice(
                    id=invoice_id,
                    client_id=client_id,
                    contact_id=_contact_id(config),
                    conversation_id=_conversation_id(config),
                    invoice_number=numero,
                    buyer_nit=f"{nit_limpio}-{digito}",
                    buyer_name=buyer_name,
                    items=lineas,
                    subtotal_cents=subtotal,
                    tax_total_cents=impuestos,
                    total_cents=total,
                    status=INVOICE_PENDING_DIAN,
                    dian_response={},
                    issued_at=emitida,
                )
            )
    except Exception:
        # Todavia no se hablo con la DIAN: no hay nada que conciliar.
        logger.exception("No se pudo reservar el consecutivo de la factura %s", invoice_id)
        return (
            "No se pudo emitir la factura por un problema tecnico. "
            "No se envio nada a la DIAN; puedes intentarlo de nuevo."
        )

    documento = {
        "invoice_number": numero,
        "prefix": PREFIJO_CONSECUTIVO.rstrip("-"),
        "buyer_nit": f"{nit_limpio}-{digito}",
        "buyer_name": buyer_name,
        "items": lineas,
        "subtotal_cents": subtotal,
        "tax_total_cents": impuestos,
        "total_cents": total,
        "currency": "COP",
        "issue_date": emitida.isoformat(),
    }

    # Paso 2: la DIAN, sin ninguna transaccion abierta. Es una llamada de red
    # de hasta 10s y no puede retener una conexion del pooler (misma razon que
    # el engine de webhooks salientes, ADR-065).
    estado = INVOICE_PENDING_DIAN
    cufe: str | None = None
    respuesta_dian: dict[str, Any] = {}
    error: str | None = None

    try:
        respuesta_dian = await enviar_factura(documento)
        cufe = respuesta_dian.get("cufe")
        estado = INVOICE_APPROVED if cufe else INVOICE_REJECTED
        if estado == INVOICE_REJECTED:
            error = str(respuesta_dian.get("error") or "La DIAN no devolvio CUFE")
    except DianError as exc:
        # Incluye DianNoConfiguradaError: la factura existe y queda pendiente
        # de validacion, nunca se le inventa un CUFE.
        logger.info("Factura %s queda pendiente de la DIAN: %s", numero, exc)
        error = str(exc)
    except Exception:
        # Un fallo inesperado aca ya no puede propagarse: la factura esta
        # reservada con su numero, y un `raise` haria que `ai_processor.py`
        # reintentara el turno entero — otra reserva y otro envio a la DIAN.
        # Queda pendiente con el motivo para conciliarla a mano (BUG-045).
        logger.exception("Fallo inesperado enviando la factura %s a la DIAN", numero)
        error = "Error inesperado al enviar a la DIAN; requiere revision manual"

    # Paso 3: guardar lo que dijo la DIAN sobre la fila ya reservada.
    try:
        async with tenant_session(client_id) as session:
            await session.execute(
                sa_update(Invoice)
                .where(Invoice.id == invoice_id, Invoice.client_id == client_id)
                .values(
                    status=estado,
                    dian_cufe=cufe,
                    dian_response=respuesta_dian,
                    error_message=error,
                )
                .execution_options(synchronize_session=False)
            )
    except Exception:
        # La DIAN ya pudo haber aceptado la factura (cufe con valor) antes de
        # que esto fallara: un `raise` aca dejaria que `ai_processor.py`
        # reintente el turno completo, y el reintento volveria a llamar a
        # `enviar_factura()` — una segunda factura real ante la DIAN por el
        # mismo pedido. Se registra en CRITICAL con el numero y el CUFE para
        # que un humano concilie a mano; la fila ya existe en `pending_dian`
        # con su numero. Hallazgo de /code-review sobre el PR #42.
        logger.critical(
            "No se pudo guardar la respuesta de la DIAN para la factura %s "
            "(estado=%s cufe=%s tenant=%s); requiere conciliacion manual",
            numero,
            estado,
            cufe,
            client_id,
            exc_info=True,
        )
        aviso_dian = f" La DIAN ya la habia aprobado con CUFE {cufe}." if cufe else ""
        return (
            f"La factura {numero} se envio pero no se pudo registrar la respuesta de la DIAN "
            f"por un problema tecnico.{aviso_dian} No la emitas otra vez: avisa a soporte "
            f"con el numero {numero} para revisarla."
        )

    resumen = (
        f"Factura {numero} emitida a {buyer_name} (NIT {nit_limpio}-{digito}). "
        f"Subtotal {_formatear_pesos(subtotal)} COP, IVA {_formatear_pesos(impuestos)} COP, "
        f"total {_formatear_pesos(total)} COP."
    )
    if estado == INVOICE_APPROVED:
        return f"{resumen} Aprobada por la DIAN, CUFE {cufe}."
    if estado == INVOICE_REJECTED:
        return f"{resumen} La DIAN la rechazo: {error}."
    return f"{resumen} Queda pendiente de validacion ante la DIAN ({error})."


@tool(parse_docstring=True)
async def get_invoice_status(invoice_number: str, config: RunnableConfig) -> str:
    """Consulta el estado de una factura por su numero.

    Args:
        invoice_number: Numero de la factura, por ejemplo FE-000012.
    """
    client_id = _client_id(config)
    contact_id = _contact_id_o_mensaje(config)
    numero = invoice_number.strip().upper()

    if isinstance(contact_id, str):
        return contact_id

    async with tenant_session(client_id) as session:
        # Un contacto solo puede consultar sus propias facturas, igual que en
        # las tools de agendamiento. El filtro es incondicional: ver SIN_CONTACTO.
        stmt = select(Invoice).where(
            Invoice.client_id == client_id,
            Invoice.invoice_number == numero,
            Invoice.contact_id == contact_id,
        )
        factura = (await session.execute(stmt)).scalar_one_or_none()

        if factura is None:
            return f"No encontre la factura {numero}."

        detalle = (
            f"Factura {factura.invoice_number} a nombre de {factura.buyer_name}: "
            f"estado {factura.status}, total {_formatear_pesos(factura.total_cents)} "
            f"{factura.currency}, emitida el {factura.issued_at:%d/%m/%Y}."
        )
        if factura.dian_cufe:
            detalle += f" CUFE {factura.dian_cufe}."
        if factura.error_message:
            detalle += f" Observacion: {factura.error_message}."
        return detalle


@tool(parse_docstring=True)
async def list_invoices(
    config: RunnableConfig,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
) -> str:
    """Lista las facturas del contacto de esta conversacion.

    Args:
        date_from: Fecha inicial en formato AAAA-MM-DD, opcional.
        date_to: Fecha final en formato AAAA-MM-DD, opcional.
        status: Estado a filtrar: draft, pending_dian, approved o rejected.
    """
    client_id = _client_id(config)
    contact_id = _contact_id_o_mensaje(config)

    if isinstance(contact_id, str):
        return contact_id

    stmt = select(Invoice).where(Invoice.client_id == client_id, Invoice.contact_id == contact_id)

    if status:
        if status not in INVOICE_STATUSES:
            return f"Estado invalido: {status}. Validos: {', '.join(INVOICE_STATUSES)}."
        stmt = stmt.where(Invoice.status == status)

    for valor, columna in ((date_from, "desde"), (date_to, "hasta")):
        if not valor:
            continue
        try:
            fecha = date.fromisoformat(valor)
        except ValueError:
            return f"Fecha {columna} invalida: {valor}. Usa el formato AAAA-MM-DD."
        momento = datetime.combine(fecha, datetime.min.time(), tzinfo=timezone.utc)
        if columna == "desde":
            stmt = stmt.where(Invoice.issued_at >= momento)
        else:
            # Estrictamente antes de la medianoche del dia SIGUIENTE, no
            # `.replace(hour=23, minute=59, second=59)`: ese replace deja
            # microsecond=0 (heredado de `datetime.min.time()`), asi que el
            # limite real quedaba en 23:59:59.000000 y una factura emitida en
            # cualquier momento de ese ultimo segundo (23:59:59.000001 en
            # adelante) se perdia del listado. Hallazgo de /code-review sobre
            # el PR #42.
            stmt = stmt.where(Invoice.issued_at < momento + timedelta(days=1))

    async with tenant_session(client_id) as session:
        facturas = (
            (await session.execute(stmt.order_by(Invoice.issued_at.desc()).limit(20)))
            .scalars()
            .all()
        )

    if not facturas:
        return "No hay facturas que coincidan con esos criterios."

    lineas = [
        f"- {factura.invoice_number} ({factura.issued_at:%d/%m/%Y}): "
        f"{_formatear_pesos(factura.total_cents)} {factura.currency}, estado {factura.status}"
        for factura in facturas
    ]
    return "Facturas encontradas:\n" + "\n".join(lineas)


#: Tools que el nodo financiero le ofrece al LLM.
INVOICE_TOOLS = [validate_nit, create_invoice, get_invoice_status, list_invoices]
