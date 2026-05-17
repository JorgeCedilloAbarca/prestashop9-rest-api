# ============================================================
#  tests/test_api_endpoints.py — Tests de endpoints FastAPI
#
#  Prueba cada endpoint usando un cliente HTTP en memoria.
#  Los componentes ps_client y db_handler están mockeados,
#  por lo que no se hacen llamadas reales a MySQL ni a PS.
# ============================================================

import pytest
from unittest.mock import patch, MagicMock
from httpx import AsyncClient, ASGITransport


# ──────────────────────────────────────────────────────────────
# Fixture del cliente API con todos los componentes mockeados
# ──────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────
# Tests de endpoints generales
# ──────────────────────────────────────────────────────────────

@pytest.mark.api
class TestGeneralEndpoints:
    async def test_root_devuelve_200(self, client):
        c, _, _ = client
        r = await c.get("/")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    async def test_health_ambos_ok(self, client):
        c, ps, db = client
        db.health_check.return_value = True
        ps._request.return_value = MagicMock()  # PS responde
        r = await c.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert "status" in data
        assert "database" in data
        assert "prestashop" in data

    async def test_health_db_caida(self, client):
        c, ps, db = client
        db.health_check.return_value = False
        ps._request.return_value = MagicMock()
        r = await c.get("/health")
        assert r.status_code == 200
        assert r.json()["database"] == "error"
        assert r.json()["status"] == "degraded"

    async def test_docs_devuelve_html(self, client):
        c, _, _ = client
        r = await c.get("/docs")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]


# ──────────────────────────────────────────────────────────────
# Tests de endpoints de productos
# ──────────────────────────────────────────────────────────────

@pytest.mark.api
class TestProductosEndpoints:
    async def test_list_products_vacio(self, client):
        c, _, db = client
        db.obtener_datos_completos.return_value = []
        r = await c.get("/products")
        assert r.status_code == 200
        assert r.json()["total"] == 0
        assert r.json()["productos"] == []

    async def test_list_products_con_datos(self, client, sample_product):
        c, _, db = client
        sample_product["precio"] = 19.99
        db.obtener_datos_completos.return_value = [sample_product]
        r = await c.get("/products")
        assert r.status_code == 200
        assert r.json()["total"] == 1
        assert r.json()["productos"][0]["nombre"] == "Camiseta Test"

    async def test_list_products_limit(self, client):
        c, _, db = client
        db.obtener_datos_completos.return_value = [
            {"id": i, "nombre": f"P{i}", "precio": 10.0,
             "referencia": f"R{i}", "prestashop_id": None,
             "nombre_categoria": "Cat", "nombre_proveedor": "Prov", "stock": 1}
            for i in range(10)
        ]
        r = await c.get("/products?limit=3")
        assert r.status_code == 200
        assert r.json()["devueltos"] == 3
        assert r.json()["total"] == 10

    async def test_create_product_exitoso(self, client):
        c, ps, _ = client
        ps.create_product.return_value = "99"
        r = await c.post("/products?name=Test&price=15.99&reference=TEST-001")
        assert r.status_code == 200
        assert r.json()["status"] == "created"
        assert r.json()["prestashop_id"] == "99"

    async def test_create_product_falla_ps(self, client):
        c, ps, _ = client
        ps.create_product.return_value = None
        r = await c.post("/products?name=Test&price=15.99&reference=TEST-001")
        assert r.status_code == 502

    async def test_update_product_sin_campos_da_400(self, client):
        c, _, _ = client
        r = await c.put("/products/1")
        assert r.status_code == 400

    async def test_update_product_exitoso(self, client):
        c, ps, _ = client
        ps.update_product.return_value = True
        r = await c.put("/products/1?name=NuevoNombre")
        assert r.status_code == 200
        assert r.json()["status"] == "updated"

    async def test_delete_product_exitoso(self, client):
        c, ps, _ = client
        ps.delete_product.return_value = True
        r = await c.delete("/products/1")
        assert r.status_code == 200
        assert r.json()["status"] == "deleted"

    async def test_delete_product_falla(self, client):
        c, ps, _ = client
        ps.delete_product.return_value = False
        r = await c.delete("/products/999")
        assert r.status_code == 502

    async def test_search_by_reference_encontrado(self, client):
        c, ps, db = client
        ps.get_product_by_reference.return_value = {"id": "1", "reference": "CAM-001", "active": "1"}
        db.obtener_producto_por_referencia.return_value = None
        r = await c.get("/products/reference/CAM-001")
        assert r.status_code == 200
        assert r.json()["prestashop"]["reference"] == "CAM-001"

    async def test_search_by_reference_no_encontrado(self, client):
        c, ps, db = client
        ps.get_product_by_reference.return_value = None
        db.obtener_producto_por_referencia.return_value = None
        r = await c.get("/products/reference/NOEXISTE")
        assert r.status_code == 404


