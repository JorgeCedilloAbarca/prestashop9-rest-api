# ============================================================
#  tests/test_auth.py — Tests unitarios de core/auth.py
#
#  Verifica hashing de contraseñas, generación/validación de
#  tokens JWT y la lógica del UserManager sin conexión real a MySQL.
# ============================================================

import time
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta, timezone


# ──────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def patch_settings(monkeypatch):
    """Inyecta una clave JWT de prueba para todos los tests de este módulo."""
    monkeypatch.setenv("JWT_SECRET_KEY", "test_secret_key_32_chars_minimum!!")
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "60")
    monkeypatch.setenv("JWT_REFRESH_TOKEN_EXPIRE_DAYS", "7")


@pytest.fixture
def user_mgr(mocker):
    """UserManager con conexión MySQL completamente mockeada."""
    with patch("core.auth.mysql.connector.connect") as mock_connect:
        from core.auth import UserManager
        mgr = UserManager()
        mgr._conn = MagicMock(return_value=mock_connect.return_value)
        yield mgr, mock_connect


# ──────────────────────────────────────────────────────────────
# Tests de hashing de contraseñas
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestPasswordHashing:
    def test_hash_genera_cadena_bcrypt(self):
        from core.auth import hash_password
        hashed = hash_password("MiContraseña123!")
        assert hashed.startswith("$2b$")
        assert len(hashed) > 50

    def test_dos_hashes_del_mismo_password_son_distintos(self):
        """bcrypt usa salt aleatorio — dos hashes del mismo texto deben diferir."""
        from core.auth import hash_password
        h1 = hash_password("misma_clave")
        h2 = hash_password("misma_clave")
        assert h1 != h2

    def test_verify_password_correcto(self):
        from core.auth import hash_password, verify_password
        hashed = hash_password("ClaveCorrecta!")
        assert verify_password("ClaveCorrecta!", hashed) is True

    def test_verify_password_incorrecto(self):
        from core.auth import hash_password, verify_password
        hashed = hash_password("ClaveCorrecta!")
        assert verify_password("ClaveIncorrecta!", hashed) is False

    def test_verify_password_hash_invalido_devuelve_false(self):
        from core.auth import verify_password
        assert verify_password("clave", "hash_invalido") is False


# ──────────────────────────────────────────────────────────────
# Tests de tokens JWT
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestTokens:
    def test_create_access_token_devuelve_string(self):
        from core.auth import create_access_token
        token = create_access_token("admin", "admin")
        assert isinstance(token, str)
        assert len(token) > 50

    def test_access_token_contiene_claims_correctos(self):
        from core.auth import create_access_token, decode_token
        token = create_access_token("jorge", "readonly")
        payload = decode_token(token)
        assert payload["sub"]  == "jorge"
        assert payload["role"] == "readonly"
        assert payload["type"] == "access"

    def test_refresh_token_tiene_tipo_refresh(self):
        from core.auth import create_refresh_token, decode_token
        token = create_refresh_token("jorge")
        payload = decode_token(token)
        assert payload["sub"]  == "jorge"
        assert payload["type"] == "refresh"

    def test_decode_token_invalido_lanza_error(self):
        import jwt
        from core.auth import decode_token
        with pytest.raises(jwt.PyJWTError):
            decode_token("token.invalido.aqui")

    def test_decode_token_expirado_lanza_expired_error(self):
        import jwt
        from core.auth import _secret
        from config import settings
        payload = {
            "sub": "test",
            "type": "access",
            "exp": datetime.now(timezone.utc) - timedelta(seconds=1),
        }
        token = jwt.encode(payload, _secret(), algorithm=settings.jwt_algorithm)
        with pytest.raises(jwt.ExpiredSignatureError):
            from core.auth import decode_token
            decode_token(token)

    def test_secret_vacia_lanza_runtime_error(self, monkeypatch):
        monkeypatch.setenv("JWT_SECRET_KEY", "")
        # Recargar settings para que tome el nuevo valor
        import importlib
        import config as cfg_module
        importlib.reload(cfg_module)
        from core.auth import _secret
        # Parchear settings directamente
        with patch("core.auth.settings") as mock_settings:
            mock_settings.jwt_secret_key = ""
            with pytest.raises(RuntimeError):
                _secret()


