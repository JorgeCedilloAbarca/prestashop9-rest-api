# ============================================================
#  tests/test_new_features.py — Tests de las nuevas funcionalidades
#
#  Cubre: búsqueda por nombre/ID, descripción de producto,
#  estadísticas de tienda, imágenes múltiples y procesamiento.
# ============================================================

import pytest
from unittest.mock import MagicMock, patch
import xml.etree.ElementTree as ET


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def _xml(content: str) -> MagicMock:
    mock = MagicMock()
    mock.status_code = 200
    mock.content = content.encode("utf-8")
    return mock


# ──────────────────────────────────────────────────────────────
# Tests de búsqueda de productos
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestSearchProducts:
    def test_busqueda_por_id_devuelve_producto(self, mock_ps_client):
        xml = """<prestashop><product>
            <id>13</id>
            <name><language id="1">Brown bear</language></name>
            <reference>demo_19</reference>
            <price>9.000000</price>
            <active>1</active>
        </product></prestashop>"""
        mock_ps_client.session.request.return_value = _xml(xml)
        result = mock_ps_client.search_products("13")
        assert len(result) == 1
        assert result[0]["id"] == "13"
        assert result[0]["nombre"] == "Brown bear"

    def test_busqueda_por_nombre_filtra_localmente(self, mock_ps_client):
        xml = """<prestashop><products>
            <product>
                <id>1</id>
                <name><language id="1">Camiseta Roja</language></name>
                <reference>CAM-R</reference><price>19.99</price><active>1</active>
            </product>
            <product>
                <id>2</id>
                <name><language id="1">Pantalón Azul</language></name>
                <reference>PAN-A</reference><price>39.99</price><active>1</active>
            </product>
        </products></prestashop>"""
        mock_ps_client.session.request.return_value = _xml(xml)
        result = mock_ps_client.search_products("camiseta")
        assert len(result) == 1
        assert result[0]["nombre"] == "Camiseta Roja"

    def test_busqueda_case_insensitive(self, mock_ps_client):
        xml = """<prestashop><products>
            <product>
                <id>1</id>
                <name><language id="1">Brown Bear - Vector Graphics</language></name>
                <reference>demo_19</reference><price>9.0</price><active>1</active>
            </product>
        </products></prestashop>"""
        mock_ps_client.session.request.return_value = _xml(xml)
        result = mock_ps_client.search_products("brown bear")
        assert len(result) == 1

    def test_busqueda_por_referencia(self, mock_ps_client):
        xml = """<prestashop><products>
            <product>
                <id>5</id>
                <name><language id="1">Producto Test</language></name>
                <reference>DEMO-99</reference><price>10.0</price><active>1</active>
            </product>
        </products></prestashop>"""
        mock_ps_client.session.request.return_value = _xml(xml)
        result = mock_ps_client.search_products("DEMO-99")
        assert len(result) == 1
        assert result[0]["referencia"] == "DEMO-99"

    def test_busqueda_sin_resultados_devuelve_lista_vacia(self, mock_ps_client):
        xml = """<prestashop><products>
            <product>
                <id>1</id>
                <name><language id="1">Otro producto</language></name>
                <reference>R1</reference><price>10.0</price><active>1</active>
            </product>
        </products></prestashop>"""
        mock_ps_client.session.request.return_value = _xml(xml)
        result = mock_ps_client.search_products("xyznoexiste")
        assert result == []

    def test_busqueda_id_no_existente_devuelve_lista_vacia(self, mock_ps_client):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.text = "<error>Not found</error>"
        mock_ps_client.session.request.return_value = mock_resp
        result = mock_ps_client.search_products("99999")
        assert result == []


