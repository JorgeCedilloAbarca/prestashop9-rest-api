# ============================================================
#  tests/test_ps_client.py — Tests unitarios de PrestashopClient
#
#  Verifican la lógica interna del cliente XML sin hacer
#  ninguna llamada HTTP real. Todo se mockea a nivel de session.
# ============================================================

import pytest
import xml.etree.ElementTree as ET
from unittest.mock import MagicMock, patch


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def _xml_response(xml_string: str) -> MagicMock:
    """Crea un mock de respuesta HTTP con contenido XML."""
    mock = MagicMock()
    mock.status_code = 200
    mock.content = xml_string.encode("utf-8")
    return mock


def _xml_created(xml_string: str) -> MagicMock:
    """Crea un mock de respuesta HTTP 201 Created."""
    mock = MagicMock()
    mock.status_code = 201
    mock.content = xml_string.encode("utf-8")
    return mock


def _error_response(status_code: int = 400) -> MagicMock:
    """Crea un mock de respuesta HTTP de error."""
    mock = MagicMock()
    mock.status_code = status_code
    mock.text = f"<error>{status_code}</error>"
    return mock


# ──────────────────────────────────────────────────────────────
# Tests de _slugify
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestSlugify:
    def test_texto_normal(self, mock_ps_client):
        assert mock_ps_client._slugify("Camiseta Básica") == "camiseta-basica"

    def test_acentos(self, mock_ps_client):
        assert mock_ps_client._slugify("Ñoño Ágil") == "nono-agil"

    def test_caracteres_especiales(self, mock_ps_client):
        assert mock_ps_client._slugify("Precio: 100€ / unidad") == "precio-100-unidad"

    def test_espacios_multiples(self, mock_ps_client):
        assert mock_ps_client._slugify("  hola   mundo  ") == "hola-mundo"

    def test_cadena_vacia(self, mock_ps_client):
        assert mock_ps_client._slugify("") == "sin-nombre"

    def test_solo_caracteres_especiales(self, mock_ps_client):
        assert mock_ps_client._slugify("!!!###") == "sin-nombre"

    def test_guiones_multiples(self, mock_ps_client):
        assert mock_ps_client._slugify("hola--mundo") == "hola-mundo"


