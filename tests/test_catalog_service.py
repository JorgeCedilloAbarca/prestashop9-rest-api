# ============================================================
#  tests/test_catalog_service.py — Tests de CatalogService
#
#  Verifica la lógica de sincronización: resolución de categorías
#  y proveedores, creación de productos, manejo de errores y
#  el objeto SyncResult.
# ============================================================

import pytest
from unittest.mock import MagicMock, patch
from services.catalog_service import CatalogService, SyncResult


# ──────────────────────────────────────────────────────────────
# Fixtures específicos
# ──────────────────────────────────────────────────────────────

@pytest.fixture
def mock_db(mocker):
    """
    Mock limpio de DatabaseHandler que evita la conexión real al pool MySQL.
    Usa mocker para parchear _init_pool antes de que __init__ lo llame.
    """
    mocker.patch("database.db_handler.MySQLConnectionPool")
    from database.db_handler import DatabaseHandler
    db = DatabaseHandler.__new__(DatabaseHandler)
    db._pool = mocker.MagicMock()
    db.obtener_datos_completos = mocker.MagicMock(return_value=[])
    db.obtener_producto_por_id = mocker.MagicMock(return_value=None)
    db.guardar_vinculacion = mocker.MagicMock(return_value=True)
    db.health_check = mocker.MagicMock(return_value=True)
    return db


@pytest.fixture
def mock_ps(mocker):
    """Mock limpio de PrestashopClient sin llamadas HTTP reales."""
    mocker.patch("core.ps_client.requests.Session")
    from core.ps_client import PrestashopClient
    ps = PrestashopClient.__new__(PrestashopClient)
    ps.base_url = "https://test.example.com/api"
    ps.session = mocker.MagicMock()
    ps.handle_resource = mocker.MagicMock(return_value="3")
    ps.create_product = mocker.MagicMock(return_value=None)
    ps.update_stock = mocker.MagicMock(return_value=True)
    ps.upload_image = mocker.MagicMock(return_value=True)
    ps._request = mocker.MagicMock(return_value=None)
    ps._slugify = mocker.MagicMock(return_value="test-slug")
    return ps


@pytest.fixture
def catalog(mock_ps, mock_db):
    """CatalogService con dependencias completamente mockeadas."""
    return CatalogService(mock_ps, mock_db)


@pytest.fixture
def productos_base():
    """Lista de productos de prueba para simular la BD local."""
    return [
        {
            "id": 1, "nombre": "Camiseta Roja", "precio": 19.99,
            "referencia": "CAM-R-001", "prestashop_id": None,
            "nombre_categoria": "Hombre", "nombre_proveedor": "Proveedor A", "stock": 10,
        },
        {
            "id": 2, "nombre": "Pantalón Azul", "precio": 39.99,
            "referencia": "PAN-A-001", "prestashop_id": "5",
            "nombre_categoria": "Mujer", "nombre_proveedor": "Proveedor B", "stock": 3,
        },
    ]


# ──────────────────────────────────────────────────────────────
# Tests de SyncResult
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestSyncResult:
    def test_resultado_vacio_es_exitoso(self):
        r = SyncResult()
        assert r.exitoso is True
        assert r.total == 0

    def test_con_errores_no_es_exitoso(self):
        r = SyncResult()
        r.errores.append("algo falló")
        assert r.exitoso is False

    def test_to_dict_contiene_todos_los_campos(self):
        r = SyncResult(total=5, creados=3, stock_actualizados=5, imagenes_subidas=2)
        d = r.to_dict()
        assert d["total_productos"] == 5
        assert d["productos_creados"] == 3
        assert d["stocks_actualizados"] == 5
        assert d["imagenes_subidas"] == 2
        assert d["errores"] == []
        assert d["exitoso"] is True

    def test_to_dict_con_errores(self):
        r = SyncResult()
        r.errores.append("Error en producto 3")
        d = r.to_dict()
        assert d["exitoso"] is False
        assert len(d["errores"]) == 1


