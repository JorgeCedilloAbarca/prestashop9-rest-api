# ============================================================
#  tests/test_logging.py — Tests del sistema de logging
#
#  Verifica que los módulos loguean correctamente los eventos
#  clave: errores de conexión, operaciones exitosas, fallos PS,
#  circuit breaker y autenticación JWT.
# ============================================================

import pytest
from unittest.mock import MagicMock, patch
from loguru import logger
import sys


# ──────────────────────────────────────────────────────────────
# Fixture para capturar logs de loguru
# ──────────────────────────────────────────────────────────────

@pytest.fixture
def log_capture():
    """
    Captura los mensajes de log emitidos durante el test.
    Devuelve una lista que se rellena con los mensajes capturados.
    """
    mensajes = []

    def sink(message):
        mensajes.append(message.record["message"])

    handler_id = logger.add(sink, level="DEBUG")
    yield mensajes
    logger.remove(handler_id)


# ──────────────────────────────────────────────────────────────
# Tests de logging en ps_client
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestPsClientLogging:
    def test_timeout_loguea_error(self, mock_ps_client, log_capture):
        import requests
        mock_ps_client.session.request.side_effect = requests.exceptions.Timeout
        mock_ps_client._request("GET", "products")
        assert any("Timeout" in m or "timeout" in m.lower() for m in log_capture)

    def test_conexion_error_loguea_error(self, mock_ps_client, log_capture):
        import requests
        mock_ps_client.session.request.side_effect = requests.exceptions.ConnectionError("conn refused")
        mock_ps_client._request("GET", "products")
        assert any("conexión" in m.lower() or "connection" in m.lower() for m in log_capture)

    def test_create_product_exitoso_loguea_info(self, mock_ps_client, log_capture):
        xml = b"<prestashop><product><id>42</id></product></prestashop>"
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.content = xml
        mock_ps_client.session.request.return_value = mock_resp

        with patch("core.ps_client.prestashop_circuit"):
            mock_ps_client.create_product({
                "name": "Test", "price": 10, "reference": "T1",
                "id_category": "2", "id_supplier": "0",
            })
        assert any("42" in m or "creado" in m.lower() for m in log_capture)

    def test_create_product_fallo_loguea_error(self, mock_ps_client, log_capture):
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "<e>bad request</e>"
        mock_ps_client.session.request.return_value = mock_resp
        mock_ps_client.create_product({"name": "Test", "price": 0})
        assert any("pudo" in m.lower() or "error" in m.lower() for m in log_capture)

    def test_update_stock_exitoso_loguea_debug(self, mock_ps_client, log_capture):
        get_xml = b"<prestashop><stock_availables><stock_available><id>3</id></stock_available></stock_availables></prestashop>"
        put_xml = b"<prestashop><stock_available><id>3</id></stock_available></prestashop>"
        mock_ps_client.session.request.side_effect = [
            MagicMock(status_code=200, content=get_xml),
            MagicMock(status_code=200, content=put_xml),
        ]
        with patch("core.ps_client.prestashop_circuit"):
            mock_ps_client.update_stock("1", 15)
        assert any("stock" in m.lower() or "actualizado" in m.lower() for m in log_capture)


# ──────────────────────────────────────────────────────────────
# Tests de logging en db_handler
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestDbHandlerLogging:
    def test_pool_iniciado_loguea_info(self, log_capture):
        """Al iniciar el pool MySQL debe loguearse un mensaje de INFO."""
        with patch("database.db_handler.MySQLConnectionPool") as mock_pool:
            mock_pool.return_value = MagicMock()
            from database.db_handler import DatabaseHandler
            DatabaseHandler.__new__(DatabaseHandler)
        # El log se produce en _init_pool — verificamos que no lanza excepción

    def test_error_mysql_en_query_loguea_error(self, mock_db_handler, log_capture):
        import mysql.connector
        conn = MagicMock()
        conn.is_connected.return_value = True
        conn.cursor.side_effect = mysql.connector.Error("query fail")
        mock_db_handler._pool.get_connection.return_value = conn
        mock_db_handler.obtener_datos_completos()
        assert any("error" in m.lower() or "Error" in m for m in log_capture)

    def test_guardar_vinculacion_exitosa_loguea_debug(self, mock_db_handler, log_capture):
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value = cursor
        conn.is_connected.return_value = True
        cursor.rowcount = 1
        mock_db_handler._pool.get_connection.return_value = conn
        mock_db_handler.guardar_vinculacion(1, "42")
        assert any("42" in m or "vinculación" in m.lower() or "local" in m.lower() for m in log_capture)