# ──────────────────────────────────────────────────────────────
# Tests de búsqueda de clientes
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestSearchCustomers:
    def test_busqueda_por_id(self, mock_ps_client):
        xml = """<prestashop><customer>
            <id>2</id><firstname>John</firstname><lastname>Doe</lastname>
            <email>john@test.com</email><active>1</active><date_add>2026-01-01</date_add>
        </customer></prestashop>"""
        mock_ps_client.session.request.return_value = _xml(xml)
        result = mock_ps_client.search_customers_by_id_or_name("2")
        assert len(result) == 1
        assert result[0]["nombre"] == "John Doe"

    def test_busqueda_por_nombre_sin_duplicados(self, mock_ps_client):
        xml_nombre = """<prestashop><customers>
            <customer><id>1</id><firstname>John</firstname><lastname>Smith</lastname>
            <email>john@test.com</email><active>1</active><date_add>2026-01-01</date_add>
            </customer>
        </customers></prestashop>"""
        xml_apellido = """<prestashop><customers>
            <customer><id>1</id><firstname>John</firstname><lastname>Smith</lastname>
            <email>john@test.com</email><active>1</active><date_add>2026-01-01</date_add>
            </customer>
        </customers></prestashop>"""
        mock_ps_client.session.request.side_effect = [
            _xml(xml_nombre), _xml(xml_apellido)
        ]
        result = mock_ps_client.search_customers_by_id_or_name("john")
        # No debe haber duplicados aunque aparezca en ambas búsquedas
        assert len(result) == 1


# ──────────────────────────────────────────────────────────────
# Tests de búsqueda de categorías
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestSearchCategories:
    def test_busqueda_por_id(self, mock_ps_client):
        xml = """<prestashop><category>
            <id>3</id><name><language id="1">Ropa</language></name>
            <id_parent>2</id_parent><active>1</active>
        </category></prestashop>"""
        mock_ps_client.session.request.return_value = _xml(xml)
        result = mock_ps_client.search_categories_by_id_or_name("3")
        assert len(result) == 1
        assert result[0]["nombre"] == "Ropa"

    def test_busqueda_por_nombre_filtra_localmente(self, mock_ps_client):
        xml = """<prestashop><categories>
            <category>
                <id>3</id><name><language id="1">Ropa</language></name>
                <id_parent>2</id_parent><active>1</active>
            </category>
            <category>
                <id>6</id><name><language id="1">Accesorios</language></name>
                <id_parent>2</id_parent><active>1</active>
            </category>
        </categories></prestashop>"""
        mock_ps_client.session.request.return_value = _xml(xml)
        result = mock_ps_client.search_categories_by_id_or_name("ropa")
        assert len(result) == 1
        assert result[0]["id"] == "3"


# ──────────────────────────────────────────────────────────────
# Tests de descripción de producto
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestProductDescription:
    def test_update_description_exitoso(self, mock_ps_client):
        xml = """<prestashop><product><id>1</id></product></prestashop>"""
        mock_ps_client.session.request.return_value = _xml(xml)
        result = mock_ps_client.update_description("1", "<p>Descripción de prueba</p>")
        assert result is True

    def test_update_description_fallo_devuelve_false(self, mock_ps_client):
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "<error>bad request</error>"
        mock_ps_client.session.request.return_value = mock_resp
        result = mock_ps_client.update_description("999", "desc")
        assert result is False

    def test_get_product_description_devuelve_texto(self, mock_ps_client):
        xml = """<prestashop><product>
            <id>1</id>
            <description><language id="1">Mi descripción HTML</language></description>
        </product></prestashop>"""
        mock_ps_client.session.request.return_value = _xml(xml)
        result = mock_ps_client.get_product_description("1")
        assert result == "Mi descripción HTML"

    def test_get_product_description_sin_descripcion_devuelve_cadena_vacia(self, mock_ps_client):
        xml = """<prestashop><product><id>1</id></product></prestashop>"""
        mock_ps_client.session.request.return_value = _xml(xml)
        result = mock_ps_client.get_product_description("1")
        assert result == ""

    def test_create_product_con_description_incluye_campo(self, mock_ps_client):
        xml = """<prestashop><product><id>42</id></product></prestashop>"""
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.content = xml.encode("utf-8")
        mock_ps_client.session.request.return_value = mock_resp

        mock_ps_client.create_product({
            "name": "Test", "price": 10, "reference": "T1",
            "id_category": "2", "id_supplier": "0",
            "description": "<p>Mi descripción</p>",
        })

        call_data = mock_ps_client.session.request.call_args[1].get("data") or \
                    mock_ps_client.session.request.call_args[0][3]
        root = ET.fromstring(call_data)
        desc = root.find(".//description/language")
        assert desc is not None
        assert desc.text == "<p>Mi descripción</p>"


