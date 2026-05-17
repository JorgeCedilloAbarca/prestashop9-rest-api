# ============================================================
#  tests/test_api_endpoints_extended.py
#
#  Tests de endpoints no cubiertos en test_api_endpoints.py:
#  - Usuarios (CRUD completo)
#  - Proveedores
#  - Imágenes
#  - Pedidos (estados, pagos)
#  - Clientes (búsqueda por email, órdenes)
#  - Combinaciones, características, transportistas
#  - Divisas, idiomas, direcciones
#  - CMS, etiquetas, países, zonas
#  - Estadísticas
#  - Reconciliación
#  - Circuit breakers
#  - Descripción de producto
#  - Nuevos campos create/update product
# ============================================================

import pytest
from unittest.mock import MagicMock, patch


# ──────────────────────────────────────────────────────────────
# TAG: Usuarios
# ──────────────────────────────────────────────────────────────

@pytest.mark.api
class TestUsuariosEndpoints:
    async def test_list_users(self, client):
        c, _, _ = client
        with patch("app.user_manager") as mock_um:
            mock_um.list_users.return_value = [
                {"id": 1, "username": "admin", "role": "admin",
                 "active": 1, "created_at": None, "last_login": None}
            ]
            r = await c.get("/users")
        assert r.status_code == 200
        assert r.json()["total"] == 1

    async def test_create_user_exitoso(self, client):
        c, _, _ = client
        with patch("app.user_manager") as mock_um:
            mock_um.create_user.return_value = True
            r = await c.post("/users?username=nuevo&password=Clave1234!&role=readonly")
        assert r.status_code == 200
        assert r.json()["status"] == "created"

    async def test_create_user_password_corta_da_400(self, client):
        c, _, _ = client
        r = await c.post("/users?username=nuevo&password=123&role=readonly")
        assert r.status_code == 400

    async def test_create_user_rol_invalido_da_400(self, client):
        c, _, _ = client
        r = await c.post("/users?username=nuevo&password=Clave1234!&role=superadmin")
        assert r.status_code == 400

    async def test_create_user_duplicado_da_409(self, client):
        c, _, _ = client
        with patch("app.user_manager") as mock_um:
            mock_um.create_user.return_value = False
            r = await c.post("/users?username=admin&password=Clave1234!&role=readonly")
        assert r.status_code == 409

    async def test_deactivate_user_exitoso(self, client):
        c, _, _ = client
        with patch("app.user_manager") as mock_um:
            mock_um.deactivate_user.return_value = True
            r = await c.delete("/users/otro_usuario")
        assert r.status_code == 200
        assert r.json()["status"] == "deactivated"

    async def test_deactivate_user_no_encontrado(self, client):
        c, _, _ = client
        with patch("app.user_manager") as mock_um:
            mock_um.deactivate_user.return_value = False
            r = await c.delete("/users/noexiste")
        assert r.status_code == 404


# ──────────────────────────────────────────────────────────────
# TAG: Productos — nuevos campos
# ──────────────────────────────────────────────────────────────

