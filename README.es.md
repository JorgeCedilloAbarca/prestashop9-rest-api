# Microservicio PrestaShop 9 — API REST

> 🇬🇧 [English version](README.md)


Microservicio Python que conecta una base de datos MySQL local con una tienda PrestaShop 9 a través de su webservice XML. Expone una API REST completa con autenticación JWT, circuit breaker, logging avanzado y suite de tests automatizados.

---

## Tecnologías

| Componente | Versión |
|---|---|
| Python | 3.13 |
| FastAPI | 0.115+ |
| uvicorn | 0.44+ |
| MySQL Connector | 9.x (use_pure=True) |
| PrestaShop | 9.x (webservice XML) |
| Autenticación | JWT (PyJWT + bcrypt) |
| Logging | Loguru |
| Tests | pytest 8.x + pytest-asyncio |

---

## Nota importante — Windows + Python 3.13

`mysql-connector-python 9.x` usa por defecto una extensión C que causa crashes en Windows con Python 3.13. El proyecto usa `use_pure=True` en todas las conexiones MySQL para forzar el driver puro Python. **No eliminar este parámetro.**

El arranque usa `WindowsSelectorEventLoopPolicy` automáticamente en Windows. No es necesario ningún paso adicional.

---

## Estructura del proyecto

```
python/
├── app.py                      # Punto de entrada — FastAPI + todos los endpoints
├── config.py                   # Configuración desde .env
├── requirements.txt            # Dependencias de producción
├── requirements-test.txt       # Dependencias de testing
├── pytest.ini                  # Configuración de pytest
├── .env                        # Variables de entorno (NO subir al repo)
│
├── core/
│   ├── auth.py                 # JWT: tokens, UserManager, bcrypt
│   ├── image_processor.py      # Procesamiento de imágenes con Pillow
│   ├── invoice_generator.py    # Generación de facturas PDF con reportlab
│   ├── ps_client.py            # Cliente del webservice XML de PrestaShop 9
│   └── resilience.py           # Circuit breaker + reintentos con backoff
│
├── database/
│   └── db_handler.py           # Pool de conexiones MySQL
│
├── services/
│   └── catalog_service.py      # Lógica de sincronización BD ↔ PrestaShop
│
├── tests/
│   ├── conftest.py             # Fixtures compartidos
│   ├── test_ps_client.py       # Tests del cliente PS
│   ├── test_db_handler.py      # Tests del handler MySQL
│   ├── test_catalog_service.py # Tests del servicio de catálogo
│   ├── test_resilience.py      # Tests del circuit breaker
│   ├── test_auth.py            # Tests de autenticación JWT
│   ├── test_api_endpoints.py   # Tests de endpoints principales
│   ├── test_api_endpoints_extended.py  # Tests de endpoints adicionales
│   ├── test_new_features.py    # Tests de búsqueda, imágenes, estadísticas
│   └── test_logging.py         # Tests del sistema de logging
│
└── logs/                       # Generado automáticamente al arrancar
    ├── app_YYYY-MM-DD.log      # Log diario completo
    └── errors.jsonl            # Errores en formato JSON estructurado
```

---

## Instalación

### 1. Dependencias

```bash
pip install -r requirements.txt
```

### 2. Variables de entorno

```bash
cp .env.example .env
```

Editar `.env`:

```env
# PrestaShop
PS_API_KEY=tu_api_key_aqui
PS_BASE_URL=https://tu-tienda.com/api

# MySQL
DB_HOST=192.168.1.50
DB_PORT=3306
DB_USER=usuario_bd
DB_PASSWORD=contraseña_bd
DB_NAME=nombre_bd

# Servidor
APP_HOST=0.0.0.0
APP_PORT=8000

# JWT — generar con: python -c "import secrets; print(secrets.token_hex(32))"
JWT_SECRET_KEY=clave_secreta_minimo_32_caracteres
JWT_ALGORITHM=HS256
JWT_ACCESS_TOKEN_EXPIRE_MINUTES=240
JWT_REFRESH_TOKEN_EXPIRE_DAYS=7
```

### 3. Crear tabla de usuarios JWT en MySQL

