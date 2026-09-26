"""Cliente de la DIAN para facturacion electronica colombiana (Sprint 12).

Degradacion, no fallo
----------------------
La integracion con la DIAN necesita credenciales de un proveedor tecnologico
autorizado, que este despliegue puede no tener. Cuando `DIAN_API_URL` o
`DIAN_API_TOKEN` estan vacias, `consultar_contribuyente()` devuelve None y
`enviar_factura()` lanza `DianNoConfiguradaError`: la factura queda en
`pending_dian` en vez de perderse, y el agente puede decirle al usuario que
esta emitida pero pendiente de validacion. Nunca se inventa un CUFE.

El digito de verificacion
--------------------------
El spec (§1.2) calcula el digito asi:

    nit_padded = nit.zfill(15)
    total = sum(int(d) * w for d, w in zip(nit_padded, weights))

Eso es incorrecto: los pesos de la DIAN se aplican **de derecha a izquierda**
(el peso 3 al ultimo digito), y rellenando por la izquierda y recorriendo en
orden natural cada digito recibe el peso que no le toca. Comprobado contra seis
NITs publicos conocidos (800197268-4, 830053812-2, 860002964-4, 890903938-8,
899999068-1, 890100577-6): la version de aca acierta los seis y la del spec
solo uno, y por casualidad. Un digito mal calculado hace que la DIAN rechace la
factura.
"""

import logging
from typing import Any

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Pesos oficiales de la DIAN, aplicados del ultimo digito hacia el primero.
_PESOS: tuple[int, ...] = (3, 7, 13, 17, 19, 23, 29, 37, 41, 43, 47, 53, 59, 67, 71)

# Largo minimo y maximo de un NIT/cedula sin digito de verificacion.
NIT_MIN_DIGITOS = 6
NIT_MAX_DIGITOS = 15


class DianError(RuntimeError):
    """Fallo al hablar con la DIAN."""


class DianNoConfiguradaError(DianError):
    """El despliegue no tiene credenciales de la DIAN."""


class NitInvalidoError(ValueError):
    """El NIT no tiene un formato utilizable."""


def normalizar_nit(nit: str) -> str:
    """Deja el NIT en digitos, sin puntos, guiones ni digito de verificacion.

    Args:
        nit: NIT tal como lo escribio el usuario (`900.373.115-0`, `900373115`).

    Returns:
        Solo los digitos del NIT, sin el de verificacion.

    Raises:
        NitInvalidoError: Si lo que queda no son digitos o no tiene un largo
            plausible.
    """
    limpio = nit.replace(".", "").replace(" ", "").strip()
    # Un `-N` final es el digito de verificacion que escribio el usuario.
    if "-" in limpio:
        limpio = limpio.rsplit("-", 1)[0]

    if not limpio.isdigit():
        raise NitInvalidoError("El NIT solo puede tener digitos, puntos y guion")
    if not NIT_MIN_DIGITOS <= len(limpio) <= NIT_MAX_DIGITOS:
        raise NitInvalidoError(
            f"El NIT debe tener entre {NIT_MIN_DIGITOS} y {NIT_MAX_DIGITOS} digitos"
        )
    return limpio


def calcular_digito_verificacion(nit: str) -> int:
    """Calcula el digito de verificacion de un NIT colombiano.

    Args:
        nit: NIT ya normalizado (solo digitos, sin digito de verificacion).

    Returns:
        El digito de verificacion, de 0 a 9.
    """
    total = sum(int(digito) * peso for digito, peso in zip(reversed(nit), _PESOS, strict=False))
    resto = total % 11
    return resto if resto <= 1 else 11 - resto


def _configurada() -> bool:
    """Indica si el despliegue tiene credenciales de la DIAN.

    Returns:
        True si hay URL y token configurados.
    """
    settings = get_settings()
    return bool(settings.DIAN_API_URL and settings.DIAN_API_TOKEN)


