"""Cifrado de columnas sensibles con pgcrypto (Sprint 8, Dev A).

Expone dos piezas que se usan juntas:

- `EncryptedString`: tipo de SQLAlchemy que cifra al escribir y descifra al
  leer, sin que el código de negocio se entere. El dato viaja cifrado a disco;
  la clave (`ENCRYPTION_KEY`) vive solo en el entorno del proceso, nunca en la
  base.
- `blind_index()`: HMAC-SHA256 determinista del mismo valor, para las dos cosas
  que el cifrado hace imposibles — buscar por igualdad y garantizar unicidad.

Por qué hace falta el índice ciego
----------------------------------
`pgp_sym_encrypt()` **no es determinista**: cifra con un IV aleatorio, así que
el mismo teléfono cifrado dos veces da dos bytes distintos. La nota del spec
(§8.4) que dice que "el cifrado es determinístico para un mismo
ENCRYPTION_KEY" es incorrecta, y sobre esa premisa se cae todo lo demás:

- `WHERE identifier_value = '+57300...'` no encuentra nada, porque la
  comparación sería contra un ciphertext nuevo.
- `UNIQUE (client_id, channel, identifier_value)` deja de proteger de nada: dos
  filas con el mismo teléfono tienen ciphertexts distintos y el índice las
  acepta como diferentes. Para este proyecto eso significa duplicar contactos
  en cada mensaje entrante.

La salida estándar es un *blind index*: una columna con el HMAC del valor en
claro, determinista, sobre la que sí se puede poner el UNIQUE y hacer la
búsqueda por igualdad. Es HMAC y no un SHA-256 pelado justamente porque un
teléfono tiene poquísima entropía: sin clave, la tabla de hashes se invierte por
fuerza bruta en minutos.

El hash es por tenant
---------------------
El HMAC se calcula sobre `"{client_id}:{valor}"`, no sobre el valor solo. Con una
clave global y un mensaje sin tenant, el mismo teléfono producía el **mismo**
hash en todos los tenants: quien tuviera lectura cruda de la tabla (un rol con
`BYPASSRLS`, un dump, un backup, el soporte del proveedor de base de datos)
podía correlacionar por igualdad de hash que la misma persona escribe a dos
clientes distintos de la plataforma, que es justo lo que el aislamiento por
tenant quiere impedir entre clientes B2B que pueden ser competidores. La
unicidad (`UNIQUE (client_id, channel, identifier_hash)`) y las búsquedas ya
estaban acotadas por tenant; lo que cambia es que ahora el propio hash tampoco
sirve para cruzarlos. `client_id` es obligatorio en `blind_index()` a propósito:
si fuera opcional, olvidarlo volvería en silencio al comportamiento global.

Lo que este tipo **no** resuelve
--------------------------------
Búsquedas parciales. `ILIKE '%juan%'` sobre una columna cifrada no existe, ni
con índice ciego (el HMAC es del valor completo). Por eso `contacts.first_name`,
`last_name` y `display_name` siguen en claro: el CRM los busca con ILIKE
(`app/api/v1/contacts.py`), y cifrarlos rompería la búsqueda de contactos sin
que ningún test lo avisara. El spec los marca como "cifrado opcional por
tenant" (§8.3) — esa opcionalidad necesita infraestructura por tenant que hoy
no existe, y queda anotada como pendiente, no implementada a medias.

Rotación de clave
-----------------
Cambiar `ENCRYPTION_KEY` deja ilegible todo lo cifrado con la anterior **y**
cambia todos los índices ciegos. Rotarla exige una migración de datos que lea
con la clave vieja y reescriba con la nueva, en la misma transacción que
recalcula los hashes. No hay atajo.

Por qué existe además `mask_identifier()`
------------------------------------------
El cifrado protege `contact_identifiers.identifier_value`, pero
`_resolve_contact()` (`app/tasks/webhook_processor.py`) copiaba ese mismo valor
sin cifrar a `contacts.display_name` para que el agente tenga algo que mostrar
en la bandeja antes de ponerle nombre al contacto. Eso dejaba el teléfono en
claro, y además buscable con `ILIKE`, en la columna de al lado de la cifrada —
cifrar `identifier_value` sin tocar esto no cerraba nada. La decisión de
producto (MEMORY.md) fue enmascarar en vez de mostrar el valor completo o
cifrar `display_name` (que rompería la búsqueda por `ILIKE` del CRM).
"""

from __future__ import annotations

import hashlib
import hmac
from typing import TYPE_CHECKING, Any

from sqlalchemy import Text, type_coerce
from sqlalchemy.dialects.postgresql import BYTEA
from sqlalchemy.sql import func
from sqlalchemy.types import TypeDecorator

from app.core.config import get_settings

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.sql.elements import ColumnElement


def _clave() -> str:
    """Devuelve la clave simétrica de cifrado.

    Se lee en cada uso y no al importar el módulo: `get_settings()` está
    cacheada, y leerla tarde permite que un test la sustituya sin reimportar.

    Returns:
        La `ENCRYPTION_KEY` del entorno.
    """
    return get_settings().ENCRYPTION_KEY