@pytest.mark.api
class TestProductosNuevosCampos:
    async def test_create_product_todos_los_campos(self, client):
        c, ps, _ = client
        ps.create_product.return_value = "99"
        r = await c.post(
            "/products?name=Test&price=15.99&reference=T1"
            "&id_category=3&id_supplier=1&active=true&stock=10"
            "&description=desc&description_short=short"
            "&weight=0.5&ean13=1234567890123&minimal_quantity=2"
        )
        assert r.status_code == 200
        call_data = ps.create_product.call_args[0][0]
        assert call_data["weight"] == 0.5
        assert call_data["ean13"] == "1234567890123"
        assert call_data["minimal_quantity"] == 2
        assert call_data["stock"] == 10

    async def test_update_product_categoria_y_proveedor(self, client):
        c, ps, _ = client
        ps.update_product.return_value = True
        r = await c.put("/products/42?id_category=5&id_supplier=2")
        assert r.status_code == 200
        call_data = ps.update_product.call_args[0][1]
        assert call_data["id_category_default"] == 5
        assert call_data["id_supplier"] == 2

    async def test_update_product_activar_desactivar(self, client):
        c, ps, _ = client
        ps.update_product.return_value = True
        r = await c.put("/products/42?active=false")
        assert r.status_code == 200
        call_data = ps.update_product.call_args[0][1]
        assert call_data["active"] is False

    async def test_update_product_stock(self, client):
        c, ps, _ = client
        ps.update_product.return_value = True
        r = await c.put("/products/42?stock=25")
        assert r.status_code == 200
        call_data = ps.update_product.call_args[0][1]
        assert call_data["stock"] == 25

    async def test_update_product_weight_ean(self, client):
        c, ps, _ = client
        ps.update_product.return_value = True
        r = await c.put("/products/42?weight=1.5&ean13=9876543210987")
        assert r.status_code == 200
        call_data = ps.update_product.call_args[0][1]
        assert call_data["weight"] == 1.5
        assert call_data["ean13"] == "9876543210987"


# ──────────────────────────────────────────────────────────────
# TAG: Descripción
# ──────────────────────────────────────────────────────────────

@pytest.mark.api
class TestDescripcionEndpoints:
    async def test_get_description_encontrada(self, client):
        c, ps, _ = client
        ps.get_product_description.return_value = "<p>Mi descripción</p>"
        r = await c.get("/products/42/description")
        assert r.status_code == 200
        assert r.json()["description"] == "<p>Mi descripción</p>"

    async def test_get_description_producto_no_existe(self, client):
        c, ps, _ = client
        ps.get_product_description.return_value = None
        r = await c.get("/products/9999/description")
        assert r.status_code == 404

    async def test_update_description_exitoso(self, client):
        c, ps, _ = client
        ps.update_description.return_value = True
        r = await c.put("/products/42/description?description=<p>Nueva</p>")
        assert r.status_code == 200
        assert r.json()["status"] == "updated"

    async def test_update_description_falla(self, client):
        c, ps, _ = client
        ps.update_description.return_value = False
        r = await c.put("/products/999/description?description=desc")
        assert r.status_code == 502


# ──────────────────────────────────────────────────────────────
# TAG: Proveedores
# ──────────────────────────────────────────────────────────────

@pytest.mark.api
class TestProveedoresEndpoints:
    async def test_get_suppliers(self, client):
        c, ps, _ = client
        ps.get_suppliers.return_value = [{"id": "1", "nombre": "Prov A"}]
        r = await c.get("/suppliers")
        assert r.status_code == 200
        assert r.json()["total"] == 1

    async def test_create_supplier_exitoso(self, client):
        c, ps, _ = client
        ps.create_supplier.return_value = "5"
        r = await c.post("/suppliers?name=Nuevo Proveedor")
        assert r.status_code == 200
        assert r.json()["status"] == "created"

    async def test_delete_supplier_exitoso(self, client):
        c, ps, _ = client
        ps.delete_supplier.return_value = True
        r = await c.delete("/suppliers/5")
        assert r.status_code == 200


# ──────────────────────────────────────────────────────────────
# TAG: Imágenes
# ──────────────────────────────────────────────────────────────

@pytest.mark.api
class TestImagenesEndpoints:
    async def test_get_images_producto(self, client):
        c, ps, _ = client
        ps.get_product_images.return_value = [
            {"id": "1", "url": "http://test/img/1/1"},
            {"id": "2", "url": "http://test/img/1/2"},
        ]
        r = await c.get("/images/1")
        assert r.status_code == 200
        assert r.json()["total"] == 2

    async def test_delete_image_exitoso(self, client):
        c, ps, _ = client
        ps.delete_product_image.return_value = True
        r = await c.delete("/images/1/3")
        assert r.status_code == 200
        assert r.json()["status"] == "deleted"

    async def test_delete_image_falla(self, client):
        c, ps, _ = client
        ps.delete_product_image.return_value = False
        r = await c.delete("/images/1/999")
        assert r.status_code == 502