# ──────────────────────────────────────────────────────────────
# Tests de endpoints de stock
# ──────────────────────────────────────────────────────────────

@pytest.mark.api
class TestStockEndpoints:
    async def test_get_stock_encontrado(self, client):
        c, ps, _ = client
        ps.get_stock.return_value = {"id_stock": "1", "id_product": "1", "quantity": 22}
        r = await c.get("/stock/1")
        assert r.status_code == 200
        assert r.json()["quantity"] == 22

    async def test_get_stock_no_encontrado(self, client):
        c, ps, _ = client
        ps.get_stock.return_value = None
        r = await c.get("/stock/999")
        assert r.status_code == 404

    async def test_update_stock_exitoso(self, client):
        c, ps, _ = client
        ps.update_stock.return_value = True
        r = await c.put("/stock/1/50")
        assert r.status_code == 200
        assert r.json()["quantity"] == 50

    async def test_update_stock_cantidad_negativa_no_permitida(self, client):
        c, _, _ = client
        r = await c.put("/stock/1/-1")
        assert r.status_code == 422  # Validación FastAPI


# ──────────────────────────────────────────────────────────────
# Tests de endpoints de categorías
# ──────────────────────────────────────────────────────────────

@pytest.mark.api
class TestCategoriesEndpoints:
    async def test_get_categories(self, client):
        c, ps, _ = client
        ps.get_categories.return_value = [
            {"id": "3", "nombre": "Ropa", "id_parent": "2", "active": "1"},
        ]
        r = await c.get("/categories")
        assert r.status_code == 200
        assert r.json()["total"] == 1

    async def test_create_category_exitoso(self, client):
        c, ps, _ = client
        ps.create_category.return_value = "10"
        r = await c.post("/categories?name=Nueva Categoria&parent_id=2")
        assert r.status_code == 200
        assert r.json()["status"] == "created"
        assert r.json()["category_id"] == "10"

    async def test_delete_category_raiz_protegida(self, client):
        c, _, _ = client
        r = await c.delete("/categories/1")
        assert r.status_code == 400

    async def test_delete_category_raiz_2_protegida(self, client):
        c, _, _ = client
        r = await c.delete("/categories/2")
        assert r.status_code == 400

    async def test_delete_category_normal(self, client):
        c, ps, _ = client
        ps.delete_category.return_value = True
        r = await c.delete("/categories/10")
        assert r.status_code == 200


# ──────────────────────────────────────────────────────────────
# Tests de endpoints de catálogo / sincronización
# ──────────────────────────────────────────────────────────────

@pytest.mark.api
class TestCatalogEndpoints:
    async def test_sync_async_acepta_inmediatamente(self, client):
        c, _, _ = client
        r = await c.post("/catalog/sync")
        assert r.status_code == 200
        assert r.json()["status"] == "accepted"

    async def test_sync_single_product_no_encontrado(self, client):
        c, _, _ = client
        with patch("app.catalog_service") as mock_cat:
            mock_cat.sincronizar_producto_unico.return_value = MagicMock(
                total=0, exitoso=True,
                to_dict=lambda: {}
            )
            r = await c.post("/catalog/sync/product/9999")
            assert r.status_code == 404


# ──────────────────────────────────────────────────────────────
# Tests de endpoints de clientes
# ──────────────────────────────────────────────────────────────

@pytest.mark.api
class TestClientesEndpoints:
    async def test_get_customers(self, client, sample_customer):
        c, ps, _ = client
        ps.get_customers.return_value = [sample_customer]
        r = await c.get("/customers")
        assert r.status_code == 200
        assert r.json()["total"] == 1
        assert r.json()["clientes"][0]["email"] == "john@example.com"

    async def test_get_customer_by_id_encontrado(self, client, sample_customer):
        c, ps, _ = client
        ps.get_customer.return_value = {**sample_customer, "telefono": "", "id_grupo": "3", "newsletter": "0"}
        r = await c.get("/customers/2")
        assert r.status_code == 200

    async def test_get_customer_by_id_no_encontrado(self, client):
        c, ps, _ = client
        ps.get_customer.return_value = None
        r = await c.get("/customers/9999")
        assert r.status_code == 404

    async def test_search_customers_encontrado(self, client, sample_customer):
        c, ps, _ = client
        ps.search_customers.return_value = [sample_customer]
        r = await c.get("/customers/search?email=john")
        assert r.status_code == 200
        assert r.json()["total"] == 1

    async def test_search_customers_no_encontrado(self, client):
        c, ps, _ = client
        ps.search_customers.return_value = []
        r = await c.get("/customers/search?email=noexiste@test.com")
        assert r.status_code == 404


