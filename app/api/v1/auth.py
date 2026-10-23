"""Endpoints de autenticación — login, refresh y logout.

POST /api/v1/auth/login       — email + password → access_token (+ cookie del refresh)
POST /api/v1/auth/refresh     — cookie del refresh → access_token nuevo y refresh rotado
POST /api/v1/auth/logout      — anula el refresh de este navegador y borra la cookie
POST /api/v1/auth/logout-all  — (con access token) anula todas las sesiones del usuario

El refresh token ya no viaja en el cuerpo: va en una cookie `httpOnly` (ver
`app/core/session_cookie.py` y ADR-089).

Por qué el login no usa el ORM (BUG-025)
----------------------------------------
Autenticar es la única operación del sistema que necesita mirar `users` **sin**
saber a qué tenant pertenece la fila: el tenant se deduce del usuario, y el
usuario es justo lo que se está buscando. La RLS de `users` y de `clients`
filtra por `app.current_client_id`, que en esa transacción no puede estar
puesto, así que un `select(User)` normal no devuelve nada contra un rol sujeto a
las políticas.

La búsqueda va por `auth_lookup_user()`, una función `SECURITY DEFINER`
(migración 007) que es el único punto con ese acceso y solo sabe resolver un
email exacto. Todo lo que viene después del login —incluida la escritura de
`last_login_at`— ya conoce el tenant y vuelve a pasar por `tenant_session()`,
con la RLS aplicándose con normalidad.
"""

import asyncio
import hashlib
import logging
import secrets
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal, tenant_session
from app.core.dependencies import get_current_user
from app.core.exceptions import (
    FORBIDDEN,
    INVALID_TOKEN,
    AppException,
    JWTExpiredError,
    JWTInvalidError,
)
from app.core.rate_limit import (
    RATE_LIMITED,
    client_ip,
    contar_fallo,
    dentro_del_limite,
    fallos,
    olvidar,
)
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_jwt,
    hash_password,
    verify_password,
)
from app.core.session_cookie import (
    REFRESH_COOKIE,
    borrar_cookie,
    poner_cookie,
    registrar_uso,
    revocar_sesiones,
)
from app.schemas.auth import LoginRequest, TokenResponse

logger = logging.getLogger(__name__)

router = APIRouter()

# La función devuelve como mucho una fila: `users.email` es UNIQUE.
_AUTH_LOOKUP = text(
    "SELECT id, client_id, email, password_hash, first_name, last_name, "
    "role, is_active, client_is_active FROM auth_lookup_user(:email)"
)

_LAST_LOGIN = text(
    "UPDATE users SET last_login_at = :ahora WHERE id = :user_id RETURNING token_version"
)

# BUG-041: el refresh_token por si solo no basta para saber si el usuario y su
# tenant siguen activos -- eso puede cambiar en los hasta 7 dias
# (JWT_REFRESH_EXPIRATION_DAYS) de vida del token. Filtro explicito en el WHERE
# ademas de la RLS de `tenant_session`, mismo patron que `contact_unifier.py` y
# `documents.py`.
_REFRESH_LOOKUP = text(
    "SELECT u.is_active AS user_is_active, c.is_active AS client_is_active, "
    "u.role::text AS role, u.email AS email, u.token_version AS token_version "
    "FROM users u JOIN clients c ON c.id = u.client_id "
    "WHERE u.id = :user_id AND u.client_id = :client_id"
)


@lru_cache(maxsize=1)
def _hash_de_relleno() -> str:
    """Hash bcrypt de un valor descartado, para igualar el tiempo del login.

    Returns:
        Un hash bcrypt con el mismo coste que los de los usuarios reales.
    """
    return hash_password(secrets.token_urlsafe(16))


def _verificar_password(password: str, hash_guardado: str | None) -> bool:
    """Verifica el password; sin usuario, gasta el mismo tiempo y devuelve False.

    Si un email inexistente respondiera sin pasar por bcrypt (~100 ms) y uno
    existente si, la diferencia de latencia permitiria enumerar los emails
    registrados. Es sincrona a proposito: bcrypt es CPU y se ejecuta en un hilo
    (`asyncio.to_thread`), no en el event loop (CLAUDE.md, regla 4).

    Args:
        password: Password recibido.
        hash_guardado: Hash del usuario, o `None` si el email no existe.

    Returns:
        True solo si hay usuario y el password coincide.
    """
    if hash_guardado is None:
        verify_password(password, _hash_de_relleno())
        return False
    return verify_password(password, hash_guardado)


