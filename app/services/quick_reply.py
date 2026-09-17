"""Resolucion de las variables de una respuesta rapida.

El contenido de una respuesta rapida admite marcadores `{{variable}}` que se
sustituyen en el momento de usarla, con los datos de la conversacion concreta.

Una variable que no se conoce, o para la que falta contexto, **se deja tal cual**
en el texto y se reporta aparte. Es deliberado: un agente que ve
`Hola {{contact_name}},` sin resolver sabe que algo falta y lo corrige antes de
enviar; si se sustituyera por vacio, saldria "Hola ," hacia el cliente final sin
que nadie se entere.
"""

import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

# `{{ nombre }}`, con o sin espacios alrededor del nombre.
VARIABLE_PATTERN = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def _contact_name(contexto: dict[str, Any]) -> str:
    """Nombre visible del contacto.

    Prefiere `display_name` porque es lo que el propio contacto puso en su
    perfil del canal; si no hay, arma nombre y apellido.

    Args:
        contexto: Debe traer `contact`.

    Returns:
        El nombre a usar en el saludo.

    Raises:
        KeyError: Si no hay contacto en el contexto.
        ValueError: Si el contacto no tiene ningun nombre utilizable.
    """
    contacto = contexto["contact"]
    if getattr(contacto, "display_name", None):
        return str(contacto.display_name)

    partes = [getattr(contacto, "first_name", None), getattr(contacto, "last_name", None)]
    nombre = " ".join(p for p in partes if p).strip()
    if not nombre:
        raise ValueError("el contacto no tiene nombre")
    return nombre


def _agent_name(contexto: dict[str, Any]) -> str:
    """Nombre del agente humano que esta respondiendo.

    Args:
        contexto: Debe traer `agent`.

    Returns:
        Nombre y apellido del agente.

    Raises:
        KeyError: Si no hay agente en el contexto.
        ValueError: Si el agente no tiene nombre.
    """
    agente = contexto["agent"]
    nombre = " ".join(
        p for p in (getattr(agente, "first_name", None), getattr(agente, "last_name", None)) if p
    ).strip()
    if not nombre:
        raise ValueError("el agente no tiene nombre")
    return nombre


def _ticket_id(contexto: dict[str, Any]) -> str:
    """Identificador corto de la conversacion, para que el cliente lo pueda citar.

    Args:
        contexto: Debe traer `conversation`.

    Returns:
        Los primeros 8 caracteres del UUID de la conversacion.

    Raises:
        KeyError: Si no hay conversacion en el contexto.
    """
    return str(contexto["conversation"].id)[:8]


def _date(_: dict[str, Any]) -> str:
    """Fecha de hoy en formato dia/mes/ano.

    Args:
        _: Contexto, sin usar.

    Returns:
        La fecha actual en UTC.
    """
    return datetime.now(timezone.utc).strftime("%d/%m/%Y")


VARIABLE_RESOLVERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "contact_name": _contact_name,
    "agent_name": _agent_name,
    "ticket_id": _ticket_id,
    "date": _date,
}


def resolve_quick_reply(content: str, context: dict[str, Any]) -> tuple[str, list[str]]:
    """Sustituye las variables del contenido con los datos del contexto.

    Args:
        content: Texto de la respuesta rapida, con marcadores `{{variable}}`.
        context: Datos disponibles (`contact`, `agent`, `conversation`). Las
            claves que falten no rompen: sus variables quedan sin resolver.

    Returns:
        El texto resuelto y la lista ordenada de variables que no se pudieron
        resolver (desconocidas o sin contexto suficiente), sin repetir.
    """
    sin_resolver: list[str] = []

    def _sustituir(match: re.Match[str]) -> str:
        """Resuelve una variable, o la deja intacta si no se puede."""
        nombre = match.group(1)
        resolver = VARIABLE_RESOLVERS.get(nombre)
        if resolver is None:
            if nombre not in sin_resolver:
                sin_resolver.append(nombre)
            return match.group(0)
        try:
            return resolver(context)
        except (KeyError, AttributeError, ValueError, TypeError):
            if nombre not in sin_resolver:
                sin_resolver.append(nombre)
            return match.group(0)

    return VARIABLE_PATTERN.sub(_sustituir, content), sin_resolver
