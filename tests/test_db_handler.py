# ============================================================
#  tests/test_db_handler.py — Tests unitarios de DatabaseHandler
#
#  Verifican la lógica SQL y el manejo de errores sin abrir
#  ninguna conexión MySQL real. El pool se mockea completamente.
# ============================================================

import pytest
from unittest.mock import MagicMock, patch
from decimal import Decimal


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def _mock_conn(rows=None, rowcount=1):
    """
    Crea un mock de conexión MySQL con cursor preconfigurado.
    rows: lista de dicts que devuelve fetchall/fetchone.
    rowcount: filas afectadas por execute (para UPDATE/DELETE).
    """
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    conn.is_connected.return_value = True

    if rows is not None:
        if isinstance(rows, list):
            cursor.fetchall.return_value = rows
            cursor.fetchone.return_value = rows[0] if rows else None
        else:
            cursor.fetchone.return_value = rows

    cursor.rowcount = rowcount
    return conn, cursor


# ──────────────────────────────────────────────────────────────
# Tests de obtener_datos_completos
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestObtenerDatosCompletos:
    def test_devuelve_lista_de_productos(self, mock_db_handler):
        productos = [
            {"id": 1, "nombre": "Camiseta", "precio": Decimal("19.99"),
             "referencia": "CAM-001", "prestashop_id": None,
             "nombre_categoria": "Ropa", "nombre_proveedor": "Prov1", "stock": 10},
            {"id": 2, "nombre": "Pantalón", "precio": Decimal("39.99"),
             "referencia": "PAN-001", "prestashop_id": "5",
             "nombre_categoria": "Ropa", "nombre_proveedor": "Prov1", "stock": 5},
        ]
        conn, _ = _mock_conn(rows=productos)
        mock_db_handler._pool.get_connection.return_value = conn

        result = mock_db_handler.obtener_datos_completos()
        assert len(result) == 2
        assert result[0]["nombre"] == "Camiseta"
        assert result[1]["prestashop_id"] == "5"

    def test_error_mysql_devuelve_lista_vacia(self, mock_db_handler):
        import mysql.connector
        mock_db_handler._pool.get_connection.side_effect = mysql.connector.Error("conn fail")
        result = mock_db_handler.obtener_datos_completos()
        assert result == []

    def test_sin_productos_devuelve_lista_vacia(self, mock_db_handler):
        conn, _ = _mock_conn(rows=[])
        mock_db_handler._pool.get_connection.return_value = conn
        result = mock_db_handler.obtener_datos_completos()
        assert result == []

    def test_cierra_conexion_siempre(self, mock_db_handler):
        """La conexión debe cerrarse aunque no haya resultados."""
        conn, _ = _mock_conn(rows=[])
        mock_db_handler._pool.get_connection.return_value = conn
        mock_db_handler.obtener_datos_completos()
        conn.close.assert_called_once()

    def test_cierra_conexion_en_caso_de_error(self, mock_db_handler):
        """La conexión debe cerrarse aunque se produzca un error."""
        import mysql.connector
        conn = MagicMock()
        conn.is_connected.return_value = True
        conn.cursor.side_effect = mysql.connector.Error("cursor fail")
        mock_db_handler._pool.get_connection.return_value = conn
        mock_db_handler.obtener_datos_completos()
        conn.close.assert_called_once()


# ──────────────────────────────────────────────────────────────
# Tests de guardar_vinculacion
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestGuardarVinculacion:
    def test_vinculacion_exitosa_devuelve_true(self, mock_db_handler):
        conn, cursor = _mock_conn(rowcount=1)
        mock_db_handler._pool.get_connection.return_value = conn
        result = mock_db_handler.guardar_vinculacion(id_local=1, id_ps="42")
        assert result is True
        conn.commit.assert_called_once()

    def test_zero_filas_afectadas_devuelve_false(self, mock_db_handler):
        """Si el UPDATE no afecta ninguna fila, el producto local no existe."""
        conn, cursor = _mock_conn(rowcount=0)
        mock_db_handler._pool.get_connection.return_value = conn
        result = mock_db_handler.guardar_vinculacion(id_local=999, id_ps="42")
        assert result is False

    def test_error_mysql_devuelve_false(self, mock_db_handler):
        import mysql.connector
        conn = MagicMock()
        conn.is_connected.return_value = True
        conn.cursor.side_effect = mysql.connector.Error("update fail")
        mock_db_handler._pool.get_connection.return_value = conn
        result = mock_db_handler.guardar_vinculacion(1, "42")
        assert result is False

    def test_ejecuta_query_con_parametros_correctos(self, mock_db_handler):
        """Verifica que el UPDATE recibe los parámetros en el orden correcto."""
        conn, cursor = _mock_conn(rowcount=1)
        mock_db_handler._pool.get_connection.return_value = conn
        mock_db_handler.guardar_vinculacion(id_local=5, id_ps="99")
        cursor.execute.assert_called_once()
        args = cursor.execute.call_args[0]
        assert args[1] == ("99", 5)  # (id_ps, id_local)


# ──────────────────────────────────────────────────────────────
# Tests de obtener_producto_por_id
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestObtenerProductoPorId:
    def test_producto_existente_devuelve_dict(self, mock_db_handler):
        producto = {
            "id": 3, "nombre": "Sudadera", "precio": Decimal("29.99"),
            "referencia": "SUD-001", "prestashop_id": None, "stock": 8,
        }
        conn, _ = _mock_conn(rows=producto)
        mock_db_handler._pool.get_connection.return_value = conn
        result = mock_db_handler.obtener_producto_por_id(3)
        assert result is not None
        assert result["nombre"] == "Sudadera"

    def test_producto_inexistente_devuelve_none(self, mock_db_handler):
        conn, cursor = _mock_conn(rows=None)
        cursor.fetchone.return_value = None
        mock_db_handler._pool.get_connection.return_value = conn
        result = mock_db_handler.obtener_producto_por_id(9999)
        assert result is None


# ──────────────────────────────────────────────────────────────
# Tests de obtener_producto_por_referencia
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestObtenerProductoPorReferencia:
    def test_referencia_existente_devuelve_dict(self, mock_db_handler):
        producto = {
            "id": 1, "nombre": "Camiseta", "precio": Decimal("19.99"),
            "referencia": "CAM-001", "prestashop_id": "42", "stock": 10,
        }
        conn, cursor = _mock_conn(rows=producto)
        cursor.fetchone.return_value = producto
        mock_db_handler._pool.get_connection.return_value = conn
        result = mock_db_handler.obtener_producto_por_referencia("CAM-001")
        assert result is not None
        assert result["referencia"] == "CAM-001"

    def test_referencia_inexistente_devuelve_none(self, mock_db_handler):
        conn, cursor = _mock_conn(rows=None)
        cursor.fetchone.return_value = None
        mock_db_handler._pool.get_connection.return_value = conn
        result = mock_db_handler.obtener_producto_por_referencia("NOEXISTE")
        assert result is None


# ──────────────────────────────────────────────────────────────
# Tests de health_check
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestHealthCheck:
    def test_conexion_activa_devuelve_true(self, mock_db_handler):
        conn, _ = _mock_conn(rows=[{"1": 1}])
        mock_db_handler._pool.get_connection.return_value = conn
        assert mock_db_handler.health_check() is True

    def test_error_conexion_devuelve_false(self, mock_db_handler):
        import mysql.connector
        mock_db_handler._pool.get_connection.side_effect = mysql.connector.Error("no conn")
        assert mock_db_handler.health_check() is False