def _demasiados_intentos() -> AppException:
    """429 comun a login y refresh (no dice si el email existe)."""
    return AppException(
        status_code=429,
        error_code=RATE_LIMITED,
        message="Demasiados intentos. Espera un rato e intentalo de nuevo",
    )


def _clave_de_fallos(email: str) -> str:
    """Clave de Redis de los fallos de un email, sin guardar el email en claro."""
    return "login-fallos:" + hashlib.sha256(email.strip().lower().encode()).hexdigest()[:32]


async def _frenar_login(request: Request, email: str) -> str:
    """Limita los intentos de login por IP y los fallos por email (fuerza bruta).

    Args:
        request: Peticion, para la IP.
        email: Email que se intenta.

    Returns:
        La clave de fallos del email, para anotar el resultado.

    Raises:
        AppException: 429 si la IP o el email superaron su limite.
    """
    ajustes = get_settings()
    ip = client_ip(request)
    if not await dentro_del_limite(f"login-ip:{ip}", ajustes.LOGIN_MAX_PER_IP_PER_MINUTE, 60):
        logger.warning("Login: limite por IP superado (%s)", ip)
        raise _demasiados_intentos()
    clave = _clave_de_fallos(email)
    if await fallos(clave) >= ajustes.LOGIN_MAX_FAILURES_PER_EMAIL:
        logger.warning("Login: email bloqueado por fallos (%s, ip=%s)", clave[-8:], ip)
        raise _demasiados_intentos()
    return clave


@router.post("/login", response_model=TokenResponse)
async def login(credentials: LoginRequest, request: Request, response: Response) -> TokenResponse:
    """Autenticar usuario con email y password.

    1. Busca al usuario por email vía `auth_lookup_user()` (ver el docstring del
       módulo: es la única consulta del sistema sin contexto de tenant).
    2. Verifica el password con bcrypt.
    3. Verifica que usuario y tenant estén activos.
    4. Genera el access_token (en el cuerpo) y el refresh_token (en la cookie `httpOnly`).
    5. Actualiza `last_login_at`, ya dentro del contexto del tenant.

    Antes de nada se limita por IP y por fallos del email (`_frenar_login`).

    Args:
        credentials: Email y password del usuario.
        request: Peticion, para la IP del limite.
        response: Respuesta, para poner la cookie del refresh token.

    Returns:
        TokenResponse con access_token, refresh_token y token_type.

    Raises:
        AppException: 401 si las credenciales son inválidas o si el usuario o su
            organización están desactivados; 429 si se superó un límite.
    """
    clave_fallos = await _frenar_login(request, credentials.email)
    async with AsyncSessionLocal() as session, session.begin():
        user = (await session.execute(_AUTH_LOOKUP, {"email": credentials.email})).one_or_none()

    # bcrypt se ejecuta siempre, exista o no el usuario, y fuera del event loop.
    password_ok = await asyncio.to_thread(
        _verificar_password, credentials.password, None if user is None else user.password_hash
    )
    if user is None or not password_ok:
        await contar_fallo(clave_fallos, get_settings().LOGIN_FAILURE_WINDOW_SECONDS)
        logger.warning("Login fallido (%s, ip=%s)", clave_fallos[-8:], client_ip(request))
        raise AppException(
            status_code=401,
            error_code=INVALID_TOKEN,
            message="Credenciales inválidas",
        )

    if not user.is_active:
        raise AppException(
            status_code=401,
            error_code=FORBIDDEN,
            message="Usuario desactivado",
        )

    # El estado del tenant viene en la misma consulta: `clients` también tiene
    # RLS, así que una segunda consulta desde aquí chocaría con lo mismo.
    if not user.client_is_active:
        raise AppException(
            status_code=401,
            error_code=FORBIDDEN,
            message="Organización suspendida",
        )

    token_data = {
        "user_id": str(user.id),
        "client_id": str(user.client_id),
        "email": user.email,
        "role": user.role,
    }

    # A partir de aquí ya se conoce el tenant, así que la escritura vuelve al
    # camino normal: con RLS aplicada y firmada por el propio usuario, que es lo
    # que el trigger de auditoría registrará el día que `users` lleve uno.
    version = await _registrar_ultimo_acceso(user.client_id, user.id)
    await olvidar(clave_fallos)

    logger.info(
        "Login exitoso: user=%s client=%s role=%s",
        user.id,
        user.client_id,
        user.role,
    )

    if version is not None:
        poner_cookie(response, create_refresh_token(str(user.id), str(user.client_id), version))
    return TokenResponse(access_token=create_access_token(token_data))