# ──────────────────────────────────────────────────────────────
# TAG: Catálogo — reconciliación
# ──────────────────────────────────────────────────────────────

@pytest.mark.api
class TestReconcileEndpoint:
    async def test_reconcile_devuelve_resumen(self, client):
        c, ps, db = client
        db.obtener_datos_completos.return_value = [
            {"id": 1, "nombre": "Prod A", "precio": 10.0, "referencia": "R1",
             "prestashop_id": "1", "stock": 5},
        ]
        # Mock de _request para productos y stocks
        xml_prods = b"""<prestashop><products>
            <product>
                <id>1</id><n><language id="1">Prod A</language></n>
                <reference>R1</reference><price>10.000000</price><active>1</active>
            </product>
        </products></prestashop>"""
        xml_stocks = b"""<prestashop><stock_availables>
            <stock_available><id_product>1</id_product><quantity>5</quantity></stock_available>
        </stock_availables></prestashop>"""

        def mock_request(method, resource, **kwargs):
            el = MagicMock()
            if "stock_availables" in resource:
                import xml.etree.ElementTree as ET
                return ET.fromstring(xml_stocks)
            elif "products" in resource:
                import xml.etree.ElementTree as ET
                return ET.fromstring(xml_prods)
            return None

        ps._request.side_effect = mock_request
        r = await c.get("/catalog/reconcile")
        assert r.status_code == 200
        data = r.json()
        assert "resumen" in data
        assert "productos" in data
        assert "ok" in data["resumen"]

    async def test_reconcile_solo_diferencias(self, client):
        c, ps, db = client
        db.obtener_datos_completos.return_value = []
        ps._request.return_value = None
        r = await c.get("/catalog/reconcile?solo_diferencias=true")
        assert r.status_code == 200


# ──────────────────────────────────────────────────────────────
# TAG: Estadísticas
# ──────────────────────────────────────────────────────────────

@pytest.mark.api
class TestEstadisticasEndpoints:
    async def test_stats_overview(self, client):
        c, ps, _ = client
        ps.get_orders_stats.return_value    = {"total_pedidos": 5, "importe_total": 250.0, "por_estado": {}}
        ps.get_products_stats.return_value  = {"total_productos": 20, "activos": 18}
        ps.get_customers_stats.return_value = {"total_clientes": 10, "activos": 9}
        ps.get_categories_stats.return_value = {"total_categorias": 6, "activas": 5}
        r = await c.get("/stats")
        assert r.status_code == 200
        data = r.json()
        assert "pedidos" in data
        assert "productos" in data

    async def test_stats_orders(self, client):
        c, ps, _ = client
        ps.get_orders_stats.return_value = {"total_pedidos": 3, "importe_total": 100.0, "por_estado": {"2": 2}}
        r = await c.get("/stats/orders")
        assert r.status_code == 200
        assert r.json()["total_pedidos"] == 3

    async def test_stats_products(self, client):
        c, ps, _ = client
        ps.get_products_stats.return_value = {
            "total_productos": 10, "activos": 8, "inactivos": 2,
            "sin_stock": 1, "precio_medio": 25.0, "precio_minimo": 5.0, "precio_maximo": 100.0,
        }
        r = await c.get("/stats/products")
        assert r.status_code == 200

    async def test_stats_customers(self, client):
        c, ps, _ = client
        ps.get_customers_stats.return_value = {"total_clientes": 5, "activos": 4, "inactivos": 1, "nuevos_30_dias": 2}
        r = await c.get("/stats/customers")
        assert r.status_code == 200

    async def test_stats_categories(self, client):
        c, ps, _ = client
        ps.get_categories_stats.return_value = {"total_categorias": 8, "activas": 7, "inactivas": 1}
        r = await c.get("/stats/categories")
        assert r.status_code == 200

    async def test_stats_falla_ps_da_502(self, client):
        c, ps, _ = client
        ps.get_orders_stats.return_value = {}
        r = await c.get("/stats/orders")
        assert r.status_code == 502