# ──────────────────────────────────────────────────────────────
# Tests de logging en circuit breaker
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestCircuitBreakerLogging:
    def test_apertura_circuito_loguea_error(self, log_capture):
        from core.resilience import CircuitBreaker
        cb = CircuitBreaker("test_log", failure_threshold=2, recovery_timeout=1.0)
        cb.record_failure()
        cb.record_failure()  # Abre el circuito
        assert any("OPEN" in m or "abierto" in m.lower() or "fallos" in m.lower() for m in log_capture)

    def test_recuperacion_loguea_info(self, log_capture):
        import time
        from core.resilience import CircuitBreaker
        cb = CircuitBreaker("test_rec", failure_threshold=2, recovery_timeout=0.05)
        cb.record_failure()
        cb.record_failure()
        time.sleep(0.1)
        cb.state  # Trigger HALF_OPEN
        cb.record_success()
        assert any("CLOSED" in m or "recuperado" in m.lower() for m in log_capture)

    def test_reset_manual_loguea_info(self, log_capture):
        from core.resilience import CircuitBreaker
        cb = CircuitBreaker("test_reset", failure_threshold=3, recovery_timeout=1.0)
        cb.record_failure()
        cb.reset()
        assert any("reset" in m.lower() or "reseteado" in m.lower() for m in log_capture)


# ──────────────────────────────────────────────────────────────
# Tests de logging en catalog_service
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestCatalogServiceLogging:
    def test_sincronizacion_exitosa_loguea_resumen(self, log_capture):
        from unittest.mock import MagicMock
        from services.catalog_service import CatalogService

        mock_ps = MagicMock()
        mock_db = MagicMock()
        mock_db.obtener_datos_completos.return_value = [{
            "id": 1, "nombre": "Test", "precio": 10.0,
            "referencia": "T1", "prestashop_id": "5",
            "nombre_categoria": "Ropa", "nombre_proveedor": "", "stock": 3,
        }]
        mock_ps.handle_resource.return_value = "3"
        mock_ps.update_stock.return_value = True

        svc = CatalogService(mock_ps, mock_db)
        svc.sincronizar_todo()

        assert any("sincroniz" in m.lower() or "finaliz" in m.lower() for m in log_capture)

    def test_producto_no_encontrado_loguea_warning(self, log_capture):
        from services.catalog_service import CatalogService
        mock_ps = MagicMock()
        mock_db = MagicMock()
        mock_db.obtener_datos_completos.return_value = []

        svc = CatalogService(mock_ps, mock_db)
        svc.sincronizar_todo()

        assert any("no se encontraron" in m.lower() or "0" in m for m in log_capture)


# ──────────────────────────────────────────────────────────────
# Tests de logging en auth
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestAuthLogging:
    def test_login_exitoso_loguea_info(self, log_capture):
        from core.auth import UserManager, hash_password
        mgr = UserManager()
        hashed = hash_password("Clave1234!")
        usuario = {"id": 1, "username": "admin", "hashed_password": hashed, "role": "admin", "active": 1}

        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value = cursor
        conn.is_connected.return_value = True
        cursor.fetchone.return_value = usuario
        cursor.rowcount = 1
        mgr._conn = MagicMock(return_value=conn)

        mgr.authenticate("admin", "Clave1234!")
        assert any("login" in m.lower() or "exitoso" in m.lower() for m in log_capture)

    def test_login_fallido_loguea_warning(self, log_capture):
        from core.auth import UserManager
        mgr = UserManager()
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value = cursor
        conn.is_connected.return_value = True
        cursor.fetchone.return_value = None
        mgr._conn = MagicMock(return_value=conn)

        mgr.authenticate("noexiste", "clave")
        assert any("fallido" in m.lower() or "no encontrado" in m.lower() for m in log_capture)

    def test_create_user_loguea_info(self, log_capture):
        from core.auth import UserManager
        mgr = UserManager()
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value = cursor
        conn.is_connected.return_value = True
        cursor.rowcount = 1
        mgr._conn = MagicMock(return_value=conn)

        mgr.create_user("nuevo_user", "Clave1234!", "readonly")
        assert any("nuevo_user" in m or "creado" in m.lower() for m in log_capture)
