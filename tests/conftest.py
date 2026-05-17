# ============================================================
#  tests/conftest.py — Fixtures compartidos para toda la suite
#
#  ARQUITECTURA DE FIXTURES:
#
#  mock_ps_client / mock_db_handler
#    → Instancias REALES con session/pool mockeado
#    → Los métodos reales se ejecutan (lógica interna testeable)
#    → Para: test_ps_client, test_db_handler, test_logging,
#            test_new_features (clases TestSearch*, TestStats, etc.)
#
#  mock_ps_api / mock_db_api
#    → Todos los métodos PÚBLICOS son MagicMock configurables
#    → Para: test_api_endpoints, test_api_endpoints_extended,
#            test_new_features (clase TestSearchEndpoints)
#
#  client
#    → Usa mock_ps_api + mock_db_api
#    → Cliente HTTP async con token JWT admin incluido
# ============================================================

import pytest
from unittest.mock import MagicMock, patch
from httpx import AsyncClient, ASGITransport

# ──────────────────────────────────────────────────────────────
# Métodos públicos a mockear en mock_ps_api
# ──────────────────────────────────────────────────────────────
_PS_PUBLIC_METHODS = [
    "handle_resource", "get_categories", "create_category", "delete_category",
    "get_suppliers", "create_supplier", "delete_supplier",
    "create_product", "update_product", "update_description",
    "get_product_description", "delete_product", "get_product_by_reference",
    "get_stock", "update_stock", "get_products_snapshot",
    "search_products", "search_customers_by_id_or_name",
    "search_categories_by_id_or_name", "search_suppliers_by_id_or_name",
    "search_orders_by_id_or_ref",
    "get_orders_stats", "get_products_stats", "get_customers_stats",
    "get_categories_stats",
    "upload_image", "get_existing_image_dimensions", "upload_image_bytes",
    "get_product_images", "delete_product_image",
    "get_orders", "get_order", "update_order_state", "get_order_states",
    "get_customers", "get_customer", "search_customers", "get_customer_orders",
    "get_tax_rules", "get_taxes", "create_tax_rule_group", "assign_tax_to_product",
    "get_product_combinations", "get_product_attributes",
    "create_combination", "update_combination_stock",
    "get_features", "get_feature_values", "assign_feature_to_product",
    "get_cart_rules", "create_cart_rule", "delete_cart_rule",
    "get_specific_prices",
    "get_carriers", "get_carrier", "create_carrier", "delete_carrier",
    "update_carrier", "get_order_payments",
    "get_currencies", "get_languages", "update_currency_rate",
    "get_customer_addresses", "create_address", "delete_address",
    "get_customer_groups", "assign_customer_group",
    "get_cms_pages", "get_cms_page", "update_cms_page",
    "get_tags", "get_product_tags", "create_tag",
    "get_countries", "get_zones", "get_product_reviews",
    "get_configuration", "get_configurations", "update_configuration",
]

_DB_PUBLIC_METHODS = [
    "obtener_datos_completos", "guardar_vinculacion",
    "obtener_producto_por_id", "obtener_producto_por_referencia",
    "health_check",
]


# ──────────────────────────────────────────────────────────────
# Fixtures unitarios — instancias REALES con transporte mockeado
# ──────────────────────────────────────────────────────────────

@pytest.fixture
def mock_ps_client():
    """
    PrestashopClient real con session HTTP mockeada.
    Los métodos reales se ejecutan — session.request devuelve lo que el test configure.
    """
    with patch("core.ps_client.requests.Session"):
        from core.ps_client import PrestashopClient
        client = PrestashopClient(api_key="TEST_KEY", base_url="https://test.example.com/api")
        client.session = MagicMock()
        yield client


@pytest.fixture
def mock_db_handler():
    """
    DatabaseHandler real con pool MySQL mockeado.
    Los métodos reales se ejecutan — _pool.get_connection devuelve lo que el test configure.
    """
    with patch("database.db_handler.MySQLConnectionPool"):
        from database.db_handler import DatabaseHandler
        handler = DatabaseHandler()
        handler._pool = MagicMock()
        yield handler


# ──────────────────────────────────────────────────────────────
# Fixtures para tests de API — todos los métodos son MagicMock
# ──────────────────────────────────────────────────────────────

@pytest.fixture
def mock_ps_api():
    """
    PrestashopClient con todos los métodos públicos como MagicMock.
    Usado en tests de endpoints de la API.
    """
    with patch("core.ps_client.requests.Session"):
        from core.ps_client import PrestashopClient
        client = PrestashopClient(api_key="TEST_KEY", base_url="https://test.example.com/api")
        client.session = MagicMock()
        for name in _PS_PUBLIC_METHODS:
            if hasattr(client, name):
                setattr(client, name, MagicMock())
        client._request = MagicMock(return_value=None)
        yield client


