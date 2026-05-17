# prestashop9-rest-api

REST API built with FastAPI and Python to manage a PrestaShop 9 store. Connects directly to the local MySQL database and the PrestaShop XML webservice, exposing all store resources via authenticated REST endpoints. Integrates with n8n for automated notifications and workflows.

> 🇪🇸 [Versión en español](README.es.md)

---

## Features

- **Complete REST API** — products, stock, orders, customers, categories, suppliers, images, invoices, discounts, carriers, taxes and statistics
- **JWT authentication** with `admin` / `readonly` roles and refresh tokens
- **Circuit breaker** for PrestaShop and MySQL — protects against external service failures
- **Background scheduler** — monitors low stock and pending orders automatically
- **Advanced logging** — console, daily rotating file and structured JSON for errors
- **290 automated tests** — unit, API and integration
- **10 n8n workflows** ready to import — order notifications, invoices, stock alerts, returns and more
- **Windows + Python 3.13 compatible** — includes fix for `mysql-connector-python 9.x` C extension crash

---

## Tech stack

| Component | Version |
|---|---|
| Python | 3.13 |
| FastAPI | 0.115.5 |
| uvicorn | 0.44.0 |
| mysql-connector-python | 9.1.0 |
| PrestaShop | 9.x (XML webservice) |
| PyJWT + bcrypt | JWT authentication |
| Loguru | Structured logging |
| reportlab | PDF invoice generation |
| APScheduler | Background jobs |
| pytest | Test suite (290 tests) |

---

## Project structure

```
prestashop9-rest-api/
├── app.py                             # Entry point — FastAPI + all endpoints
├── config.py                          # Configuration from environment variables
├── requirements.txt                   # Production dependencies
├── requirements-test.txt              # Testing dependencies
├── pytest.ini                         # pytest configuration
├── .env.example                       # Environment variables template
│
├── core/
│   ├── auth.py                        # JWT, UserManager, bcrypt
│   ├── ps_client.py                   # PrestaShop 9 XML webservice client
│   ├── resilience.py                  # Circuit breaker + exponential backoff retry
│   ├── image_processor.py             # Image processing with Pillow
│   └── invoice_generator.py           # PDF invoice generation with reportlab
│
├── database/
│   └── db_handler.py                  # MySQL connection pool (lazy, with circuit breaker)
│
├── services/
│   └── catalog_service.py             # Local DB ↔ PrestaShop sync logic
│
├── tests/
│   ├── conftest.py                    # Shared fixtures
│   ├── test_ps_client.py              # 29 tests
│   ├── test_db_handler.py             # 15 tests
│   ├── test_catalog_service.py        # 14 tests
│   ├── test_resilience.py             # 22 tests
│   ├── test_auth.py                   # 30 tests
│   ├── test_api_endpoints.py          # 44 tests
│   ├── test_api_endpoints_extended.py # 56 tests
│   ├── test_new_features.py           # 43 tests
│   ├── test_logging.py                # 16 tests
│   └── test_integration.py            # 21 tests
│
└── n8n_workflows/                     # 10 ready-to-import n8n workflows
    ├── 01_Nuevo Pedido PS.json
    ├── 02_Cambio Estado Pedido.json
    ├── 03_Factura PS.json
    ├── 04_Carrito Abandonado PS.json
    ├── 05_Envio con Tracking.json
    ├── 06_Confirmacion Entrega y Valoracion.json
    ├── 07_Alerta Devolucion PS.json
    ├── 08_Stock Bajo.json
    ├── 09_Productos sin Stock.json
    └── 10_Resumen Diario Ventas.json
```

---

## Quick start

```bash
git clone https://github.com/tu-usuario/prestashop9-rest-api.git
cd prestashop9-rest-api
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your credentials
python app.py
```

See [INSTALL.md](INSTALL.md) for the full step-by-step guide.

---

## API reference

### Authentication
| Method | Route | Description |
|---|---|---|
| POST | `/auth/login` | Login — get JWT token |
| POST | `/auth/refresh` | Refresh access token |
| POST | `/auth/change-password` | Change password |

### Users
| Method | Route | Description |
|---|---|---|
| GET | `/users` | List users |
| POST | `/users` | Create user |
| PUT | `/users/{username_or_id}` | Update user |
| DELETE | `/users/{username}` | Deactivate user |

### Products
| Method | Route | Description |
|---|---|---|
| GET | `/products` | List products |
| GET | `/products/reference/{ref}` | Search by reference |
| POST | `/products` | Create product |
| PUT | `/products/{id}` | Update product |
| DELETE | `/products/{id}` | Delete product |
| GET | `/products/{id}/description` | Get description |
| PUT | `/products/{id}/description` | Update description |

### Stock
| Method | Route | Description |
|---|---|---|
| GET | `/stock/{id}` | Get product stock |
| PUT | `/stock/{id}/{qty}` | Update stock |
| GET | `/stock/low` | Low stock products |

### Catalog / Sync
| Method | Route | Description |
|---|---|---|
| POST | `/catalog/sync` | Full sync (background) |
| POST | `/catalog/sync/now` | Full sync (synchronous) |
| POST | `/catalog/sync/product/{id}` | Sync single product |
| GET | `/catalog/reconcile` | DB ↔ PS diff report |

### Categories & Suppliers
| Method | Route | Description |
|---|---|---|
| GET | `/categories` | List categories |
| POST | `/categories` | Create category |
| DELETE | `/categories/{id}` | Delete category |
| GET | `/suppliers` | List suppliers |
| POST | `/suppliers` | Create supplier |
| DELETE | `/suppliers/{id}` | Delete supplier |

