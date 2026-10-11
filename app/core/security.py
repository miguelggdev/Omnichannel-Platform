"""Seguridad: JWT y hashing de passwords.

JWT con PyJWT (HS256 por defecto). Password hashing con passlib/bcrypt.

Todos los tokens llevan `iss` y `aud` propios de la plataforma, `iat`, `exp` y `type`
(ADR-089). `decode_jwt` exige los cinco: un token firmado con el mismo secreto por otro
emisor (por ejemplo, Supabase Auth si alguien reutilizo su secreto JWT, como sugeria el
`.env.example`) no tiene nuestro `iss`/`aud` y se rechaza.

- access: `{user_id, client_id, email, role}`; vive `JWT_EXPIRATION_MINUTES`.
- refresh: `{user_id, client_id, ver, jti}`; `ver` es `users.token_version` (subirla revoca
  todas las sesiones del usuario) y `jti` identifica el token para la rotacion.
"""

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, cast

import jwt
from passlib.context import CryptContext

from app.core.config import get_settings
from app.core.exceptions import JWTExpiredError, JWTInvalidError

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    """Genera hash bcrypt de un password.

    Args:
        password: Password en texto plano.

    Returns:
        Hash bcrypt del password.
    """
    return cast("str", pwd_context.hash(password))


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifica un password contra su hash bcrypt.

    Args:
        plain_password: Password en texto plano.
        hashed_password: Hash bcrypt almacenado.

    Returns:
        True si el password coincide.
    """
    return cast("bool", pwd_context.verify(plain_password, hashed_password))


_CLAIMS_OBLIGATORIOS = ["exp", "iat", "iss", "aud", "type"]


def _firmar(payload: dict[str, Any], vida: timedelta) -> str:
    """Firma un payload con los claims comunes (`iss`, `aud`, `iat`, `exp`).

    Args:
        payload: Claims propios del tipo de token (incluido `type`).
        vida: Tiempo hasta la expiracion.

    Returns:
        Token JWT codificado.
    """
    ajustes = get_settings()
    ahora = datetime.now(timezone.utc)
    cuerpo = {
        **payload,
        "iss": ajustes.JWT_ISSUER,
        "aud": ajustes.JWT_AUDIENCE,
        "iat": ahora,
        "exp": ahora + vida,
    }
    return jwt.encode(cuerpo, ajustes.JWT_SECRET, algorithm=ajustes.JWT_ALGORITHM)


def create_access_token(
    data: dict[str, Any],
    expires_delta: timedelta | None = None,
) -> str:
    """Crea un access token JWT.

    Args:
        data: Payload del token (user_id, client_id, email, role).
        expires_delta: Duración personalizada. Default: JWT_EXPIRATION_MINUTES.

    Returns:
        Token JWT codificado.
    """
    vida = expires_delta or timedelta(minutes=get_settings().JWT_EXPIRATION_MINUTES)
    return _firmar({**data, "type": "access"}, vida)


def create_refresh_token(user_id: str, client_id: str, token_version: int) -> str:
    """Crea un refresh token JWT, de un solo uso (se rota en cada `/auth/refresh`).

    Solo lleva identidad, version y `jti`: el rol y el email se leen de la base al refrescar.

    Args:
        user_id: Usuario.
        client_id: Tenant del usuario.
        token_version: `users.token_version` actual; si sube, el token deja de valer.

    Returns:
        Refresh token JWT codificado, con `JWT_REFRESH_EXPIRATION_DAYS` de vida.
    """
    payload = {
        "user_id": user_id,
        "client_id": client_id,
        "ver": token_version,
        "jti": secrets.token_urlsafe(16),
        "type": "refresh",
    }
    return _firmar(payload, timedelta(days=get_settings().JWT_REFRESH_EXPIRATION_DAYS))


def create_email_verification_token(user_id: str, client_id: str, email: str) -> str:
    """Crea el token de un solo proposito que viaja en el enlace de verificacion.

    Lleva `type=email_verification`, asi que el middleware lo rechaza como Bearer
    y no sirve para llamar a la API.

    Args:
        user_id: Usuario cuyo email se verifica.
        client_id: Tenant del usuario.
        email: Email verificado; si el usuario lo cambia despues, el token deja de valer.

    Returns:
        Token JWT codificado, con `EMAIL_VERIFICATION_TTL_HOURS` de vida.
    """
    payload = {
        "user_id": user_id,
        "client_id": client_id,
        "email": email,
        "type": "email_verification",
    }
    return _firmar(payload, timedelta(hours=get_settings().EMAIL_VERIFICATION_TTL_HOURS))


def decode_jwt(token: str) -> dict[str, Any]:
    """Decodifica y valida un token JWT.

    Comprueba firma, `exp`, `iss` y `aud`, y que traiga todos los claims obligatorios.

    Args:
        token: Token JWT codificado.

    Returns:
        Payload decodificado del token.

    Raises:
        JWTExpiredError: Si el token ha expirado.
        JWTInvalidError: Si el token es inválido, corrupto o de otro emisor.
    """
    ajustes = get_settings()
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            ajustes.JWT_SECRET,
            algorithms=[ajustes.JWT_ALGORITHM],
            audience=ajustes.JWT_AUDIENCE,
            issuer=ajustes.JWT_ISSUER,
            options={"require": _CLAIMS_OBLIGATORIOS},
        )
        return payload
    except jwt.ExpiredSignatureError as err:
        raise JWTExpiredError("Token expirado") from err
    except jwt.PyJWTError as err:
        raise JWTInvalidError("Token inválido") from err
