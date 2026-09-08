"""Seguridad: JWT y hashing de passwords.

JWT con python-jose (HS256). Password hashing con passlib/bcrypt.
Payload JWT: { user_id, client_id, email, role, exp, type }.
"""

from datetime import UTC, datetime, timedelta

from jose import ExpiredSignatureError, JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings
from app.core.exceptions import JWTExpiredError, JWTInvalidError

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    """Genera hash bcrypt de un password.

    Args:
        password: Password en texto plano.

    Returns:
        Hash bcrypt del password.
    """
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifica un password contra su hash bcrypt.

    Args:
        plain_password: Password en texto plano.
        hashed_password: Hash bcrypt almacenado.

    Returns:
        True si el password coincide.
    """
    return pwd_context.verify(plain_password, hashed_password)


def create_access_token(
    data: dict,
    expires_delta: timedelta | None = None,
) -> str:
    """Crea un access token JWT.

    Args:
        data: Payload del token (user_id, client_id, email, role).
        expires_delta: Duración personalizada. Default: JWT_EXPIRATION_MINUTES.

    Returns:
        Token JWT codificado.
    """
    to_encode = data.copy()
    expire = datetime.now(UTC) + (
        expires_delta or timedelta(minutes=settings.JWT_EXPIRATION_MINUTES)
    )
    to_encode.update({"exp": expire, "type": "access"})
    return jwt.encode(to_encode, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def create_refresh_token(data: dict) -> str:
    """Crea un refresh token JWT con expiración de 7 días.

    Args:
        data: Payload del token (user_id, client_id, email, role).

    Returns:
        Refresh token JWT codificado.
    """
    to_encode = data.copy()
    expire = datetime.now(UTC) + timedelta(days=settings.JWT_REFRESH_EXPIRATION_DAYS)
    to_encode.update({"exp": expire, "type": "refresh"})
    return jwt.encode(to_encode, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_jwt(token: str) -> dict:
    """Decodifica y valida un token JWT.

    Args:
        token: Token JWT codificado.

    Returns:
        Payload decodificado del token.

    Raises:
        JWTExpiredError: Si el token ha expirado.
        JWTInvalidError: Si el token es inválido o corrupto.
    """
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        return payload
    except ExpiredSignatureError as err:
        raise JWTExpiredError("Token expirado") from err
    except JWTError as err:
        raise JWTInvalidError("Token inválido") from err
