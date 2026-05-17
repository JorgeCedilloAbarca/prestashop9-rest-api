# ============================================================
#  core/auth.py — Autenticación JWT con roles
#
#  Implementa:
#    - Hashing seguro de contraseñas con bcrypt
#    - Generación y validación de access tokens (JWT)
#    - Generación de refresh tokens
#    - Dependencias FastAPI para proteger endpoints por rol
#    - Gestión de usuarios en MySQL (tabla api_users)
# ============================================================

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
import mysql.connector
from loguru import logger

from config import settings


# ──────────────────────────────────────────────────────────────
# Hashing de contraseñas
# ──────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    """Genera el hash bcrypt de una contraseña en texto plano."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Verifica que una contraseña en texto plano coincide con su hash."""
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────
# Tokens JWT
# ──────────────────────────────────────────────────────────────

def _secret() -> str:
    """Obtiene la clave secreta JWT. Lanza error si no está configurada."""
    key = settings.jwt_secret_key
    if not key:
        raise RuntimeError(
            "JWT_SECRET_KEY no está configurada en el .env. "
            "Genera una con: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    return key


def create_access_token(username: str, role: str) -> str:
    """
    Genera un JWT de acceso con los claims del usuario.
    Expira según JWT_ACCESS_TOKEN_EXPIRE_MINUTES del .env.
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub":  username,
        "role": role,
        "type": "access",
        "iat":  now,
        "exp":  now + timedelta(minutes=settings.jwt_access_token_expire_minutes),
    }
    return jwt.encode(payload, _secret(), algorithm=settings.jwt_algorithm)


def create_refresh_token(username: str) -> str:
    """
    Genera un JWT de refresco de larga duración.
    Solo contiene el username — no tiene privilegios de acceso.
    Expira según JWT_REFRESH_TOKEN_EXPIRE_DAYS del .env.
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub":  username,
        "type": "refresh",
        "iat":  now,
        "exp":  now + timedelta(days=settings.jwt_refresh_token_expire_days),
    }
    return jwt.encode(payload, _secret(), algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    """
    Decodifica y valida un JWT. Lanza jwt.PyJWTError si es inválido o expirado.
    Devuelve el payload como dict.
    """
    return jwt.decode(token, _secret(), algorithms=[settings.jwt_algorithm])


# ──────────────────────────────────────────────────────────────
# Gestión de usuarios en MySQL
# ──────────────────────────────────────────────────────────────

class UserManager:
    """
    Gestiona los usuarios de la API almacenados en la tabla api_users.
    Opera directamente sobre MySQL sin pasar por el pool del DatabaseHandler
    para mantener la independencia de la capa de autenticación.
    """

    def __init__(self) -> None:
        self._config = settings.db_config

    def _conn(self):
        """Conexión MySQL directa con timeout. use_pure=True evita crashes de la extensión C en Windows."""
        return mysql.connector.connect(**self._config, connection_timeout=5, use_pure=True)

    def get_user(self, username: str) -> Optional[dict]:
        """
        Busca un usuario activo por username.
        Devuelve dict con id, username, hashed_password, role o None.
        """
        conn = None
        try:
            conn = self._conn()
            cur = conn.cursor(dictionary=True)
            cur.execute(
                "SELECT id, username, hashed_password, role, active "
                "FROM api_users WHERE username = %s AND active = 1 LIMIT 1",
                (username,),
            )
            return cur.fetchone()
        except mysql.connector.Error as exc:
            logger.error("Error obteniendo usuario '{u}': {e}", u=username, e=exc)
            return None
        finally:
            if conn and conn.is_connected():
                conn.close()

    def authenticate(self, username: str, password: str) -> Optional[dict]:
        """
        Autentica un usuario por username y contraseña.
        Devuelve el dict del usuario si es correcto, None si falla.
        Actualiza last_login en caso de éxito.
        """
        user = self.get_user(username)
        if not user:
            logger.warning("Login fallido — usuario no encontrado: '{u}'", u=username)
            return None
        if not verify_password(password, user["hashed_password"]):
            logger.warning("Login fallido — contraseña incorrecta: '{u}'", u=username)
            return None
        self._update_last_login(user["id"])
        logger.info("Login exitoso: '{u}' (rol: {r})", u=username, r=user["role"])
        return user

    def _update_last_login(self, user_id: int) -> None:
        conn = None
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                "UPDATE api_users SET last_login = NOW() WHERE id = %s",
                (user_id,),
            )
            conn.commit()
        except mysql.connector.Error as exc:
            logger.warning("No se pudo actualizar last_login: {e}", e=exc)
        finally:
            if conn and conn.is_connected():
                conn.close()

    def create_user(self, username: str, password: str, role: str = "readonly") -> bool:
        """
        Crea un nuevo usuario. Devuelve True si se creó correctamente.
        El role debe ser 'admin' o 'readonly'.
        """
        if role not in ("admin", "readonly"):
            raise ValueError(f"Role '{role}' no válido. Usa 'admin' o 'readonly'.")
        hashed = hash_password(password)
        conn = None
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO api_users (username, hashed_password, role) VALUES (%s, %s, %s)",
                (username, hashed, role),
            )
            conn.commit()
            logger.info("Usuario '{u}' creado con rol '{r}'.", u=username, r=role)
            return True
        except mysql.connector.IntegrityError:
            logger.warning("El usuario '{u}' ya existe.", u=username)
            return False
        except mysql.connector.Error as exc:
            logger.error("Error creando usuario '{u}': {e}", u=username, e=exc)
            return False
        finally:
            if conn and conn.is_connected():
                conn.close()

    def change_password(self, username: str, new_password: str) -> bool:
        """Actualiza la contraseña de un usuario existente."""
        hashed = hash_password(new_password)
        conn = None
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                "UPDATE api_users SET hashed_password = %s WHERE username = %s",
                (hashed, username),
            )
            conn.commit()
            filas = cur.rowcount
            if filas:
                logger.info("Contraseña cambiada para '{u}'.", u=username)
            return filas > 0
        except mysql.connector.Error as exc:
            logger.error("Error cambiando contraseña de '{u}': {e}", u=username, e=exc)
            return False
        finally:
            if conn and conn.is_connected():
                conn.close()

    def deactivate_user(self, username: str) -> bool:
        """Desactiva un usuario (no lo elimina)."""
        conn = None
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                "UPDATE api_users SET active = 0 WHERE username = %s",
                (username,),
            )
            conn.commit()
            logger.info("Usuario '{u}' desactivado.", u=username)
            return cur.rowcount > 0
        except mysql.connector.Error as exc:
            logger.error("Error desactivando usuario '{u}': {e}", u=username, e=exc)
            return False
        finally:
            if conn and conn.is_connected():
                conn.close()

    def list_users(self) -> list:
        """Lista todos los usuarios (sin exponer el hash de contraseña)."""
        conn = None
        try:
            conn = self._conn()
            cur = conn.cursor(dictionary=True)
            cur.execute(
                "SELECT id, username, role, active, created_at, last_login "
                "FROM api_users ORDER BY id ASC"
            )
            return cur.fetchall() or []
        except mysql.connector.Error as exc:
            logger.error("Error listando usuarios: {e}", e=exc)
            return []
        finally:
            if conn and conn.is_connected():
                conn.close()


# ──────────────────────────────────────────────────────────────
# Dependencias FastAPI
# ──────────────────────────────────────────────────────────────

from fastapi import Depends, HTTPException, Request, status

user_manager = UserManager()


def require_auth(request: Request) -> dict:
    """
    Dependencia que extrae el usuario del request.state inyectado por el middleware JWT.
    Lanza 401 si no hay sesión activa.
    """
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token de autenticación requerido.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def require_admin(request: Request) -> dict:
    """
    Dependencia que exige rol admin.
    El middleware ya validó el token — aquí solo comprobamos el rol.
    """
    user = require_auth(request)
    if user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acceso denegado. Se requiere rol 'admin'.",
        )
    return user