# ──────────────────────────────────────────────────────────────
# Tests de endpoints de pedidos
# ──────────────────────────────────────────────────────────────

@pytest.mark.api
class TestPedidosEndpoints:
    async def test_get_orders(self, client, sample_order):
        c, ps, _ = client
        ps.get_orders.return_value = [sample_order]
        r = await c.get("/orders")
        assert r.status_code == 200
        assert r.json()["total"] == 1

    async def test_get_order_detail_encontrado(self, client, sample_order):
        c, ps, _ = client
        ps.get_order.return_value = sample_order
        r = await c.get("/orders/1")
        assert r.status_code == 200
        assert r.json()["referencia"] == "TESTREF1"

    async def test_get_order_detail_no_encontrado(self, client):
        c, ps, _ = client
        ps.get_order.return_value = None
        r = await c.get("/orders/9999")
        assert r.status_code == 404

    async def test_update_order_state_exitoso(self, client):
        c, ps, _ = client
        ps.update_order_state.return_value = True
        r = await c.put("/orders/1/state/4")
        assert r.status_code == 200
        assert r.json()["new_state_id"] == 4

    async def test_get_order_states(self, client):
        c, ps, _ = client
        ps.get_order_states.return_value = [
            {"id": "2", "nombre": "Pago aceptado"},
            {"id": "4", "nombre": "Enviado"},
        ]
        r = await c.get("/orders/states")
        assert r.status_code == 200
        assert r.json()["total"] == 2


# ──────────────────────────────────────────────────────────────
# Tests de endpoints de impuestos
# ──────────────────────────────────────────────────────────────

@pytest.mark.api
class TestImpuestosEndpoints:
    async def test_get_taxes(self, client):
        c, ps, _ = client
        ps.get_taxes.return_value = [
            {"id": "1", "nombre": "IVA 21%", "porcentaje": "21.000", "activo": "1"},
        ]
        r = await c.get("/taxes")
        assert r.status_code == 200
        assert r.json()["total"] == 1

    async def test_get_tax_rules(self, client):
        c, ps, _ = client
        ps.get_tax_rules.return_value = [
            {"id": "1", "nombre": "IVA ES 21%", "activo": "1"},
        ]
        r = await c.get("/taxes/rules")
        assert r.status_code == 200

    async def test_assign_tax_exitoso(self, client):
        c, ps, _ = client
        ps.assign_tax_to_product.return_value = True
        r = await c.put("/taxes/assign?product_id=1&tax_rule_group_id=1")
        assert r.status_code == 200
        assert r.json()["status"] == "updated"

    async def test_assign_tax_falla(self, client):
        c, ps, _ = client
        ps.assign_tax_to_product.return_value = False
        r = await c.put("/taxes/assign?product_id=999&tax_rule_group_id=1")
        assert r.status_code == 502


# ──────────────────────────────────────────────────────────────
# Tests de endpoints de descuentos
# ──────────────────────────────────────────────────────────────

@pytest.mark.api
class TestDescuentosEndpoints:
    async def test_get_discounts(self, client):
        c, ps, _ = client
        ps.get_cart_rules.return_value = [
            {"id": "1", "nombre": "Verano", "codigo": "VERANO20",
             "descuento_%": "20", "descuento_importe": "0",
             "activo": "1", "desde": "2026-01-01", "hasta": "2026-12-31"},
        ]
        r = await c.get("/discounts")
        assert r.status_code == 200
        assert r.json()["total"] == 1

    async def test_create_discount_sin_reduccion_da_400(self, client):
        c, _, _ = client
        r = await c.post("/discounts?name=Test&code=TEST&reduction_percent=0&reduction_amount=0")
        assert r.status_code == 400

    async def test_create_discount_exitoso(self, client):
        c, ps, _ = client
        ps.create_cart_rule.return_value = "5"
        r = await c.post("/discounts?name=Verano&code=VER20&reduction_percent=20")
        assert r.status_code == 200
        assert r.json()["status"] == "created"

    async def test_delete_discount(self, client):
        c, ps, _ = client
        ps.delete_cart_rule.return_value = True
        r = await c.delete("/discounts/1")
        assert r.status_code == 200