# ──────────────────────────────────────────────────────────────
# TAG: Clientes — endpoints adicionales
# ──────────────────────────────────────────────────────────────

@pytest.mark.api
class TestClientesEndpointsExtended:
    async def test_get_customer_orders(self, client, sample_order):
        c, ps, _ = client
        ps.get_customer_orders.return_value = [sample_order]
        r = await c.get("/customers/2/orders")
        assert r.status_code == 200
        assert r.json()["total_pedidos"] == 1

    async def test_search_customers_por_email(self, client, sample_customer):
        c, ps, _ = client
        ps.search_customers.return_value = [sample_customer]
        r = await c.get("/customers/search?email=john@test.com")
        assert r.status_code == 200
        assert r.json()["total"] == 1

    async def test_search_customers_no_encontrado_devuelve_404(self, client):
        c, ps, _ = client
        ps.search_customers.return_value = []
        r = await c.get("/customers/search?email=noexiste@test.com")
        assert r.status_code == 404


# ──────────────────────────────────────────────────────────────
# TAG: Pedidos — adicionales
# ──────────────────────────────────────────────────────────────

@pytest.mark.api
class TestPedidosEndpointsExtended:
    async def test_get_order_payments(self, client):
        c, ps, _ = client
        ps.get_order_payments.return_value = [
            {"id": "1", "order_reference": "REF1", "amount": "49.99", "method": "Tarjeta"}
        ]
        r = await c.get("/payments/order/REF1")
        assert r.status_code == 200

    async def test_update_order_state_falla(self, client):
        c, ps, _ = client
        ps.update_order_state.return_value = False
        r = await c.put("/orders/999/state/4")
        assert r.status_code == 502


# ──────────────────────────────────────────────────────────────
# TAG: Sistema
# ──────────────────────────────────────────────────────────────

@pytest.mark.api
class TestSistemaEndpoints:
    async def test_get_circuit_breakers(self, client):
        c, _, _ = client
        r = await c.get("/system/circuit-breakers")
        assert r.status_code == 200
        data = r.json()
        assert "circuit_breakers" in data
        assert "prestashop" in data["circuit_breakers"]
        assert "mysql" in data["circuit_breakers"]

    async def test_reset_circuit_breakers_all(self, client):
        c, _, _ = client
        r = await c.post("/system/circuit-breakers/reset?target=all")
        assert r.status_code == 200
        assert "prestashop" in r.json()["reseteados"]
        assert "mysql" in r.json()["reseteados"]

    async def test_reset_circuit_breaker_prestashop(self, client):
        c, _, _ = client
        r = await c.post("/system/circuit-breakers/reset?target=prestashop")
        assert r.status_code == 200
        assert r.json()["reseteados"] == ["prestashop"]

    async def test_reset_circuit_breaker_target_invalido(self, client):
        c, _, _ = client
        r = await c.post("/system/circuit-breakers/reset?target=noexiste")
        assert r.status_code == 400


# ──────────────────────────────────────────────────────────────
# TAG: Divisas, idiomas, países, zonas
# ──────────────────────────────────────────────────────────────

@pytest.mark.api
class TestConfiguracionEndpoints:
    async def test_get_currencies(self, client):
        c, ps, _ = client
        ps.get_currencies.return_value = [{"id": "1", "nombre": "Euro", "iso": "EUR", "activa": "1"}]
        r = await c.get("/currencies")
        assert r.status_code == 200

    async def test_get_languages(self, client):
        c, ps, _ = client
        ps.get_languages.return_value = [{"id": "1", "nombre": "Español", "iso": "es"}]
        r = await c.get("/languages")
        assert r.status_code == 200

    async def test_get_countries(self, client):
        c, ps, _ = client
        ps.get_countries.return_value = [{"id": "6", "nombre": "España", "iso": "ES"}]
        r = await c.get("/countries")
        assert r.status_code == 200

    async def test_get_zones(self, client):
        c, ps, _ = client
        ps.get_zones.return_value = [{"id": "1", "nombre": "Europe", "activa": "1"}]
        r = await c.get("/zones")
        assert r.status_code == 200

    async def test_update_currency_rate(self, client):
        c, ps, _ = client
        ps.update_currency_rate.return_value = True
        r = await c.put("/currencies/1/rate?rate=1.08")
        assert r.status_code == 200