class EncryptedString(TypeDecorator[str]):
    """Columna `BYTEA` que se cifra al escribir y se descifra al leer.

    El cifrado lo hace PostgreSQL (`pgp_sym_encrypt`/`pgp_sym_decrypt`, de la
    extensión `pgcrypto` que crea la migración `001_baseline`), no Python: así
    el valor en claro no aparece en ningún log de la aplicación, y un `SELECT`
    desde `psql` sin la clave devuelve bytes.

    Attributes:
        impl: Tipo real de la columna en PostgreSQL (`BYTEA`).
        cache_ok: El tipo no lleva estado propio, así que SQLAlchemy puede
            cachear las sentencias que lo usan.
    """

    impl = BYTEA
    cache_ok = True

    def bind_expression(self, bindvalue: Any) -> ColumnElement[Any]:
        """Envuelve el parámetro con `pgp_sym_encrypt()` al escribir.

        El `type_coerce(..., Text)` no es decorativo: sin él, SQLAlchemy trata
        el parámetro como del tipo de la columna (`BYTEA`) y asyncpg rechaza un
        `str` donde espera `bytes`. Es el detalle que el spec (§8.2) omite y
        que hace fallar todo INSERT sobre la columna.

        Args:
            bindvalue: Parámetro con el valor en claro.

        Returns:
            Expresión SQL que cifra el valor.
        """
        return func.pgp_sym_encrypt(type_coerce(bindvalue, Text), _clave())

    def column_expression(self, col: Any) -> ColumnElement[Any]:
        """Envuelve la columna con `pgp_sym_decrypt()` al leer.

        Args:
            col: Columna cifrada.

        Returns:
            Expresión SQL que devuelve el texto en claro.
        """
        return func.pgp_sym_decrypt(col, _clave(), type_=Text)


def blind_index(valor: str | None, client_id: UUID | str, *, normalizar: bool = True) -> str | None:
    """Calcula el índice ciego (HMAC-SHA256) de un valor sensible, por tenant.

    Es determinista para una misma `ENCRYPTION_KEY` **y** un mismo tenant: dos
    llamadas con el mismo valor y el mismo `client_id` dan el mismo hash, que es
    justo lo que permite el `UNIQUE` y la búsqueda por igualdad sobre una
    columna cifrada. El mismo valor en tenants distintos da hashes distintos.

    Args:
        valor: Valor en claro. `None` devuelve `None`.
        client_id: Tenant propietario del valor. Entra en el mensaje del HMAC.
        normalizar: Recortar espacios y pasar a minúsculas antes de hashear.
            Evita que `" Juan@X.com "` y `"juan@x.com"` se traten como dos
            identificadores distintos. Desactivarlo solo tiene sentido para un
            valor donde las mayúsculas signifiquen algo.

    Returns:
        El HMAC en hexadecimal (64 caracteres), o `None`.

    Raises:
        ValueError: Si `client_id` está vacío. Un hash sin tenant sería el mismo
            en todos los tenants, justo lo que esta función evita.

    Example:
        >>> from uuid import uuid4
        >>> from app.core.encryption import blind_index
        >>> cliente = uuid4()
        >>> blind_index("  +57300123  ", cliente) == blind_index("+57300123", cliente)
        True
        >>> blind_index("+57300123", cliente) == blind_index("+57300123", uuid4())
        False
    """
    if not client_id:
        raise ValueError("blind_index() necesita el client_id del tenant")
    if valor is None:
        return None
    texto = valor.strip().lower() if normalizar else valor
    # `.lower()` sobre el tenant: `str(UUID)` ya sale en minusculas, pero un
    # `client_id` que llegue como `str` en mayusculas no debe dar otro hash (la
    # migracion 009 lo calcula en SQL con `client_id::text`, siempre minusculas).
    return hmac.new(
        _clave().encode("utf-8"),
        f"{str(client_id).lower()}:{texto}".encode(),
        hashlib.sha256,
    ).hexdigest()


def mask_identifier(valor: str) -> str:
    """Enmascara un identificador para mostrarlo sin exponerlo completo.

    Conserva los últimos 4 caracteres y reemplaza el resto por `*`, para que
    un agente pueda reconocer a quién le escribe (los últimos dígitos de un
    teléfono suelen bastar) sin que el valor completo quede legible en una
    columna que no está cifrada.

    Un valor de 4 caracteres o menos se enmascara entero: dejar "algo" a la
    vista de un identificador tan corto ya revela la mayor parte.

    Args:
        valor: Identificador en claro (teléfono, handle, email).

    Returns:
        El valor enmascarado, mismo largo que el original.

    Example:
        >>> mask_identifier("573001234567")
        '********4567'
        >>> mask_identifier("ab")
        '**'
    """
    if len(valor) <= 4:
        return "*" * len(valor)
    return "*" * (len(valor) - 4) + valor[-4:]
