"""El dominio de la empresa de un lead (Sprint 17: "empresa por dominio").

Si el lead no trae `company_domain`, se deduce del email, salvo que sea de un correo gratuito:
`ana@gmail.com` no dice nada de su empresa y consultar `gmail.com` a un proveedor devolveria
Google. Ante la duda se devuelve `None`: un dominio equivocado enriquece al lead con otra empresa.
"""

import re

from app.schemas.lead_scoring import normalizar_dominio

#: Dominios de correo gratuito o de proveedor de acceso que se ven en LatAm y Europa.
FREE_EMAIL_DOMAINS: frozenset[str] = frozenset(
    {
        "aol.com",
        "bol.com.br",
        "fastmail.com",
        "free.fr",
        "gmx.com",
        "gmx.de",
        "gmx.es",
        "gmx.net",
        "hey.com",
        "inbox.com",
        "latinmail.com",
        "mail.com",
        "mail.ru",
        "me.com",
        "laposte.net",
        "msn.com",
        "orange.fr",
        "prodigy.net.mx",
        "proton.me",
        "protonmail.com",
        "rocketmail.com",
        "terra.com.br",
        "tutanota.com",
        "uol.com.br",
        "wanadoo.fr",
        "web.de",
        "ymail.com",
        "zoho.com",
    }
)
#: Marcas de correo gratuito con un dominio por pais (`hotmail.es`, `yahoo.com.mx`, `outlook.fr`).
_MARCAS_GRATUITAS: frozenset[str] = frozenset(
    {"gmail", "googlemail", "hotmail", "outlook", "live", "yahoo", "icloud", "yandex"}
)
_TLD_O_SEGUNDO_NIVEL = re.compile(r"^(?:[a-z]{2,3})(?:\.[a-z]{2})?$")
_ESQUEMA = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)


def es_correo_gratuito(dominio: str) -> bool:
    """Si el dominio es de un proveedor de correo gratuito.

    Args:
        dominio: Dominio normalizado.

    Returns:
        `True` para `gmail.com`, `hotmail.es`, `yahoo.com.mx`, `outlook.fr`, `aol.com`...
    """
    if dominio in FREE_EMAIL_DOMAINS:
        return True
    marca, _, resto = dominio.partition(".")
    return marca in _MARCAS_GRATUITAS and bool(_TLD_O_SEGUNDO_NIVEL.match(resto))


def dominio_de_email(email: str | None) -> str | None:
    """Dominio corporativo de un email.

    Args:
        email: Email del lead.

    Returns:
        El dominio normalizado, o `None` si no hay email, no es valido o es de correo gratuito.
    """
    if not email or email.count("@") != 1:
        return None
    dominio = normalizar_dominio(email.rsplit("@", 1)[1])
    if dominio is None or es_correo_gratuito(dominio):
        return None
    return dominio


def dominio_de_url(valor: str | None) -> str | None:
    """Dominio de un valor que puede ser un dominio o una URL (`https://www.acme.com/contacto`).

    Args:
        valor: Lo que se escribio en `company_domain`.

    Returns:
        El dominio normalizado, o `None` si no hay uno valido (una IP, `localhost`, texto suelto).
    """
    if not valor or not valor.strip():
        return None
    limpio = _ESQUEMA.sub("", valor.strip())
    host = re.split(r"[/?#]", limpio, maxsplit=1)[0]
    host = host.rsplit("@", 1)[-1].split(":", 1)[0]
    return normalizar_dominio(host)


def inferir_dominio(company_domain: str | None, email: str | None) -> str | None:
    """El dominio por el que buscar la empresa del lead.

    Manda `company_domain` (lo escribio alguien); si falta o no es valido, el del email
    corporativo. Un `company_domain` de correo gratuito (alguien escribio `gmail.com`) tampoco
    sirve.

    Args:
        company_domain: `leads.company_domain`.
        email: `leads.email`.

    Returns:
        El dominio, o `None`.
    """
    dominio = dominio_de_url(company_domain)
    if dominio is not None and not es_correo_gratuito(dominio):
        return dominio
    return dominio_de_email(email)