```sql
CREATE TABLE IF NOT EXISTS api_users (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    username        VARCHAR(64)  NOT NULL UNIQUE,
    hashed_password VARCHAR(255) NOT NULL,
    role            ENUM('admin','readonly') NOT NULL DEFAULT 'readonly',
    active          TINYINT(1)   NOT NULL DEFAULT 1,
    created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_login      DATETIME     NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

Crear usuario admin inicial:

```bash
python -c "import bcrypt; h=bcrypt.hashpw(b'SGladmin', bcrypt.gensalt(12)).decode(); print(f\"INSERT INTO api_users (username, hashed_password, role) VALUES ('admin', '{h}', 'admin');\")"
```

Ejecutar el INSERT resultante sobre la BD.

### 4. Arrancar

```bash
python app.py
```

Swagger UI: `http://localhost:8000/docs`

---

## Autenticación

La API usa JWT. Flujo:

1. `POST /auth/login?username=admin&password=SGladmin`
2. Copiar el `access_token` de la respuesta
3. En Swagger: pulsar **Authorize** → introducir el token
4. En Postman: **Authorization → Bearer Token** → pegar el token

> En Windows usar `http://127.0.0.1:8000` en lugar de `http://localhost:8000`

### Roles

| Rol | Permisos |
|---|---|
| `admin` | GET + POST + PUT + DELETE |
| `readonly` | Solo GET |

---

## Referencia de endpoints

### Autenticación
| Método | Ruta | Descripción |
|---|---|---|
| POST | `/auth/login` | Login — obtener token JWT |
| POST | `/auth/refresh` | Renovar access token |
| POST | `/auth/change-password` | Cambiar contraseña |

### Usuarios (admin)
| Método | Ruta | Descripción |
|---|---|---|
| GET | `/users` | Listar usuarios |
| POST | `/users` | Crear usuario |
| PUT | `/users/{username_o_id}` | Modificar usuario |
| DELETE | `/users/{username}` | Desactivar usuario |

### Productos
| Método | Ruta | Descripción |
|---|---|---|
| GET | `/products` | Listar productos |
| GET | `/products/reference/{ref}` | Buscar por referencia |
| POST | `/products` | Crear producto |
| PUT | `/products/{id}` | Editar producto |
| DELETE | `/products/{id}` | Eliminar producto |
| GET | `/products/{id}/description` | Ver descripción |
| PUT | `/products/{id}/description` | Editar descripción |

### Stock
| Método | Ruta | Descripción |
|---|---|---|
| GET | `/stock/{id}` | Ver stock |
| PUT | `/stock/{id}/{qty}` | Actualizar stock |
| GET | `/stock/low` | Productos con stock bajo |

### Catálogo / Sincronización
| Método | Ruta | Descripción |
|---|---|---|
| POST | `/catalog/sync` | Sincronización completa (background) |
| POST | `/catalog/sync/now` | Sincronización completa (síncrona) |
| POST | `/catalog/sync/product/{id}` | Sincronizar un producto |
| GET | `/catalog/reconcile` | Informe BD ↔ PS |

### Categorías y Proveedores
| Método | Ruta | Descripción |
|---|---|---|
| GET | `/categories` | Listar categorías |
| POST | `/categories` | Crear categoría |
| DELETE | `/categories/{id}` | Eliminar categoría |
| GET | `/suppliers` | Listar proveedores |
| POST | `/suppliers` | Crear proveedor |
| DELETE | `/suppliers/{id}` | Eliminar proveedor |

### Imágenes
| Método | Ruta | Descripción |
|---|---|---|
| GET | `/images/{id}` | Listar imágenes |
| POST | `/images/{id}` | Subir imágenes |
| DELETE | `/images/{id}/{image_id}` | Eliminar imagen |

### Pedidos
| Método | Ruta | Descripción |
|---|---|---|
| GET | `/orders` | Listar pedidos |
| GET | `/orders/{id}` | Detalle de pedido |
| GET | `/orders/{id}/invoice/pdf` | Descargar factura PDF |
| GET | `/orders/states` | Estados disponibles |
| PUT | `/orders/{id}/state/{state_id}` | Cambiar estado |

### Clientes
| Método | Ruta | Descripción |
|---|---|---|
| GET | `/customers` | Listar clientes |
| GET | `/customers/{id}` | Detalle de cliente |
| GET | `/customers/{id}/orders` | Pedidos de un cliente |
| GET | `/customers/search` | Buscar por email |

### Impuestos y Descuentos
| Método | Ruta | Descripción |
|---|---|---|
| GET | `/taxes` | Ver tipos de IVA |
| GET | `/taxes/rules` | Ver grupos de impuesto |
| PUT | `/taxes/assign` | Asignar IVA a producto |
| GET | `/discounts` | Listar cupones |
| POST | `/discounts` | Crear cupón |
| DELETE | `/discounts/{id}` | Eliminar cupón |