async def _registrar_ultimo_acceso(client_id: UUID, user_id: UUID) -> int | None:
    """Anota la fecha del último acceso y devuelve la version de sesion del usuario.

    La fecha es contabilidad, no parte de la autenticación: si falla, el login ya es
    válido y negarlo dejaría al usuario fuera por un dato accesorio. Se registra
    el fallo y se sigue, pero sin version no se emite refresh token: la sesion dura lo
    que el access token (ADR-089).

    Args:
        client_id: Tenant del usuario, ya conocido.
        user_id: Usuario que acaba de entrar.

    Returns:
        `users.token_version`, o `None` si la base fallo.
    """
    try:
        async with tenant_session(client_id, user_id=user_id) as session:
            version = (
                await session.execute(
                    _LAST_LOGIN, {"ahora": datetime.now(timezone.utc), "user_id": user_id}
                )
            ).scalar_one_or_none()
    except Exception:
        logger.exception("No se pudo actualizar last_login_at de %s", user_id)
        return None
    return None if version is None else int(version)


def _sesion_invalida(mensaje: str = "Sesión no válida") -> AppException:
    """401 de sesion; `refresh_token` lo convierte en respuesta que borra la cookie.

    Args:
        mensaje: Mensaje para el cliente.

    Returns:
        La excepcion a lanzar.
    """
    return AppException(status_code=401, error_code=INVALID_TOKEN, message=mensaje)


def _leer_refresh(request: Request) -> dict[str, Any] | None:
    """El payload del refresh token de la cookie, o `None` si no hay o no vale.

    Args:
        request: Peticion con la cookie.

    Returns:
        El payload, solo si es un token valido de tipo `refresh`.
    """
    token = request.cookies.get(REFRESH_COOKIE)
    if not token:
        return None
    try:
        payload = decode_jwt(token)
    except (JWTExpiredError, JWTInvalidError):
        return None
    campos = ("user_id", "client_id", "ver", "jti")
    if payload.get("type") != "refresh" or any(c not in payload for c in campos):
        return None
    return payload


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(request: Request, response: Response) -> TokenResponse | JSONResponse:
    """Renueva el access token con el refresh token de la cookie y lo rota.

    Ver `_refrescar`. Un 401 se devuelve aqui como respuesta propia, no como excepcion: el
    handler global de `AppException` crea otra respuesta y perderia el `Set-Cookie` que borra
    la cookie, y el navegador seguiria enviando un refresh que ya no vale.

    Args:
        request: Peticion, con la cookie y la IP del limite.
        response: Respuesta, para la cookie nueva.

    Returns:
        TokenResponse con el access token nuevo, o el 401 con la cookie borrada.

    Raises:
        AppException: 429 por el limite por IP.
    """
    try:
        return await _refrescar(request, response)
    except AppException as exc:
        if exc.status_code != 401:
            raise
        rechazo = JSONResponse(
            status_code=401, content={"error_code": exc.error_code, "message": exc.message}
        )
        borrar_cookie(rechazo)
        return rechazo


