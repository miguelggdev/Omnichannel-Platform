"""Lectura del webhook de Phantombuster (Sprint 16, ADR-084).

Phantombuster avisa al terminar un agente con un `POST` JSON con, entre otros, `agentId`,
`containerId`, `exitCode`, `exitMessage` y `resultObject` (**un texto** con el resultado del
agente). Lo que **no** se pudo comprobar (la documentacion de Phantombuster no era accesible
desde este entorno) es la forma de `resultObject` de cada Phantom: unos devuelven la lista de
perfiles, otros solo enlaces a un CSV/JSON. Por eso la lectura es **tolerante** y no adivina:

- Acepta el payload completo, o directamente una lista de perfiles (para quien reenvia el
  resultado con Zapier/Make).
- `resultObject` puede venir como texto JSON o ya decodificado; si es una lista, son los perfiles;
  si es un objeto, se busca una lista bajo `results`/`profiles`/`data`/`items`/`leads`, o se
  trata como un unico perfil si tiene campos reconocibles.
- Si el resultado **solo trae enlaces** a archivos (`csvUrl`, `jsonUrl`...), no se descargan:
  seguir un enlace que llega en un `POST` publico es un SSRF servido en bandeja (apuntaria a la
  red interna) y la plataforma no tiene salida a internet por diseno. Se informa y el cliente usa
  la importacion de CSV con ese archivo.
- Un agente que no termino bien (`exitCode != 0` o `exitMessage` distinto de `finished`) no importa.
"""

import json
from dataclasses import dataclass, field
from typing import Any

from app.services.lead_import import campos_de_objeto

MAX_PERFILES = 500
_CLAVES_DE_LISTA = ("results", "profiles", "data", "items", "leads")
_CLAVES_DE_ENLACE = ("csvurl", "jsonurl", "csv", "json", "resulturl", "fileurl")


@dataclass
class Extraccion:
    """Lo que se pudo sacar de un webhook de Phantombuster.

    Attributes:
        perfiles: Los perfiles (objetos), hasta `MAX_PERFILES`.
        recibidos: Cuantos perfiles traia el payload (antes de recortar).
        solo_enlaces: El resultado solo traia enlaces a archivos: no se importa nada.
        exit_ok: El agente termino bien.
        motivo: Por que no hay perfiles, si no los hay (para el log, nunca datos).
    """

    perfiles: list[dict[str, Any]] = field(default_factory=list)
    recibidos: int = 0
    solo_enlaces: bool = False
    exit_ok: bool = True
    motivo: str | None = None


def _lista_de_objetos(valor: Any) -> list[dict[str, Any]] | None:
    if isinstance(valor, list) and all(isinstance(x, dict) for x in valor):
        return valor
    return None


def _a_perfiles(resultado: Any) -> list[dict[str, Any]] | None:
    """Los perfiles dentro de un resultado, o `None` si no se reconoce la forma."""
    directa = _lista_de_objetos(resultado)
    if directa is not None:
        return directa
    if isinstance(resultado, dict):
        for clave in _CLAVES_DE_LISTA:
            interna = _lista_de_objetos(resultado.get(clave))
            if interna is not None:
                return interna
        if campos_de_objeto(resultado):
            return [resultado]
    return None


def extraer_perfiles(payload: Any) -> Extraccion:
    """Saca los perfiles de un payload de Phantombuster (o de una lista que alguien reenvie).

    Args:
        payload: El cuerpo JSON ya decodificado.

    Returns:
        La extraccion: perfiles recortados a `MAX_PERFILES` y por que no hay, si no los hay.
    """
    if isinstance(payload, list):
        lista = _lista_de_objetos(payload)
        if lista is None:
            return Extraccion(motivo="la lista no contiene objetos")
        return Extraccion(perfiles=lista[:MAX_PERFILES], recibidos=len(lista))
    if not isinstance(payload, dict):
        return Extraccion(motivo="el cuerpo no es un objeto ni una lista")

    codigo = payload.get("exitCode")
    mensaje = payload.get("exitMessage")
    if (codigo not in (None, 0)) or (mensaje not in (None, "finished")):
        return Extraccion(exit_ok=False, motivo="el agente no termino correctamente")

    resultado = payload.get("resultObject")
    if isinstance(resultado, str):
        try:
            resultado = json.loads(resultado)
        except ValueError:
            return Extraccion(motivo="resultObject no es JSON valido")
    if resultado is None:
        return Extraccion(motivo="sin resultObject")

    perfiles = _a_perfiles(resultado)
    if perfiles is None:
        if isinstance(resultado, dict) and any(
            str(k).lower() in _CLAVES_DE_ENLACE for k in resultado
        ):
            return Extraccion(solo_enlaces=True, motivo="el resultado solo trae enlaces a archivos")
        return Extraccion(motivo="forma de resultObject no reconocida")
    return Extraccion(perfiles=perfiles[:MAX_PERFILES], recibidos=len(perfiles))
