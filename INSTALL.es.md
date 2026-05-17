# Guía de instalación — Microservicio PrestaShop 9

Guía paso a paso para instalar y configurar el microservicio desde cero hasta tenerlo funcionando con Swagger UI, Postman y n8n.

---

## Requisitos previos

| Requisito | Versión mínima | Notas |
|---|---|---|
| Python | 3.13 | Probado en 3.13.12 |
| MySQL | 8.0+ | Base de datos de PrestaShop |
| PrestaShop | 9.x | Con webservice XML activado |
| n8n | Cualquiera | Cloud o self-hosted |

---

## 1. Clonar el repositorio

```bash
git clone https://github.com/tu-usuario/microservicio-prestashop.git
cd microservicio-prestashop
```

---

## 2. Instalar dependencias Python

```bash
pip install -r requirements.txt
```

> **Windows + Python 3.13:** el proyecto usa `use_pure=True` en todas las conexiones MySQL para evitar un crash conocido de `mysql-connector-python 9.x` con la extensión C en Windows. No es necesario ningún paso adicional.

---

## 3. Configurar variables de entorno

```bash
cp .env.example .env
```

Editar `.env` con tus valores reales:

```env
# PrestaShop
PS_API_KEY=TU_API_KEY
PS_BASE_URL=https://tu-tienda.com/api

# MySQL — mismas credenciales que usa PrestaShop
DB_HOST=localhost
DB_PORT=3306
DB_USER=tu_usuario
DB_PASSWORD=tu_contraseña
DB_NAME=tu_base_de_datos

# Servidor
APP_HOST=0.0.0.0
APP_PORT=8000
APP_RELOAD=false

# JWT — generar clave con:
# python -c "import secrets; print(secrets.token_hex(32))"
JWT_SECRET_KEY=clave_generada_aqui
JWT_ALGORITHM=HS256
JWT_ACCESS_TOKEN_EXPIRE_MINUTES=240
JWT_REFRESH_TOKEN_EXPIRE_DAYS=7
```

---

## 4. Activar el webservice XML de PrestaShop

1. En el panel de administración de PrestaShop ir a:
   **Parámetros avanzados → Webservice**

2. Activar el webservice: **Sí**

3. Pulsar **Añadir nueva clave webservice** y configurar:
   - **Clave:** pulsar "Generar" o poner la tuya
   - **Descripción:** `Microservicio API`
   - **Permisos:** marcar **GET, POST, PUT, DELETE** en todos los recursos

4. Copiar la clave generada al `.env` como `PS_API_KEY`

5. Verificar que funciona:
   ```bash
   curl -u TU_API_KEY: https://tu-tienda.com/api
   ```
   Debe devolver XML con los recursos disponibles.

---

## 5. Crear tabla de usuarios JWT en MySQL

El microservicio usa su propia tabla `api_users` en la base de datos de PrestaShop para gestionar los usuarios de la API (independiente de los clientes de la tienda).

### 5.1 Crear la tabla

Ejecutar sobre la base de datos configurada en el `.env`:

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

### 5.2 Crear el usuario admin inicial

Generar el hash bcrypt de tu contraseña desde el terminal:

```bash
python -c "import bcrypt; h=bcrypt.hashpw(b'TU_CONTRASEÑA', bcrypt.gensalt(12)).decode(); print(f\"INSERT INTO api_users (username, hashed_password, role) VALUES ('admin', '{h}', 'admin');\")"
```

Ejecutar el INSERT resultante en MySQL. El usuario `admin` ya estará listo para hacer login.

---

## 6. Arrancar el microservicio

```bash
python app.py
```

Salida esperada:

```
INFO | DatabaseHandler inicializado (pool lazy, db=tu_bd)
INFO | MICROSERVICIO PRESTASHOP — ARRANQUE
INFO | PS API  : https://tu-tienda.com/api
INFO | MySQL   : localhost/tu_bd
INFO | Uvicorn running on http://0.0.0.0:8000
INFO | Scheduler: low_stock cada 60min | pending_orders cada 30min
```

> **Windows:** usar siempre `http://127.0.0.1:8000` en lugar de `http://localhost:8000` en Postman y el navegador.

---

## 7. Swagger UI

Abrir en el navegador:

```
http://127.0.0.1:8000/docs
```

### 7.1 Hacer login

1. Buscar el endpoint `POST /auth/login`
2. Pulsar **Try it out**
3. Introducir `username` y `password`
4. Pulsar **Execute**
5. Copiar el valor de `access_token` de la respuesta

### 7.2 Autorizar

1. Pulsar el botón **Authorize** (arriba a la derecha)
2. Introducir `Bearer TU_ACCESS_TOKEN`
3. Pulsar **Authorize**

Ya puedes usar todos los endpoints desde Swagger.