async def _refrescar(request: Request, response: Response) -> TokenResponse:
    """Renueva el access token con el refresh token de la cookie y lo rota.

    1. Lee y valida el refresh de la cookie `httpOnly` (firma, `iss`/`aud`, `type`).
    2. Revalida contra la base que usuario y tenant sigan activos (BUG-041) y que la version
       de sesion (`users.token_version`) sea la del token: si subio (logout de todos los
       dispositivos, cambio de contrasena o de rol), el token ya no vale.
    3. Rotacion: el refresh es de un solo uso. Reusarlo fuera del margen
       (`REFRESH_REUSE_GRACE_SECONDS`) es indicio de robo y revoca todas las sesiones.
    4. Devuelve un access token con el rol y el email **actuales** y pone un refresh nuevo.

    Args:
        request: Peticion, con la cookie y la IP del limite.
        response: Respuesta, para la cookie nueva.

    Returns:
        TokenResponse con el access token nuevo.

    Raises:
        AppException: 401 si no hay refresh valido, el usuario no existe o esta inactivo, la
            sesion fue revocada o el token se reutilizo; 429 por el limite por IP.
    """
    if not await dentro_del_limite(
        f"refresh-ip:{client_ip(request)}", get_settings().REFRESH_MAX_PER_IP_PER_MINUTE, 60
    ):
        raise _demasiados_intentos()
    payload = _leer_refresh(request)
    if payload is None:
        raise _sesion_invalida()

    try:
        client_id = UUID(str(payload["client_id"]))
        user_id = UUID(str(payload["user_id"]))
        version = int(payload["ver"])
    except (ValueError, TypeError) as exc:
        raise _sesion_invalida() from exc

    async with tenant_session(client_id) as session:
        fila = (
            await session.execute(_REFRESH_LOOKUP, {"user_id": user_id, "client_id": client_id})
        ).one_or_none()

    if fila is None:
        raise _sesion_invalida("Credenciales inválidas")
    if not fila.user_is_active:
        raise AppException(status_code=401, error_code=FORBIDDEN, message="Usuario desactivado")
    if not fila.client_is_active:
        raise AppException(status_code=401, error_code=FORBIDDEN, message="Organización suspendida")
    if int(fila.token_version) != version:
        raise _sesion_invalida("La sesión se cerró; vuelve a iniciar sesión")

    if await registrar_uso(str(payload["jti"]), int(payload["exp"])) == "reutilizado":
        # Un refresh ya rotado vuelve a llegar: o lo copio alguien, o lo usa el legitimo
        # despues de que el atacante lo rotara. No se sabe cual: se cierran todas.
        async with tenant_session(client_id, user_id=user_id) as session:
            await revocar_sesiones(session, client_id, user_id)
        logger.warning(
            "Refresh token reutilizado para el usuario %s (ip=%s): sesiones revocadas",
            user_id,
            client_ip(request),
        )
        raise _sesion_invalida("La sesión se cerró por seguridad; vuelve a entrar")

    # Rol y email salen de la base, no del refresh token: si un admin cambia el rol de
    # alguien (o lo degrada), el access token nuevo ya lleva el rol actual.
    token_data = {
        "user_id": str(user_id),
        "client_id": str(client_id),
        "email": fila.email,
        "role": fila.role,
    }
    poner_cookie(response, create_refresh_token(str(user_id), str(client_id), version))
    return TokenResponse(access_token=create_access_token(token_data))


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response) -> Response:
    """Cierra la sesion de este navegador: anula su refresh token y borra la cookie.

    No exige access token (puede haber caducado) ni falla si no hay cookie: cerrar sesion
    siempre deja al navegador sin sesion.

    Args:
        request: Peticion con la cookie.
        response: Respuesta, para borrar la cookie.

    Returns:
        204 sin cuerpo.
    """
    payload = _leer_refresh(request)
    if payload is not None:
        # Marcarlo como usado lo anula: si alguien lo copio, al usarlo se detecta la
        # reutilizacion y se cierran todas las sesiones.
        await registrar_uso(str(payload["jti"]), int(payload["exp"]))
    respuesta = Response(status_code=204)
    borrar_cookie(respuesta)
    return respuesta


@router.post("/logout-all", status_code=204)
async def logout_all(user: dict[str, Any] = Depends(get_current_user)) -> Response:
    """Cierra todas las sesiones del usuario en todos los dispositivos.

    Sube `users.token_version`: ningun refresh token emitido antes vale. Los access tokens
    ya emitidos siguen hasta su caducidad (`JWT_EXPIRATION_MINUTES`).

    Args:
        user: Usuario autenticado.

    Returns:
        204 sin cuerpo y la cookie borrada.
    """
    client_id = UUID(str(user["client_id"]))
    user_id = UUID(str(user["user_id"]))
    async with tenant_session(client_id, user_id=user_id) as session:
        await revocar_sesiones(session, client_id, user_id)
    logger.info("Usuario %s cerro todas sus sesiones", user_id)
    respuesta = Response(status_code=204)
    borrar_cookie(respuesta)
    return respuesta