# ──────────────────────────────────────────────────────────────
# Tests de sincronizar_todo
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestSincronizarTodo:
    def test_sin_productos_devuelve_total_cero(self, catalog, mock_db):
        mock_db.obtener_datos_completos.return_value = []
        resultado = catalog.sincronizar_todo()
        assert resultado.total == 0
        assert resultado.creados == 0

    def test_producto_nuevo_se_crea_y_vincula(self, catalog, mock_ps, mock_db, productos_base):
        # Solo el primer producto (sin prestashop_id)
        mock_db.obtener_datos_completos.return_value = [productos_base[0]]
        mock_ps.handle_resource.return_value = "3"
        mock_ps.create_product.return_value = "42"
        mock_ps.update_stock.return_value = True
        mock_db.guardar_vinculacion.return_value = True

        resultado = catalog.sincronizar_todo()

        assert resultado.total == 1
        assert resultado.creados == 1
        mock_ps.create_product.assert_called_once()
        mock_db.guardar_vinculacion.assert_called_once_with(1, "42")

    def test_producto_ya_vinculado_no_se_crea(self, catalog, mock_ps, mock_db, productos_base):
        # Solo el segundo producto (con prestashop_id="5")
        mock_db.obtener_datos_completos.return_value = [productos_base[1]]
        mock_ps.handle_resource.return_value = "3"
        mock_ps.update_stock.return_value = True

        resultado = catalog.sincronizar_todo()

        assert resultado.total == 1
        assert resultado.creados == 0
        mock_ps.create_product.assert_not_called()

    def test_fallo_al_crear_producto_registra_error(self, catalog, mock_ps, mock_db, productos_base):
        mock_db.obtener_datos_completos.return_value = [productos_base[0]]
        mock_ps.handle_resource.return_value = "3"
        mock_ps.create_product.return_value = None

        resultado = catalog.sincronizar_todo()

        assert resultado.creados == 0
        assert len(resultado.errores) == 1
        assert resultado.exitoso is False

    def test_fallo_en_un_producto_no_detiene_el_resto(self, catalog, mock_ps, mock_db, productos_base):
        """Si un producto falla, el resto debe procesarse igualmente."""
        mock_db.obtener_datos_completos.return_value = productos_base
        mock_ps.handle_resource.return_value = "3"
        mock_ps.create_product.return_value = None
        mock_ps.update_stock.return_value = True

        resultado = catalog.sincronizar_todo()

        assert resultado.total == 2
        # El segundo (ya vinculado) actualizó stock aunque el primero falló
        assert resultado.stock_actualizados == 1

    def test_stock_se_actualiza_para_productos_vinculados(self, catalog, mock_ps, mock_db, productos_base):
        mock_db.obtener_datos_completos.return_value = [productos_base[1]]
        mock_ps.handle_resource.return_value = "3"
        mock_ps.update_stock.return_value = True

        resultado = catalog.sincronizar_todo()

        mock_ps.update_stock.assert_called_once_with("5", 3)
        assert resultado.stock_actualizados == 1

    def test_imagen_se_sube_si_existe(self, catalog, mock_ps, mock_db, productos_base, tmp_path):
        """Si existe la imagen en disco debe intentar subirla."""
        import os
        # Crear imagen fake
        img_path = tmp_path / "CAM-R-001.jpg"
        img_path.write_bytes(b"fake_image")

        producto = {**productos_base[0], "prestashop_id": "42"}
        mock_db.obtener_datos_completos.return_value = [producto]
        mock_ps.handle_resource.return_value = "3"
        mock_ps.update_stock.return_value = True
        mock_ps.upload_image.return_value = True

        with patch("services.catalog_service.settings") as mock_settings:
            mock_settings.img_path = str(tmp_path) + os.sep
            resultado = catalog.sincronizar_todo()

        mock_ps.upload_image.assert_called_once()
        assert resultado.imagenes_subidas == 1

    def test_excepcion_inesperada_registra_error_y_continua(self, catalog, mock_ps, mock_db, productos_base):
        """Una excepción no capturada en un producto no debe crashear la sincronización."""
        mock_db.obtener_datos_completos.return_value = productos_base
        mock_ps.handle_resource.side_effect = [Exception("error inesperado"), "3"]
        mock_ps.update_stock.return_value = True

        resultado = catalog.sincronizar_todo()

        assert resultado.total == 2
        assert len(resultado.errores) >= 1


# ──────────────────────────────────────────────────────────────
# Tests de sincronizar_producto_unico
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestSincronizarProductoUnico:
    def test_producto_no_encontrado_devuelve_error(self, catalog, mock_db):
        mock_db.obtener_producto_por_id.return_value = None
        resultado = catalog.sincronizar_producto_unico(999)
        assert resultado.total == 0
        assert len(resultado.errores) == 1

    def test_producto_encontrado_se_sincroniza(self, catalog, mock_ps, mock_db):
        producto = {
            "id": 1, "nombre": "Test", "precio": 10.0,
            "referencia": "T-001", "prestashop_id": None,
            "nombre_proveedor": "", "stock": 5,
        }
        mock_db.obtener_producto_por_id.return_value = producto
        mock_ps.handle_resource.return_value = "2"
        mock_ps.create_product.return_value = "10"
        mock_ps.update_stock.return_value = True
        mock_db.guardar_vinculacion.return_value = True

        resultado = catalog.sincronizar_producto_unico(1)

        assert resultado.total == 1
        assert resultado.creados == 1