# ──────────────────────────────────────────────────────────────
# Tests de UserManager
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestUserManager:
    def _mock_conn(self, mocker, fetchone_result=None, rowcount=1):
        """Helper para crear una conexión MySQL mockeada."""
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value = cursor
        conn.is_connected.return_value = True
        cursor.fetchone.return_value = fetchone_result
        cursor.rowcount = rowcount
        return conn, cursor

    def test_get_user_existente(self, mocker):
        from core.auth import UserManager
        mgr = UserManager()
        usuario = {
            "id": 1, "username": "admin",
            "hashed_password": "$2b$12$xxx", "role": "admin", "active": 1,
        }
        conn, cursor = self._mock_conn(mocker, fetchone_result=usuario)
        mgr._conn = MagicMock(return_value=conn)
        result = mgr.get_user("admin")
        assert result is not None
        assert result["username"] == "admin"
        assert result["role"] == "admin"

    def test_get_user_no_existente_devuelve_none(self, mocker):
        from core.auth import UserManager
        mgr = UserManager()
        conn, _ = self._mock_conn(mocker, fetchone_result=None)
        mgr._conn = MagicMock(return_value=conn)
        result = mgr.get_user("noexiste")
        assert result is None

    def test_authenticate_credenciales_correctas(self, mocker):
        from core.auth import UserManager, hash_password
        mgr = UserManager()
        hashed = hash_password("MiClave123!")
        usuario = {
            "id": 1, "username": "admin",
            "hashed_password": hashed, "role": "admin", "active": 1,
        }
        conn, _ = self._mock_conn(mocker, fetchone_result=usuario)
        mgr._conn = MagicMock(return_value=conn)
        result = mgr.authenticate("admin", "MiClave123!")
        assert result is not None
        assert result["username"] == "admin"

    def test_authenticate_password_incorrecta_devuelve_none(self, mocker):
        from core.auth import UserManager, hash_password
        mgr = UserManager()
        hashed = hash_password("ClaveCorrecta!")
        usuario = {
            "id": 1, "username": "admin",
            "hashed_password": hashed, "role": "admin", "active": 1,
        }
        conn, _ = self._mock_conn(mocker, fetchone_result=usuario)
        mgr._conn = MagicMock(return_value=conn)
        result = mgr.authenticate("admin", "ClaveIncorrecta!")
        assert result is None

    def test_authenticate_usuario_no_existe_devuelve_none(self, mocker):
        from core.auth import UserManager
        mgr = UserManager()
        conn, _ = self._mock_conn(mocker, fetchone_result=None)
        mgr._conn = MagicMock(return_value=conn)
        result = mgr.authenticate("noexiste", "clave")
        assert result is None

    def test_create_user_exitoso(self, mocker):
        from core.auth import UserManager
        mgr = UserManager()
        conn, cursor = self._mock_conn(mocker, rowcount=1)
        mgr._conn = MagicMock(return_value=conn)
        result = mgr.create_user("nuevo", "Clave1234!", "readonly")
        assert result is True
        conn.commit.assert_called_once()

    def test_create_user_role_invalido_lanza_error(self, mocker):
        from core.auth import UserManager
        mgr = UserManager()
        with pytest.raises(ValueError):
            mgr.create_user("user", "clave1234", "superadmin")

    def test_create_user_duplicado_devuelve_false(self, mocker):
        import mysql.connector
        from core.auth import UserManager
        mgr = UserManager()
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value = cursor
        conn.is_connected.return_value = True
        cursor.execute.side_effect = mysql.connector.IntegrityError("Duplicate")
        mgr._conn = MagicMock(return_value=conn)
        result = mgr.create_user("admin", "Clave1234!", "readonly")
        assert result is False

    def test_change_password_exitoso(self, mocker):
        from core.auth import UserManager
        mgr = UserManager()
        conn, cursor = self._mock_conn(mocker, rowcount=1)
        mgr._conn = MagicMock(return_value=conn)
        result = mgr.change_password("admin", "NuevaClave123!")
        assert result is True
        conn.commit.assert_called_once()

    def test_change_password_usuario_no_existe(self, mocker):
        from core.auth import UserManager
        mgr = UserManager()
        conn, cursor = self._mock_conn(mocker, rowcount=0)
        mgr._conn = MagicMock(return_value=conn)
        result = mgr.change_password("noexiste", "NuevaClave123!")
        assert result is False

    def test_deactivate_user_exitoso(self, mocker):
        from core.auth import UserManager
        mgr = UserManager()
        conn, cursor = self._mock_conn(mocker, rowcount=1)
        mgr._conn = MagicMock(return_value=conn)
        result = mgr.deactivate_user("usuario_prueba")
        assert result is True

    def test_list_users_devuelve_lista(self, mocker):
        from core.auth import UserManager
        mgr = UserManager()
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value = cursor
        conn.is_connected.return_value = True
        cursor.fetchall.return_value = [
            {"id": 1, "username": "admin", "role": "admin",
             "active": 1, "created_at": None, "last_login": None},
        ]
        mgr._conn = MagicMock(return_value=conn)
        result = mgr.list_users()
        assert len(result) == 1
        assert result[0]["username"] == "admin"