---

## 8. Postman

### 8.1 URL base

Usar siempre `http://127.0.0.1:8000` en todas las requests (no `localhost`).

### 8.2 Login

```
POST http://127.0.0.1:8000/auth/login?username=admin&password=TU_CONTRASEÑA
```

La respuesta incluye `access_token` y `refresh_token`.

### 8.3 Autenticación en cada request

- Pestaña **Authorization** → tipo **Bearer Token**
- Pegar el `access_token` recibido

> El token dura 240 minutos por defecto. Renovar con `POST /auth/refresh`.

---

## 9. n8n

La carpeta `n8n_workflows/` contiene los 10 workflows listos para importar. Cada uno incluye una nota con las instrucciones de configuración específicas.

### 9.1 Importar un workflow

1. En n8n ir a **Workflows → Import from file**
2. Seleccionar el archivo `.json` correspondiente
3. Leer la nota sticky del workflow con las instrucciones
4. Configurar las credenciales indicadas
5. Activar el workflow

### 9.2 Credenciales necesarias

| Credencial | Workflows que la usan |
|---|---|
| **Gmail OAuth2** | Todos los workflows |
| **Bearer Token API** (Header Auth) | Solo `03_Factura PS` |
| **Twilio** (opcional) | `01_Nuevo Pedido`, `07_Alerta Devolución`, `08_Stock Bajo` |

### 9.3 Configurar Bearer Token para n8n

El workflow de factura necesita llamar al microservicio. Para autenticarse:

1. En n8n ir a **Credentials → New → Header Auth**
2. **Name:** `Authorization`
3. **Value:** `Bearer TU_ACCESS_TOKEN`

Para evitar que el token expire, cambiar en `.env`:
```env
JWT_ACCESS_TOKEN_EXPIRE_MINUTES=525600  # 1 año
```

### 9.4 URL del microservicio en n8n

Si n8n está en la nube y el microservicio en local, necesitas una IP pública o túnel. En cada workflow que llame a la API, busca el placeholder `TU_IP:8000` y sustitúyelo por tu URL real.

### 9.5 Endpoints clave para n8n

| Uso | Endpoint | Método |
|---|---|---|
| Stock bajo | `GET /stock/low?threshold=5` | GET |
| Pedidos pendientes | `GET /orders?filter_state=1` | GET |
| Resumen diario | `GET /reports/daily-sales` | GET |
| Detalle pedido | `GET /orders/{id}` | GET |
| Factura PDF | `GET /orders/{id}/invoice/pdf` | GET |
| Cambiar estado | `PUT /orders/{id}/state/{state_id}` | PUT |

---

## 10. Verificar que todo funciona

```bash
# Health check (sin token)
curl http://127.0.0.1:8000/health

# Login
curl -X POST "http://127.0.0.1:8000/auth/login?username=admin&password=TU_CONTRASEÑA"

# Listar productos (con token)
curl -H "Authorization: Bearer TU_TOKEN" http://127.0.0.1:8000/products

# Estado circuit breakers
curl -H "Authorization: Bearer TU_TOKEN" http://127.0.0.1:8000/system/circuit-breakers
```

---

## 11. Tests

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

---

## Resolución de problemas

### Puerto 8000 ocupado

```
ERROR: [Errno 10048] error while attempting to bind on address ('0.0.0.0', 8000)
```

```powershell
# Windows PowerShell
Get-Process -Name python -ErrorAction SilentlyContinue | Stop-Process -Force
```

```bash
# Linux/Mac
fuser -k 8000/tcp
```

### ECONNRESET en Postman o navegador

Usar `http://127.0.0.1:8000` en lugar de `http://localhost:8000`. En Windows 11, `localhost` resuelve a IPv6 (`::1`) mientras el servidor escucha en IPv4.

### Access violation con MySQL en Windows

El proyecto incluye `use_pure=True` en todas las conexiones. Es un bug conocido de `mysql-connector-python 9.x` con su extensión C en Python 3.13 en Windows. Si el error persiste, verificar que tienes los archivos actualizados de `core/auth.py` y `database/db_handler.py`.

### Token expirado

```json
{"detail": "El token ha expirado. Usa POST /auth/refresh para renovarlo."}
```

```bash
curl -X POST "http://127.0.0.1:8000/auth/refresh" \
  -H "Content-Type: application/json" \
  -d '{"refresh_token": "TU_REFRESH_TOKEN"}'
```

### Circuit breaker abierto

```json
{"detail": "Circuit breaker MySQL ABIERTO"}
```

MySQL no disponible o demasiados fallos consecutivos. Verificar conexión y resetear:

```bash
curl -X POST -H "Authorization: Bearer TU_TOKEN" \
  "http://127.0.0.1:8000/system/circuit-breakers/reset?target=all"
```