@pytest.fixture
def mock_db_api():
    """
    DatabaseHandler con todos los métodos públicos como MagicMock.
    Usado en tests de endpoints de la API.
    """
    with patch("database.db_handler.MySQLConnectionPool"):
        from database.db_handler import DatabaseHandler
        handler = DatabaseHandler()
        handler._pool = MagicMock()
        for name in _DB_PUBLIC_METHODS:
            if hasattr(handler, name):
                setattr(handler, name, MagicMock())
        yield handler


# ──────────────────────────────────────────────────────────────
# Fixture principal de API — cliente HTTP con JWT
# ──────────────────────────────────────────────────────────────

@pytest.fixture
async def client(mock_ps_api, mock_db_api):
    """
    Cliente HTTP que prueba la app FastAPI sin conexiones reales.
    Incluye token JWT de admin en todas las peticiones por defecto.
    Devuelve (client, mock_ps_api, mock_db_api).
    """
    import os
    os.environ.setdefault("JWT_SECRET_KEY", "test_secret_key_32_chars_minimum!!")
    os.environ.setdefault("JWT_ALGORITHM", "HS256")
    os.environ.setdefault("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "60")
    from core.auth import create_access_token
    token = create_access_token("admin", "admin")
    headers = {"Authorization": f"Bearer {token}"}

    with patch("app.ps_client", mock_ps_api), \
         patch("app.db_handler", mock_db_api), \
         patch("app.catalog_service") as mock_catalog:
        mock_catalog.sincronizar_todo.return_value = MagicMock(
            exitoso=True,
            to_dict=lambda: {
                "total_productos": 0, "productos_creados": 0,
                "stocks_actualizados": 0, "imagenes_subidas": 0,
                "errores": [], "exitoso": True,
            }
        )
        from app import app
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers=headers,
        ) as c:
            yield c, mock_ps_api, mock_db_api


# ──────────────────────────────────────────────────────────────
# Datos de ejemplo reutilizables
# ──────────────────────────────────────────────────────────────

@pytest.fixture
def sample_product():
    return {
        "id": 1,
        "nombre": "Camiseta Test",
        "precio": 19.99,
        "referencia": "TEST-001",
        "prestashop_id": None,
        "nombre_categoria": "Ropa",
        "nombre_proveedor": "Proveedor Test",
        "stock": 10,
    }


@pytest.fixture
def sample_product_with_ps_id(sample_product):
    return {**sample_product, "prestashop_id": "42"}


@pytest.fixture
def sample_order():
    return {
        "id": "1",
        "referencia": "TESTREF1",
        "id_cliente": "2",
        "total_pagado": "49.99",
        "id_estado": "2",
        "fecha": "2026-01-15 10:30:00",
        "metodo_pago": "Tarjeta",
        "lineas": [{
            "id_producto": "1",
            "nombre": "Camiseta Test",
            "referencia": "TEST-001",
            "cantidad": "2",
            "precio_unidad": "19.99",
            "total": "39.98",
        }],
    }


@pytest.fixture
def sample_customer():
    return {
        "id": "2",
        "nombre": "John Doe",
        "email": "john@example.com",
        "activo": "1",
        "registro": "2026-03-25 11:56:57",
    }


# ──────────────────────────────────────────────────────────────
# Fixtures de tokens JWT
# ──────────────────────────────────────────────────────────────

@pytest.fixture
def admin_token():
    import os
    os.environ.setdefault("JWT_SECRET_KEY", "test_secret_key_32_chars_minimum!!")
    os.environ.setdefault("JWT_ALGORITHM", "HS256")
    os.environ.setdefault("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "60")
    from core.auth import create_access_token
    return create_access_token("admin", "admin")


@pytest.fixture
def readonly_token():
    import os
    os.environ.setdefault("JWT_SECRET_KEY", "test_secret_key_32_chars_minimum!!")
    os.environ.setdefault("JWT_ALGORITHM", "HS256")
    os.environ.setdefault("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "60")
    from core.auth import create_access_token
    return create_access_token("usuario", "readonly")


# ──────────────────────────────────────────────────────────────
# Fixtures de integración — conexiones reales
# ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def real_ps_client():
    from dotenv import load_dotenv
    load_dotenv()
    from config import settings
    from core.ps_client import PrestashopClient
    return PrestashopClient(settings.ps_api_key, settings.ps_base_url)


@pytest.fixture(scope="session")
def real_db_handler():
    from dotenv import load_dotenv
    load_dotenv()
    from database.db_handler import DatabaseHandler
    return DatabaseHandler()


@pytest.fixture
async def api_client(mock_ps_api, mock_db_api):
    """Cliente HTTP sin token — para tests que gestionan auth manualmente."""
    with patch("app.ps_client", mock_ps_api), \
         patch("app.db_handler", mock_db_api):
        from app import app
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test"
        ) as c:
            yield c