# ──────────────────────────────────────────────────────────────
# Tests del middleware JWT en los endpoints
# ──────────────────────────────────────────────────────────────

@pytest.mark.api
class TestJWTMiddleware:
    async def test_endpoint_protegido_sin_token_devuelve_401(self, mock_ps_api, mock_db_api):
        """Un cliente sin token debe recibir 401 al acceder a rutas protegidas."""
        from unittest.mock import patch, MagicMock
        from httpx import AsyncClient, ASGITransport
        import os
        os.environ.setdefault("JWT_SECRET_KEY", "test_secret_key_32_chars_minimum!!")

        with patch("app.ps_client", mock_ps_api),              patch("app.db_handler", mock_db_api):
            from app import app
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                # Sin headers de autorización
            ) as c_sin_token:
                r = await c_sin_token.get("/products")
        assert r.status_code == 401

    async def test_endpoint_protegido_con_token_invalido_devuelve_401(self, client):
        c, _, _ = client
        r = await c.get("/products", headers={"Authorization": "Bearer token.falso"})
        assert r.status_code == 401

    async def test_endpoint_publico_sin_token_funciona(self, client):
        c, _, _ = client
        r = await c.get("/")
        assert r.status_code == 200

    async def test_auth_login_sin_token_funciona(self, client):
        """El endpoint de login debe ser accesible sin token."""
        c, _, _ = client
        # Mockeamos authenticate para que devuelva un usuario válido
        with patch("app.user_manager") as mock_um:
            mock_um.authenticate.return_value = {
                "username": "admin", "role": "admin"
            }
            r = await c.post("/auth/login?username=admin&password=Admin1234!")
            assert r.status_code == 200
            assert "access_token" in r.json()

    async def test_token_readonly_en_get_funciona(self, client, mock_db_handler):
        """Un token con rol readonly debe poder acceder a endpoints GET."""
        from core.auth import create_access_token
        token = create_access_token("usuario", "readonly")
        c, _, db = client
        db.obtener_datos_completos.return_value = []
        r = await c.get(
            "/products",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200

    async def test_token_readonly_en_post_devuelve_403(self, client):
        """Un token con rol readonly NO debe poder acceder a endpoints POST."""
        from core.auth import create_access_token
        token = create_access_token("usuario", "readonly")
        c, _, _ = client
        r = await c.post(
            "/products?name=Test&price=10&reference=T1",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 403

    async def test_token_admin_en_post_funciona(self, client):
        """Un token con rol admin debe poder acceder a endpoints POST."""
        from core.auth import create_access_token
        token = create_access_token("admin", "admin")
        c, ps, _ = client
        ps.create_product.return_value = "42"
        r = await c.post(
            "/products?name=Test&price=10&reference=T1",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
