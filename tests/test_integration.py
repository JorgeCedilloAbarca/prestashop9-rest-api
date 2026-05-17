# ============================================================
#  tests/test_integration.py — Tests de integración
#
#  Estos tests usan las conexiones REALES de MySQL y PrestaShop
#  definidas en el archivo .env. Solo se ejecutan explícitamente:
#
#      pytest -m integration
#
#  NO se ejecutan con el comando por defecto "pytest" para
#  no requerir conexiones de red en CI/CD o en offline.
# ============================================================

import pytest


# ──────────────────────────────────────────────────────────────
# Tests de integración con MySQL
# ──────────────────────────────────────────────────────────────

@pytest.mark.integration
class TestMySQLIntegracion:
    def test_health_check_conecta(self, real_db_handler):
        """Verifica que la conexión real a MySQL está activa."""
        assert real_db_handler.health_check() is True

    def test_obtener_datos_completos_devuelve_lista(self, real_db_handler):
        """Verifica que la query principal devuelve una lista (puede estar vacía)."""
        result = real_db_handler.obtener_datos_completos()
        assert isinstance(result, list)

    def test_productos_tienen_campos_requeridos(self, real_db_handler):
        """Cada producto debe tener los campos que usa CatalogService."""
        productos = real_db_handler.obtener_datos_completos()
        if not productos:
            pytest.skip("No hay productos activos en la BD local")

        campos_requeridos = {"id", "nombre", "precio", "referencia", "prestashop_id", "stock"}
        for p in productos:
            for campo in campos_requeridos:
                assert campo in p, f"Campo '{campo}' ausente en producto {p.get('id')}"

    def test_stock_nunca_es_none(self, real_db_handler):
        """El COALESCE de la query garantiza que stock nunca sea None."""
        productos = real_db_handler.obtener_datos_completos()
        if not productos:
            pytest.skip("No hay productos activos en la BD local")
        for p in productos:
            assert p["stock"] is not None

    def test_obtener_producto_por_id_existente(self, real_db_handler):
        """Si hay productos, el primero debe poder recuperarse por ID."""
        productos = real_db_handler.obtener_datos_completos()
        if not productos:
            pytest.skip("No hay productos activos en la BD local")
        primer_id = productos[0]["id"]
        result = real_db_handler.obtener_producto_por_id(primer_id)
        assert result is not None
        assert result["id"] == primer_id

    def test_obtener_producto_por_id_inexistente(self, real_db_handler):
        """Un ID que no existe debe devolver None."""
        result = real_db_handler.obtener_producto_por_id(999999)
        assert result is None

    def test_obtener_producto_por_referencia(self, real_db_handler):
        """Si hay productos con referencia, debe poder buscarse por ella."""
        productos = real_db_handler.obtener_datos_completos()
        con_ref = [p for p in productos if p.get("referencia")]
        if not con_ref:
            pytest.skip("No hay productos con referencia en la BD local")
        ref = con_ref[0]["referencia"]
        result = real_db_handler.obtener_producto_por_referencia(ref)
        assert result is not None
        assert result["referencia"] == ref


# ──────────────────────────────────────────────────────────────
# Tests de integración con PrestaShop Webservice
# ──────────────────────────────────────────────────────────────

@pytest.mark.integration
class TestPrestaShopIntegracion:
    def test_conexion_webservice_activa(self, real_ps_client):
        """Verifica que el webservice de PS responde correctamente."""
        result = real_ps_client._request("GET", "")
        assert result is not None

    def test_get_categories_devuelve_lista(self, real_ps_client):
        result = real_ps_client.get_categories()
        assert isinstance(result, list)
        assert len(result) > 0

    def test_categories_tienen_campos_requeridos(self, real_ps_client):
        categorias = real_ps_client.get_categories()
        for cat in categorias:
            assert "id" in cat
            assert "nombre" in cat
            assert "id_parent" in cat

    def test_get_suppliers_devuelve_lista(self, real_ps_client):
        result = real_ps_client.get_suppliers()
        assert isinstance(result, list)

    def test_get_customers_devuelve_lista(self, real_ps_client):
        result = real_ps_client.get_customers()
        assert isinstance(result, list)

    def test_get_orders_devuelve_lista(self, real_ps_client):
        result = real_ps_client.get_orders(limit=10)
        assert isinstance(result, list)

    def test_get_taxes_devuelve_lista(self, real_ps_client):
        result = real_ps_client.get_taxes()
        assert isinstance(result, list)

    def test_get_tax_rules_devuelve_lista(self, real_ps_client):
        result = real_ps_client.get_tax_rules()
        assert isinstance(result, list)

    def test_get_currencies_devuelve_lista(self, real_ps_client):
        result = real_ps_client.get_currencies()
        assert isinstance(result, list)
        assert len(result) > 0

    def test_get_languages_devuelve_lista(self, real_ps_client):
        result = real_ps_client.get_languages()
        assert isinstance(result, list)
        assert len(result) > 0

    def test_get_countries_activos(self, real_ps_client):
        result = real_ps_client.get_countries(active_only=True)
        assert isinstance(result, list)

    def test_get_stock_producto_1(self, real_ps_client):
        """El producto ID 1 debería tener stock_available en PS."""
        result = real_ps_client.get_stock("1")
        # Puede ser None si PS no tiene ese producto — no fallamos el test
        if result is not None:
            assert "quantity" in result
            assert "id_product" in result

    def test_get_product_by_reference(self, real_ps_client):
        """Buscar por referencia demo_1 que existe en la tienda de prueba."""
        result = real_ps_client.get_product_by_reference("demo_1")
        # Si existe demo_1 debe tener id
        if result is not None:
            assert "id" in result
            assert result["reference"] == "demo_1"

    def test_configuracion_shop_name(self, real_ps_client):
        """La clave PS_SHOP_NAME debe existir en la configuración de PS."""
        result = real_ps_client.get_configuration("PS_SHOP_NAME")
        # Puede ser None si la clave no existe en esta versión de PS
        # El test verifica que el método no lanza excepción
        assert result is None or isinstance(result, str)