### Images
| Method | Route | Description |
|---|---|---|
| GET | `/images/{id}` | List product images |
| POST | `/images/{id}` | Upload images (multipart) |
| DELETE | `/images/{id}/{image_id}` | Delete image |

### Orders
| Method | Route | Description |
|---|---|---|
| GET | `/orders` | List orders |
| GET | `/orders/{id}` | Order detail |
| GET | `/orders/{id}/invoice/pdf` | Download invoice PDF |
| GET | `/orders/states` | Available order states |
| PUT | `/orders/{id}/state/{state_id}` | Change order state |

### Customers
| Method | Route | Description |
|---|---|---|
| GET | `/customers` | List customers |
| GET | `/customers/{id}` | Customer detail |
| GET | `/customers/{id}/orders` | Customer orders |
| GET | `/customers/search` | Search by email |

### Taxes & Discounts
| Method | Route | Description |
|---|---|---|
| GET | `/taxes` | List VAT rates |
| GET | `/taxes/rules` | List tax rule groups |
| PUT | `/taxes/assign` | Assign tax to product |
| GET | `/discounts` | List coupons |
| POST | `/discounts` | Create coupon |
| DELETE | `/discounts/{id}` | Delete coupon |

### Carriers
| Method | Route | Description |
|---|---|---|
| GET | `/carriers` | List carriers |
| POST | `/carriers` | Create carrier |
| PUT | `/carriers/{id}` | Update carrier |
| DELETE | `/carriers/{id}` | Delete carrier |

### Search
| Method | Route | Description |
|---|---|---|
| GET | `/search?q=X` | Global search |
| GET | `/search/products?q=X` | Search products |
| GET | `/search/customers?q=X` | Search customers |
| GET | `/search/categories?q=X` | Search categories |
| GET | `/search/orders?q=X` | Search orders |

### Statistics & Reports
| Method | Route | Description |
|---|---|---|
| GET | `/stats` | Store overview |
| GET | `/stats/orders` | Order statistics |
| GET | `/stats/products` | Catalog statistics |
| GET | `/stats/customers` | Customer statistics |
| GET | `/reports/daily-sales` | Previous day sales summary |

### Scheduler
| Method | Route | Description |
|---|---|---|
| GET | `/scheduler/status` | Job status |
| POST | `/scheduler/run/low-stock` | Run low stock job now |
| POST | `/scheduler/run/pending-orders` | Run pending orders job now |
| PUT | `/scheduler/config/low-stock` | Update interval and threshold |
| PUT | `/scheduler/config/pending-orders` | Update interval |

### System
| Method | Route | Description |
|---|---|---|
| GET | `/health` | Health check |
| GET | `/system/circuit-breakers` | Circuit breaker status |
| POST | `/system/circuit-breakers/reset` | Reset circuit breakers |

---

## Authentication

```bash
# 1. Login
curl -X POST "http://127.0.0.1:8000/auth/login?username=admin&password=YOUR_PASSWORD"

# 2. Use the token in every request
curl -H "Authorization: Bearer YOUR_ACCESS_TOKEN" http://127.0.0.1:8000/products
```

| Role | Permissions |
|---|---|
| `admin` | GET + POST + PUT + DELETE |
| `readonly` | GET only |

> **Windows:** use `http://127.0.0.1:8000` instead of `http://localhost:8000`

---

## Swagger UI

```
http://127.0.0.1:8000/docs
```

Interactive API documentation with **Authorize** button for JWT authentication.

---

## Tests

```bash
python -m pytest tests/ -v                          # All tests (290)
python -m pytest -m "unit or api" -v               # No real connections needed
python -m pytest -m "unit or api" --cov=. --cov-report=term-missing
python -m pytest -m integration -v                 # Requires MySQL and PS running
```

| File | Tests |
|---|---|
| test_ps_client.py | 29 |
| test_db_handler.py | 15 |
| test_catalog_service.py | 14 |
| test_resilience.py | 22 |
| test_auth.py | 30 |
| test_api_endpoints.py | 44 |
| test_api_endpoints_extended.py | 56 |
| test_new_features.py | 43 |
| test_logging.py | 16 |
| test_integration.py | 21 |
| **Total** | **290** |

---

## n8n workflows

| Workflow | Trigger | Description |
|---|---|---|
| 01 — New Order | PS Webhook | Customer confirmation + internal notification |
| 02 — Order Status Change | PS Webhook | Customer email with new status |
| 03 — Invoice | PS Webhook | Download PDF and send to customer |
| 04 — Abandoned Cart | PS Webhook | Recovery email after 2h inactivity |
| 05 — Shipping & Tracking | PS Webhook | Email with tracking number |
| 06 — Delivery & Review | PS Webhook | Review request 2 days after delivery |
| 07 — Return Alert | PS Webhook | Customer confirmation + internal alert |
| 08 — Low Stock | PS Webhook | Internal alert when stock drops below threshold |
| 09 — Out of Stock | PS Webhook | Daily summary of depleted products |
| 10 — Daily Sales Summary | PS Webhook | Previous day sales statistics |

Import in n8n: **Workflows → Import from file**. Each workflow includes a sticky note with configuration instructions.

---

## Resilience

| Service | Threshold | Window | Recovery |
|---|---|---|---|
| PrestaShop | 5 failures | 60s | 30s |
| MySQL | 3 failures | 30s | 15s |

Automatic retry with exponential backoff (1s → 2s → 4s) for HTTP 429, 500, 502, 503, 504.

---

## Logs

| Destination | Level | Rotation |
|---|---|---|
| Console | INFO+ | — |
| `logs/app_YYYY-MM-DD.log` | DEBUG+ | Daily, 30 days |
| `logs/errors.jsonl` | WARNING+ | Cumulative |

---

## License

MIT