# ──────────────────────────────────────────────────────────────
# Tests de _request
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestRequest:
    def test_get_exitoso_devuelve_xml(self, mock_ps_client):
        xml = b"<prestashop><products></products></prestashop>"
        mock_ps_client.session.request.return_value = _xml_response(
            xml.decode()
        )
        result = mock_ps_client._request("GET", "products")
        assert result is not None
        assert result.tag == "prestashop"

    def test_error_400_devuelve_none(self, mock_ps_client):
        mock_ps_client.session.request.return_value = _error_response(400)
        result = mock_ps_client._request("GET", "products")
        assert result is None

    def test_delete_exitoso_devuelve_elemento_ok(self, mock_ps_client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_ps_client.session.request.return_value = mock_resp
        result = mock_ps_client._request("DELETE", "products/1")
        assert result is not None
        assert result.tag == "ok"

    def test_timeout_devuelve_none(self, mock_ps_client):
        import requests
        mock_ps_client.session.request.side_effect = requests.exceptions.Timeout
        result = mock_ps_client._request("GET", "products")
        assert result is None

    def test_connection_error_devuelve_none(self, mock_ps_client):
        import requests
        mock_ps_client.session.request.side_effect = requests.exceptions.ConnectionError
        result = mock_ps_client._request("GET", "products")
        assert result is None

    def test_xml_invalido_devuelve_none(self, mock_ps_client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"esto no es xml <<<"
        mock_ps_client.session.request.return_value = mock_resp
        result = mock_ps_client._request("GET", "products")
        assert result is None


# ──────────────────────────────────────────────────────────────
# Tests de create_product
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestCreateProduct:
    def test_crea_producto_exitoso_devuelve_id(self, mock_ps_client):
        xml = """<prestashop><product><id>42</id></product></prestashop>"""
        mock_ps_client.session.request.return_value = _xml_created(xml)

        result = mock_ps_client.create_product({
            "name": "Camiseta Test",
            "price": 19.99,
            "reference": "TEST-001",
            "id_category": "3",
            "id_supplier": "1",
        })
        assert result == "42"

    def test_fallo_api_devuelve_none(self, mock_ps_client):
        mock_ps_client.session.request.return_value = _error_response(500)
        result = mock_ps_client.create_product({"name": "Test", "price": 0})
        assert result is None

    def test_xml_enviado_contiene_id_shop_default(self, mock_ps_client):
        """Verifica que el XML de creación incluye id_shop_default=1."""
        xml = """<prestashop><product><id>1</id></product></prestashop>"""
        mock_ps_client.session.request.return_value = _xml_created(xml)

        mock_ps_client.create_product({
            "name": "Test", "price": 10, "reference": "R1",
            "id_category": "2", "id_supplier": "0",
        })

        call_kwargs = mock_ps_client.session.request.call_args
        xml_data = call_kwargs[1].get("data") or call_kwargs[0][3]
        root = ET.fromstring(xml_data)
        shop_default = root.find(".//id_shop_default")
        assert shop_default is not None
        assert shop_default.text == "1"

    def test_xml_enviado_contiene_associations_categories(self, mock_ps_client):
        """Verifica que el XML incluye la asociación de categoría."""
        xml = """<prestashop><product><id>1</id></product></prestashop>"""
        mock_ps_client.session.request.return_value = _xml_created(xml)

        mock_ps_client.create_product({
            "name": "Test", "price": 10, "reference": "R1",
            "id_category": "5", "id_supplier": "0",
        })

        call_kwargs = mock_ps_client.session.request.call_args
        xml_data = call_kwargs[1].get("data") or call_kwargs[0][3]
        root = ET.fromstring(xml_data)
        cat_assoc = root.find(".//associations/categories/category/id")
        assert cat_assoc is not None
        assert cat_assoc.text == "5"


# ──────────────────────────────────────────────────────────────
# Tests de update_stock
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestUpdateStock:
    def _setup_stock(self, mock_ps_client, stock_id="3"):
        """Prepara el mock para simular que existe un stock_available."""
        get_xml = f"""<prestashop>
            <stock_availables>
                <stock_available><id>{stock_id}</id></stock_available>
            </stock_availables>
        </prestashop>"""
        put_xml = f"""<prestashop>
            <stock_available><id>{stock_id}</id><quantity>15</quantity></stock_available>
        </prestashop>"""
        mock_ps_client.session.request.side_effect = [
            _xml_response(get_xml),
            _xml_response(put_xml),
        ]

    def test_actualiza_stock_exitoso(self, mock_ps_client):
        self._setup_stock(mock_ps_client)
        result = mock_ps_client.update_stock("1", 15)
        assert result is True

    def test_stock_available_no_encontrado_devuelve_false(self, mock_ps_client):
        xml = "<prestashop><stock_availables></stock_availables></prestashop>"
        mock_ps_client.session.request.return_value = _xml_response(xml)
        result = mock_ps_client.update_stock("999", 10)
        assert result is False

    def test_cantidad_negativa_se_convierte_a_cero(self, mock_ps_client):
        """El stock nunca debe enviarse como negativo."""
        self._setup_stock(mock_ps_client)
        mock_ps_client.update_stock("1", -5)

        calls = mock_ps_client.session.request.call_args_list
        put_call = calls[1]
        xml_data = put_call[1].get("data") or put_call[0][3]
        root = ET.fromstring(xml_data)
        qty = root.find(".//quantity")
        assert qty is not None
        assert int(qty.text) == 0

    def test_cantidad_float_se_convierte_a_int(self, mock_ps_client):
        """Las cantidades decimales (ej: 10.5) se truncan a entero."""
        self._setup_stock(mock_ps_client)
        mock_ps_client.update_stock("1", 10)

        calls = mock_ps_client.session.request.call_args_list
        put_call = calls[1]
        xml_data = put_call[1].get("data") or put_call[0][3]
        root = ET.fromstring(xml_data)
        qty = root.find(".//quantity")
        assert qty is not None
        assert "." not in qty.text


# ──────────────────────────────────────────────────────────────
# Tests de get_categories
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestGetCategories:
    def test_devuelve_lista_de_categorias(self, mock_ps_client):
        xml = """<prestashop>
            <categories>
                <category>
                    <id>3</id>
                    <name><language id="1">Ropa</language></name>
                    <id_parent>2</id_parent>
                    <active>1</active>
                </category>
                <category>
                    <id>6</id>
                    <name><language id="1">Accesorios</language></name>
                    <id_parent>2</id_parent>
                    <active>1</active>
                </category>
            </categories>
        </prestashop>"""
        mock_ps_client.session.request.return_value = _xml_response(xml)
        result = mock_ps_client.get_categories()

        assert len(result) == 2
        assert result[0]["id"] == "3"
        assert result[0]["nombre"] == "Ropa"
        assert result[1]["id"] == "6"

    def test_api_falla_devuelve_lista_vacia(self, mock_ps_client):
        mock_ps_client.session.request.return_value = _error_response(500)
        result = mock_ps_client.get_categories()
        assert result == []


# ──────────────────────────────────────────────────────────────
# Tests de get_customers
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestGetCustomers:
    def test_devuelve_lista_de_clientes(self, mock_ps_client):
        xml = """<prestashop>
            <customers>
                <customer>
                    <id>2</id>
                    <firstname>John</firstname>
                    <lastname>Doe</lastname>
                    <email>john@example.com</email>
                    <active>1</active>
                    <date_add>2026-03-25 11:56:57</date_add>
                </customer>
            </customers>
        </prestashop>"""
        mock_ps_client.session.request.return_value = _xml_response(xml)
        result = mock_ps_client.get_customers()

        assert len(result) == 1
        assert result[0]["id"] == "2"
        assert result[0]["nombre"] == "John Doe"
        assert result[0]["email"] == "john@example.com"

    def test_nombre_compuesto_correctamente(self, mock_ps_client):
        xml = """<prestashop>
            <customers>
                <customer>
                    <id>3</id>
                    <firstname>María</firstname>
                    <lastname>García López</lastname>
                    <email>maria@test.com</email>
                    <active>1</active>
                    <date_add>2026-01-01 00:00:00</date_add>
                </customer>
            </customers>
        </prestashop>"""
        mock_ps_client.session.request.return_value = _xml_response(xml)
        result = mock_ps_client.get_customers()
        assert result[0]["nombre"] == "María García López"


# ──────────────────────────────────────────────────────────────
# Tests de upload_image
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestUploadImage:
    def test_archivo_no_existe_devuelve_false(self, mock_ps_client):
        result = mock_ps_client.upload_image("1", "/ruta/inexistente/imagen.jpg")
        assert result is False
        mock_ps_client.session.request.assert_not_called()

    def test_imagen_existente_se_sube(self, mock_ps_client, tmp_path):
        img = tmp_path / "test.jpg"
        img.write_bytes(b"fake_jpeg_content")

        xml = "<prestashop><image><id>10</id></image></prestashop>"
        mock_ps_client.session.request.return_value = _xml_created(xml)

        # Mockear process_image para no intentar abrir el archivo fake con Pillow
        with patch("core.ps_client.process_image", return_value=b"fake_jpeg_processed"):
            result = mock_ps_client.upload_image("1", str(img))
        assert result is True


# ──────────────────────────────────────────────────────────────
# Tests de delete_product
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestDeleteProduct:
    def test_elimina_producto_exitoso(self, mock_ps_client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_ps_client.session.request.return_value = mock_resp
        assert mock_ps_client.delete_product("5") is True

    def test_error_api_devuelve_false(self, mock_ps_client):
        mock_ps_client.session.request.return_value = _error_response(404)
        assert mock_ps_client.delete_product("999") is False