def _cliente() -> httpx.AsyncClient:
    """Cliente HTTP con la URL base, el token y el timeout de la DIAN.

    Returns:
        Cliente listo para usar como context manager.
    """
    settings = get_settings()
    return httpx.AsyncClient(
        base_url=settings.DIAN_API_URL.rstrip("/"),
        headers={"Authorization": f"Bearer {settings.DIAN_API_TOKEN}"},
        timeout=settings.DIAN_TIMEOUT_SECONDS,
    )


def _cuerpo_json(respuesta: httpx.Response) -> dict[str, Any] | None:
    """Lee el cuerpo de una respuesta de la DIAN como objeto JSON.

    `dict(respuesta.json())` levantaba `ValueError` con un cuerpo que no es
    JSON (una pagina de error de un proxy, un 200 vacio) y `TypeError`/
    `ValueError` con un JSON que no es objeto. Ninguna de las dos es
    `httpx.HTTPError` ni `DianError`, asi que escapaban de quien llama y
    tumbaban la tool (BUG-045).

    Args:
        respuesta: Respuesta HTTP de la DIAN.

    Returns:
        El objeto JSON, o `None` si el cuerpo no es un objeto JSON.
    """
    try:
        cuerpo = respuesta.json()
    except ValueError:
        return None
    return cuerpo if isinstance(cuerpo, dict) else None


async def consultar_contribuyente(nit: str) -> dict[str, Any] | None:
    """Consulta los datos de un contribuyente por NIT.

    Args:
        nit: NIT ya normalizado.

    Returns:
        Datos del contribuyente, `None` si la DIAN no esta configurada o no
        responde (el formato del NIT se valida igual, sin red), o un dict
        vacio si la DIAN dice que no existe.
    """
    if not _configurada():
        logger.debug("DIAN no configurada; solo se valida el formato del NIT")
        return None

    try:
        async with _cliente() as cliente:
            respuesta = await cliente.get(f"/taxpayers/{nit}")
    except httpx.HTTPError as exc:
        logger.warning("No se pudo consultar el NIT %s en la DIAN: %s", nit, exc)
        return None

    if respuesta.status_code == 404:
        return {}
    if respuesta.status_code >= 400:
        logger.warning("La DIAN devolvio %s al consultar el NIT", respuesta.status_code)
        return None

    cuerpo = _cuerpo_json(respuesta)
    if cuerpo is None:
        logger.warning(
            "La DIAN respondio al consultar el NIT %s con un cuerpo no interpretable", nit
        )
    return cuerpo


async def enviar_factura(factura: dict[str, Any]) -> dict[str, Any]:
    """Envia una factura a la DIAN para su validacion.

    Args:
        factura: Documento de la factura, ya calculado.

    Returns:
        Respuesta de la DIAN; se espera `cufe` cuando la aprueba.

    Raises:
        DianNoConfiguradaError: Si el despliegue no tiene credenciales. Quien
            llama debe dejar la factura en `pending_dian`, no inventar un CUFE.
        DianError: Si la DIAN no responde o rechaza la peticion.
    """
    if not _configurada():
        raise DianNoConfiguradaError("La integracion con la DIAN no esta configurada")

    try:
        async with _cliente() as cliente:
            respuesta = await cliente.post("/invoices", json=factura)
    except httpx.HTTPError as exc:
        raise DianError(f"No se pudo contactar con la DIAN: {exc}") from exc

    if respuesta.status_code >= 400:
        raise DianError(f"La DIAN devolvio {respuesta.status_code} al enviar la factura")

    cuerpo = _cuerpo_json(respuesta)
    if cuerpo is None:
        # Aceptada o no, no se puede saber: la factura queda pendiente para que
        # alguien la concilie, nunca se reintenta sola (podria duplicarla).
        raise DianError(
            f"La DIAN respondio {respuesta.status_code} con un cuerpo no interpretable; "
            "verifica en su portal si la factura quedo registrada"
        )
    return cuerpo