# ──────────────────────────────────────────────────────────────
# Tests de estadísticas
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestStats:
    def test_get_orders_stats_calcula_correctamente(self, mock_ps_client):
        xml_orders = """<prestashop><orders>
            <order><id>1</id><total_paid>50.00</total_paid><current_state>2</current_state><date_add>2026-01-01</date_add></order>
            <order><id>2</id><total_paid>30.00</total_paid><current_state>4</current_state><date_add>2026-01-02</date_add></order>
            <order><id>3</id><total_paid>20.00</total_paid><current_state>2</current_state><date_add>2026-01-03</date_add></order>
        </orders></prestashop>"""
        xml_states = """<prestashop><order_states>
            <order_state><id>2</id><name><language id="1">Pago aceptado</language></name></order_state>
            <order_state><id>4</id><name><language id="1">Enviado</language></name></order_state>
        </order_states></prestashop>"""
        # Primera llamada: pedidos, segunda: estados
        mock_ps_client.session.request.side_effect = [_xml(xml_orders), _xml(xml_states)]
        result = mock_ps_client.get_orders_stats()
        assert result["total_pedidos"] == 3
        assert result["importe_total"] == 100.0
        # Ahora por_estado usa nombres de estado en lugar de IDs
        assert result["por_estado"]["Pago aceptado"] == 2
        assert result["por_estado"]["Enviado"] == 1

    def test_get_products_stats_cuenta_activos(self, mock_ps_client):
        xml_products = """<prestashop><products>
            <product><id>1</id><active>1</active><price>10.0</price></product>
            <product><id>2</id><active>1</active><price>20.0</price></product>
            <product><id>3</id><active>0</active><price>5.0</price></product>
        </products></prestashop>"""
        xml_stock = """<prestashop><stock_availables>
            <stock_available><id>1</id></stock_available>
        </stock_availables></prestashop>"""
        mock_ps_client.session.request.side_effect = [_xml(xml_products), _xml(xml_stock)]
        result = mock_ps_client.get_products_stats()
        assert result["total_productos"] == 3
        assert result["activos"] == 2
        assert result["inactivos"] == 1
        assert result["sin_stock"] == 1
        assert result["precio_medio"] == pytest.approx(11.67, rel=0.01)

    def test_get_categories_stats(self, mock_ps_client):
        xml = """<prestashop><categories>
            <category><id>1</id><active>1</active></category>
            <category><id>2</id><active>1</active></category>
            <category><id>3</id><active>0</active></category>
        </categories></prestashop>"""
        mock_ps_client.session.request.return_value = _xml(xml)
        result = mock_ps_client.get_categories_stats()
        assert result["total_categorias"] == 3
        assert result["activas"] == 2
        assert result["inactivas"] == 1

    def test_stats_api_falla_devuelve_dict_vacio(self, mock_ps_client):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "error"
        mock_ps_client.session.request.return_value = mock_resp
        assert mock_ps_client.get_orders_stats() == {}
        assert mock_ps_client.get_categories_stats() == {}