# ──────────────────────────────────────────────────────────────
# TAG: Transportistas
# ──────────────────────────────────────────────────────────────

@pytest.mark.api
class TestTransportistasEndpoints:
    async def test_get_carriers(self, client):
        c, ps, _ = client
        ps.get_carriers.return_value = [{"id": "1", "nombre": "Correos", "activo": "1"}]
        r = await c.get("/carriers")
        assert r.status_code == 200

    async def test_update_carrier(self, client):
        c, ps, _ = client
        ps.update_carrier.return_value = True
        r = await c.put("/carriers/1?activo=false")
        assert r.status_code == 200


# ──────────────────────────────────────────────────────────────
# TAG: Búsqueda — search/customers con email
# ──────────────────────────────────────────────────────────────

@pytest.mark.api
class TestBusquedaExtended:
    async def test_search_customers_por_email(self, client, sample_customer):
        c, ps, _ = client
        ps.search_customers.return_value = [sample_customer]
        r = await c.get("/search/customers?email=john@example.com")
        assert r.status_code == 200
        assert r.json()["total"] == 1

    async def test_search_customers_sin_params_da_400(self, client):
        c, _, _ = client
        r = await c.get("/search/customers")
        assert r.status_code == 400

    async def test_search_suppliers(self, client):
        c, ps, _ = client
        ps.search_suppliers_by_id_or_name.return_value = [{"id": "1", "nombre": "Prov A"}]
        r = await c.get("/search/suppliers?q=prov")
        assert r.status_code == 200
        assert r.json()["total"] == 1

    async def test_search_global_por_tipo_productos(self, client):
        c, ps, _ = client
        ps.search_products.return_value = [
            {"id": "1", "nombre": "Test", "referencia": "T1", "precio": "10", "activo": "1"}
        ]
        r = await c.get("/search?q=test&tipo=productos")
        assert r.status_code == 200
        assert r.json()["resultados"]["productos"][0]["id"] == "1"