### Transportistas
| Método | Ruta | Descripción |
|---|---|---|
| GET | `/carriers` | Listar transportistas |
| POST | `/carriers` | Crear transportista |
| PUT | `/carriers/{id}` | Editar transportista |
| DELETE | `/carriers/{id}` | Eliminar transportista |

### Búsqueda
| Método | Ruta | Descripción |
|---|---|---|
| GET | `/search?q=X` | Búsqueda global |
| GET | `/search/products?q=X` | Buscar productos |
| GET | `/search/customers?q=X` | Buscar clientes |
| GET | `/search/categories?q=X` | Buscar categorías |
| GET | `/search/orders?q=X` | Buscar pedidos |

### Estadísticas
| Método | Ruta | Descripción |
|---|---|---|
| GET | `/stats` | Resumen general |
| GET | `/stats/orders` | Estadísticas de pedidos |
| GET | `/stats/products` | Estadísticas de catálogo |
| GET | `/stats/customers` | Estadísticas de clientes |

### Webhooks
| Método | Ruta | Descripción |
|---|---|---|
| POST | `/webhooks/prestashop` | Recibir eventos de PS |
| GET | `/webhooks/events` | Listar eventos recibidos |
| DELETE | `/webhooks/events` | Limpiar eventos |

### Scheduler
| Método | Ruta | Descripción |
|---|---|---|
| GET | `/scheduler/status` | Estado de los jobs |
| POST | `/scheduler/run/low-stock` | Ejecutar job ahora |
| POST | `/scheduler/run/pending-orders` | Ejecutar job ahora |
| PUT | `/scheduler/config/low-stock` | Cambiar intervalo/umbral |
| PUT | `/scheduler/config/pending-orders` | Cambiar intervalo |

### Sistema
| Método | Ruta | Descripción |
|---|---|---|
| GET | `/health` | Health check |
| GET | `/system/circuit-breakers` | Estado circuit breakers |
| POST | `/system/circuit-breakers/reset` | Resetear circuit breakers |

---

## Tests

```bash
# Todos los tests
python -m pytest tests/ -v

# Solo unitarios y API (sin conexiones reales)
python -m pytest -m "unit or api" -v

# Con cobertura
python -m pytest -m "unit or api" --cov=. --cov-report=term-missing

# Tests de integración (requieren MySQL y PS activos)
python -m pytest -m integration -v
```

**Estado actual: 290 tests, 0 fallos.**

| Archivo | Tests |
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

---

## Logs

| Destino | Nivel | Rotación |
|---|---|---|
| Terminal | INFO+ | — |
| `logs/app_YYYY-MM-DD.log` | DEBUG+ | Diaria, 30 días |
| `logs/errors.jsonl` | WARNING+ | Acumulativo |

---

## Resiliencia

### Circuit Breaker
| Circuit | Umbral | Ventana | Recuperación |
|---|---|---|---|
| PrestaShop | 5 fallos | 60s | 30s |
| MySQL | 3 fallos | 30s | 15s |

### Reintentos automáticos
- HTTP 429, 500, 502, 503, 504 → hasta 3 reintentos con backoff exponencial
- Errores 4xx → no se reintentan

---

## n8n — Workflows activos

10 workflows en `pruebaps.app.n8n.cloud`:

| Workflow | Trigger |
|---|---|
| Nuevo Pedido | `new_order` webhook |
| Factura | `order_status` + invoice_exists |
| Cambio Estado | `order_status` webhook |
| Stock bajo | Scheduler cada 60min |
| Carrito abandonado | Scheduler |
| Envío con tracking | `order_status` |
| Confirmación entrega | `order_status` |
| Alerta devolución | `order_status` |
| Resumen Diario | Scheduler |
| Productos sin Stock | Scheduler |

**Pendiente para producción:** cambiar URLs de ngrok por IP/dominio fijo y activar email real del cliente en workflows Gmail.

---

## Seguridad

- Contraseñas hasheadas con bcrypt (12 rounds)
- Tokens JWT firmados con clave secreta configurable
- Middleware JWT en todas las rutas excepto las públicas
- Rutas públicas: `/`, `/health`, `/auth/login`, `/auth/refresh`, `/docs`, `/webhooks/*`