# ──────────────────────────────────────────────────────────────
# Tests de imágenes múltiples
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestMultipleImages:
    def test_upload_image_bytes_exitoso(self, mock_ps_client):
        """Verifica que upload_image_bytes sube la imagen procesada correctamente."""
        from PIL import Image
        import io
        # Crear una imagen JPEG real pequeña en memoria para el test
        img = Image.new("RGB", (10, 10), color=(255, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        real_jpeg = buf.getvalue()

        xml = "<prestashop><image><id>5</id></image></prestashop>"
        mock_ps_client.session.request.return_value = MagicMock(
            status_code=201, content=xml.encode()
        )
        result = mock_ps_client.upload_image_bytes(
            "1", real_jpeg, "foto.jpg", max_width=800, max_height=800
        )
        assert result is True

    def test_upload_image_bytes_fallo_proceso_devuelve_false(self, mock_ps_client):
        """Verifica que devuelve False si los bytes no son una imagen válida."""
        result = mock_ps_client.upload_image_bytes(
            "1", b"esto_no_es_una_imagen", "foto.jpg", max_width=800, max_height=800
        )
        assert result is False

    def test_get_product_images_devuelve_lista(self, mock_ps_client):
        xml = """<prestashop><images>
            <image id="1"><id>1</id></image>
            <image id="2"><id>2</id></image>
        </images></prestashop>"""
        mock_ps_client.session.request.return_value = _xml(xml)
        result = mock_ps_client.get_product_images("10")
        assert len(result) == 2
        assert result[0]["id"] == "1"

    def test_delete_product_image_exitoso(self, mock_ps_client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_ps_client.session.request.return_value = mock_resp
        assert mock_ps_client.delete_product_image("1", "3") is True

    def test_delete_product_image_fallo(self, mock_ps_client):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.text = "not found"
        mock_ps_client.session.request.return_value = mock_resp
        assert mock_ps_client.delete_product_image("1", "999") is False


# ──────────────────────────────────────────────────────────────
# Tests de image_processor
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestImageProcessor:
    def test_process_image_desde_bytes_devuelve_jpeg(self):
        """Procesar una imagen JPEG válida devuelve bytes JPEG."""
        from PIL import Image
        import io
        from core.image_processor import process_image

        # Crear imagen de prueba válida en memoria
        img = Image.new("RGB", (200, 200), color=(100, 150, 200))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        jpeg_bytes = buf.getvalue()

        result = process_image(jpeg_bytes)
        assert result is not None
        assert isinstance(result, bytes)
        assert len(result) > 0
        # Verificar que es un JPEG válido
        img2 = Image.open(io.BytesIO(result))
        assert img2.format == "JPEG"

    def test_process_image_redimensiona_correctamente(self):
        """Una imagen grande debe redimensionarse respetando el aspect ratio."""
        from PIL import Image
        import io
        from core.image_processor import process_image

        img = Image.new("RGB", (3000, 2000), color=(255, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")

        result = process_image(buf.getvalue(), max_width=1200, max_height=1200)
        img2 = Image.open(io.BytesIO(result))
        assert img2.width <= 1200
        assert img2.height <= 1200

    def test_process_image_convierte_png_a_jpeg(self):
        """Una imagen PNG con transparencia debe convertirse a JPEG RGB."""
        from PIL import Image
        import io
        from core.image_processor import process_image

        img = Image.new("RGBA", (100, 100), color=(0, 255, 0, 128))
        buf = io.BytesIO()
        img.save(buf, format="PNG")

        result = process_image(buf.getvalue())
        img2 = Image.open(io.BytesIO(result))
        assert img2.format == "JPEG"
        assert img2.mode == "RGB"

    def test_validate_image_imagen_valida(self):
        """validate_image debe devolver (True, "") para una imagen válida."""
        from PIL import Image
        import io
        from core.image_processor import validate_image

        img = Image.new("RGB", (50, 50), color=(0, 0, 255))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")

        ok, msg = validate_image(buf.getvalue())
        assert ok is True
        assert msg == ""

    def test_validate_image_bytes_invalidos(self):
        """validate_image debe devolver (False, msg) para bytes no válidos."""
        from core.image_processor import validate_image
        ok, msg = validate_image(b"esto no es una imagen")
        assert ok is False
        assert msg != ""

    def test_validate_image_supera_tamanio_maximo(self):
        """validate_image debe rechazar archivos que superen el límite."""
        from core.image_processor import validate_image
        datos_grandes = b"x" * (9 * 1024 * 1024)  # 9 MB
        ok, msg = validate_image(datos_grandes, max_mb=8.0)
        assert ok is False
        assert "MB" in msg


# ──────────────────────────────────────────────────────────────
# Tests de endpoints de búsqueda (API)
# ──────────────────────────────────────────────────────────────

@pytest.mark.api
class TestSearchEndpoints:
    async def test_search_global_devuelve_estructura_correcta(self, client):
        c, ps, _ = client
        ps.search_products.return_value = []
        ps.search_customers_by_id_or_name.return_value = []
        ps.search_categories_by_id_or_name.return_value = []
        ps.search_suppliers_by_id_or_name.return_value = []
        ps.search_orders_by_id_or_ref.return_value = []
        r = await c.get("/search?q=test")
        assert r.status_code == 200
        data = r.json()
        assert "resultados" in data
        assert "total" in data
        assert "productos" in data["resultados"]

    async def test_search_global_tipo_invalido_da_400(self, client):
        c, _, _ = client
        r = await c.get("/search?q=test&tipo=noexiste")
        assert r.status_code == 400

    async def test_search_products_por_nombre(self, client):
        c, ps, _ = client
        ps.search_products.return_value = [
            {"id": "1", "nombre": "Camiseta", "referencia": "CAM", "precio": "19.99", "activo": "1"}
        ]
        r = await c.get("/search/products?q=camiseta")
        assert r.status_code == 200
        assert r.json()["total"] == 1

    async def test_search_customers(self, client):
        c, ps, _ = client
        ps.search_customers_by_id_or_name.return_value = [
            {"id": "2", "nombre": "John Doe", "email": "john@test.com", "activo": "1", "registro": "2026-01-01"}
        ]
        r = await c.get("/search/customers?q=john")
        assert r.status_code == 200
        assert r.json()["total"] == 1

    async def test_search_categories(self, client):
        c, ps, _ = client
        ps.search_categories_by_id_or_name.return_value = [
            {"id": "3", "nombre": "Ropa", "id_parent": "2", "activo": "1"}
        ]
        r = await c.get("/search/categories?q=ropa")
        assert r.status_code == 200

    async def test_search_orders(self, client):
        c, ps, _ = client
        ps.search_orders_by_id_or_ref.return_value = [
            {"id": "1", "referencia": "TESTREF", "id_cliente": "2",
             "total_pagado": "50.0", "id_estado": "2", "fecha": "2026-01-01"}
        ]
        r = await c.get("/search/orders?q=TESTREF")
        assert r.status_code == 200
        assert r.json()["total"] == 1


# ──────────────────────────────────────────────────────────────
# Tests de nuevas correcciones
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestImpuestos:
    def _xml(self, content: str):
        mock = MagicMock()
        mock.status_code = 200
        mock.content = content.encode("utf-8")
        return mock

    def test_assign_tax_construye_xml_con_campos_minimos(self, mock_ps_client):
        """assign_tax_to_product debe incluir name, price, reference e id_tax_rules_group."""
        import xml.etree.ElementTree as ET
        xml_get = """<prestashop><product>
            <id>1</id><n><language id="1">Camiseta</language></n>
            <reference>CAM-001</reference><price>19.99</price>
            <id_category_default>3</id_category_default>
            <id_supplier>1</id_supplier><active>1</active>
        </product></prestashop>"""
        xml_put = b"<prestashop><product><id>1</id></product></prestashop>"
        mock_ps_client.session.request.side_effect = [
            self._xml(xml_get),
            MagicMock(status_code=200, content=xml_put),
        ]
        result = mock_ps_client.assign_tax_to_product("1", "2")
        assert result is True
        # Verificar que el XML enviado incluye id_tax_rules_group
        put_call = mock_ps_client.session.request.call_args
        data = put_call[1].get("data") or put_call[0][3]
        root = ET.fromstring(data)
        assert root.findtext(".//id_tax_rules_group") == "2"
        assert root.findtext(".//price") == "19.99"

    def test_assign_tax_falla_si_producto_no_existe(self, mock_ps_client):
        """assign_tax_to_product devuelve False si el producto no existe."""
        mock_ps_client.session.request.return_value = MagicMock(status_code=404, text="not found")
        result = mock_ps_client.assign_tax_to_product("9999", "1")
        assert result is False


@pytest.mark.unit
class TestOrderStates:
    def _xml(self, content: str):
        mock = MagicMock()
        mock.status_code = 200
        mock.content = content.encode("utf-8")
        return mock

    def test_get_order_states_devuelve_nombres(self, mock_ps_client):
        """get_order_states debe devolver nombre legible del estado."""
        xml = """<prestashop><order_states>
            <order_state>
                <id>2</id>
                <name><language id="1">Pago aceptado</language></name>
            </order_state>
            <order_state>
                <id>4</id>
                <name><language id="1">Enviado</language></name>
            </order_state>
        </order_states></prestashop>"""
        mock_ps_client.session.request.return_value = self._xml(xml)
        estados = mock_ps_client.get_order_states()
        assert len(estados) == 2
        assert estados[0]["nombre"] == "Pago aceptado"
        assert estados[1]["id"] == "4"

    def test_orders_stats_usa_nombres_de_estado(self, mock_ps_client):
        """get_orders_stats debe usar nombres de estado en por_estado."""
        xml_orders = """<prestashop><orders>
            <order><id>1</id><total_paid>50.00</total_paid>
                <current_state>2</current_state><date_add>2026-01-01</date_add></order>
        </orders></prestashop>"""
        xml_states = """<prestashop><order_states>
            <order_state>
                <id>2</id>
                <name><language id="1">Pago aceptado</language></name>
            </order_state>
        </order_states></prestashop>"""
        mock_ps_client.session.request.side_effect = [self._xml(xml_orders), self._xml(xml_states)]
        result = mock_ps_client.get_orders_stats()
        assert "Pago aceptado" in result["por_estado"]


@pytest.mark.unit
class TestCustomerOrders:
    def _xml(self, content: str):
        mock = MagicMock()
        mock.status_code = 200
        mock.content = content.encode("utf-8")
        return mock

    def test_get_customer_orders_filtra_por_cliente(self, mock_ps_client):
        """get_customer_orders filtra localmente por id_customer."""
        xml_orders = """<prestashop><orders>
            <order><id>1</id><reference>R1</reference><id_customer>2</id_customer>
                <total_paid>50.00</total_paid><current_state>2</current_state>
                <date_add>2026-01-01</date_add></order>
            <order><id>2</id><reference>R2</reference><id_customer>5</id_customer>
                <total_paid>30.00</total_paid><current_state>4</current_state>
                <date_add>2026-01-02</date_add></order>
        </orders></prestashop>"""
        xml_states = """<prestashop><order_states>
            <order_state><id>2</id><name><language id="1">Pago aceptado</language></name></order_state>
        </order_states></prestashop>"""
        mock_ps_client.session.request.side_effect = [self._xml(xml_orders), self._xml(xml_states)]
        pedidos = mock_ps_client.get_customer_orders("2")
        # Solo debe devolver los pedidos del cliente 2
        assert len(pedidos) == 1
        assert pedidos[0]["id"] == "1"
        assert pedidos[0]["estado"] == "Pago aceptado"

    def test_get_customer_orders_sin_pedidos_devuelve_vacio(self, mock_ps_client):
        xml_orders = """<prestashop><orders>
            <order><id>1</id><reference>R1</reference><id_customer>99</id_customer>
                <total_paid>50.00</total_paid><current_state>2</current_state>
                <date_add>2026-01-01</date_add></order>
        </orders></prestashop>"""
        xml_states = """<prestashop><order_states>
            <order_state><id>2</id><name><language id="1">Pago aceptado</language></name></order_state>
        </order_states></prestashop>"""
        mock_ps_client.session.request.side_effect = [self._xml(xml_orders), self._xml(xml_states)]
        pedidos = mock_ps_client.get_customer_orders("2")
        assert pedidos == []


@pytest.mark.unit
class TestCreateSupplierSinId:
    def _xml(self, content: str):
        mock = MagicMock()
        mock.status_code = 201
        mock.content = content.encode("utf-8")
        return mock

    def test_create_supplier_sin_id_manual(self, mock_ps_client):
        """create_supplier no debe incluir <id> en el XML — PS9 lo asigna."""
        import xml.etree.ElementTree as ET
        xml = "<prestashop><supplier><id>5</id></supplier></prestashop>"
        mock_ps_client.session.request.return_value = self._xml(xml)
        result = mock_ps_client.create_supplier("Nuevo Proveedor")
        assert result == "5"
        # Verificar que el XML enviado NO incluye <id>
        call_data = mock_ps_client.session.request.call_args[1].get("data") or                     mock_ps_client.session.request.call_args[0][3]
        root = ET.fromstring(call_data)
        id_el = root.find(".//supplier/id")
        assert id_el is None, "El XML no debe incluir <id> al crear un proveedor"