# ──────────────────────────────────────────────────────────────
# TAG: ps_client — nuevos métodos
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestPsClientNuevosMetodos:
    def _xml(self, content: str):
        mock = MagicMock()
        mock.status_code = 200
        mock.content = content.encode("utf-8")
        return mock

    def test_create_product_con_todos_los_campos(self, mock_ps_client):
        """create_product debe incluir weight, ean13, minimal_quantity y description_short."""
        import xml.etree.ElementTree as ET
        xml = b"<prestashop><product><id>1</id></product></prestashop>"
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.content = xml
        # update_stock también se llamará
        mock_ps_client.session.request.return_value = mock_resp

        with patch("core.ps_client.prestashop_circuit"):
            mock_ps_client.create_product({
                "name": "Test", "price": 10.0, "reference": "T1",
                "id_category": "2", "id_supplier": "0",
                "weight": 0.5, "ean13": "1234567890123",
                "minimal_quantity": 2,
                "description_short": "Desc corta",
            })

        call_data = mock_ps_client.session.request.call_args_list[0][1].get("data") or \
                    mock_ps_client.session.request.call_args_list[0][0][3]
        root = ET.fromstring(call_data)
        assert root.findtext(".//weight") == "0.5"
        assert root.findtext(".//ean13") == "1234567890123"
        assert root.findtext(".//minimal_quantity") == "2"
        desc_s = root.find(".//description_short/language")
        assert desc_s is not None
        assert desc_s.text == "Desc corta"

    def test_update_product_con_nuevos_campos(self, mock_ps_client):
        """update_product debe actualizar id_category_default, weight, ean13."""
        import xml.etree.ElementTree as ET
        xml = b"<prestashop><product><id>1</id></product></prestashop>"
        mock_ps_client.session.request.return_value = MagicMock(status_code=200, content=xml)

        with patch("core.ps_client.prestashop_circuit"):
            mock_ps_client.update_product("1", {
                "id_category_default": "5",
                "weight": 1.2,
                "ean13": "9876543210987",
                "minimal_quantity": 3,
            })

        call_data = mock_ps_client.session.request.call_args[1].get("data") or \
                    mock_ps_client.session.request.call_args[0][3]
        root = ET.fromstring(call_data)
        assert root.findtext(".//id_category_default") == "5"
        assert root.findtext(".//weight") == "1.2"
        assert root.findtext(".//ean13") == "9876543210987"
        assert root.findtext(".//minimal_quantity") == "3"

    def test_get_existing_image_dimensions_sin_imagenes(self, mock_ps_client):
        """Sin imágenes existentes debe devolver (1200, 1200)."""
        mock_ps_client.session.request.return_value = MagicMock(
            status_code=200,
            content=b"<prestashop><images></images></prestashop>"
        )
        w, h = mock_ps_client.get_existing_image_dimensions("1")
        assert w == 1200
        assert h == 1200

    def test_search_customers_por_email_filtra_local(self, mock_ps_client):
        """search_customers debe filtrar localmente por email."""
        xml = """<prestashop><customers>
            <customer>
                <id>1</id><firstname>John</firstname><lastname>Doe</lastname>
                <email>john@test.com</email><active>1</active>
                <date_add>2026-01-01</date_add>
            </customer>
            <customer>
                <id>2</id><firstname>Jane</firstname><lastname>Smith</lastname>
                <email>jane@other.com</email><active>1</active>
                <date_add>2026-01-01</date_add>
            </customer>
        </customers></prestashop>"""
        mock_ps_client.session.request.return_value = MagicMock(
            status_code=200, content=xml.encode()
        )
        result = mock_ps_client.search_customers("john@test.com")
        assert len(result) == 1
        assert result[0]["email"] == "john@test.com"

    def test_search_customers_parcial(self, mock_ps_client):
        """search_customers debe soportar búsqueda parcial por email."""
        xml = """<prestashop><customers>
            <customer>
                <id>1</id><firstname>A</firstname><lastname>B</lastname>
                <email>usuario@empresa.com</email><active>1</active>
                <date_add>2026-01-01</date_add>
            </customer>
            <customer>
                <id>2</id><firstname>C</firstname><lastname>D</lastname>
                <email>otro@distinto.com</email><active>1</active>
                <date_add>2026-01-01</date_add>
            </customer>
        </customers></prestashop>"""
        mock_ps_client.session.request.return_value = MagicMock(
            status_code=200, content=xml.encode()
        )
        result = mock_ps_client.search_customers("empresa")
        assert len(result) == 1

    def test_get_orders_pagina_correctamente(self, mock_ps_client):
        """get_orders debe paginar en Python cuando PS devuelve todos."""
        xml = """<prestashop><orders>""" + \
              "".join(f"<order><id>{i}</id><reference>R{i}</reference>"
                      f"<id_customer>1</id_customer><total_paid>10</total_paid>"
                      f"<current_state>2</current_state><date_add>2026-01-01</date_add>"
                      f"</order>" for i in range(1, 11)) + \
              """</orders></prestashop>"""
        mock_ps_client.session.request.return_value = MagicMock(
            status_code=200, content=xml.encode()
        )
        result_5  = mock_ps_client.get_orders(limit=5, offset=0)
        result_p2 = mock_ps_client.get_orders(limit=5, offset=5)
        assert len(result_5)  == 5
        assert len(result_p2) == 5
        assert result_5[0]["id"]  != result_p2[0]["id"]
