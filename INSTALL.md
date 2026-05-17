# Installation Guide — PrestaShop 9 REST API

Step-by-step guide to install and configure the microservice from scratch until it is fully working with Swagger UI, Postman and n8n.

---

## Prerequisites

| Requirement | Min version | Notes |
|---|---|---|
| Python | 3.13 | Tested on 3.13.12 |
| MySQL | 8.0+ | PrestaShop database |
| PrestaShop | 9.x | With XML webservice enabled |
| n8n | Any | Cloud or self-hosted |

---

## 1. Clone the repository

```bash
git clone https://github.com/tu-usuario/prestashop9-rest-api.git
cd prestashop9-rest-api
```

---

## 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

> **Windows + Python 3.13:** the project uses `use_pure=True` on all MySQL connections to avoid a known crash in `mysql-connector-python 9.x` with its C extension on Windows. No additional steps are required.

---

## 3. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` with your real values:

```env
# PrestaShop
PS_API_KEY=YOUR_API_KEY
PS_BASE_URL=https://your-store.com/api

# MySQL — same credentials PrestaShop uses
DB_HOST=localhost
DB_PORT=3306
DB_USER=your_user
DB_PASSWORD=your_password
DB_NAME=your_database

# Server
APP_HOST=0.0.0.0
APP_PORT=8000
APP_RELOAD=false

# JWT — generate a key with:
# python -c "import secrets; print(secrets.token_hex(32))"
JWT_SECRET_KEY=generated_key_here
JWT_ALGORITHM=HS256
JWT_ACCESS_TOKEN_EXPIRE_MINUTES=240
JWT_REFRESH_TOKEN_EXPIRE_DAYS=7
```

---

## 4. Enable the PrestaShop XML webservice

1. In the PrestaShop admin panel go to:
   **Advanced Parameters → Webservice**

2. Enable the webservice: **Yes**

3. Click **Add new webservice key** and configure:
   - **Key:** click "Generate" or enter your own
   - **Description:** `REST API`
   - **Permissions:** check **GET, POST, PUT, DELETE** on all resources

4. Copy the generated key to `.env` as `PS_API_KEY`

5. Verify it works:
   ```bash
   curl -u YOUR_API_KEY: https://your-store.com/api
   ```
   Should return XML with the available resources.

---

## 5. Create the JWT users table in MySQL

The microservice uses its own `api_users` table in the PrestaShop database to manage API users (separate from store customers).

### 5.1 Create the table

Run on the database configured in `.env`:

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

### 5.2 Create the initial admin user

Generate the bcrypt hash of your password from the terminal:

```bash
python -c "import bcrypt; h=bcrypt.hashpw(b'YOUR_PASSWORD', bcrypt.gensalt(12)).decode(); print(f\"INSERT INTO api_users (username, hashed_password, role) VALUES ('admin', '{h}', 'admin');\")"
```

Run the resulting INSERT on MySQL. The `admin` user will be ready to log in.

---

## 6. Start the microservice

```bash
python app.py
```

Expected output:

```
INFO | DatabaseHandler initialized (lazy pool, db=your_db)
INFO | PRESTASHOP MICROSERVICE — STARTUP
INFO | PS API  : https://your-store.com/api
INFO | MySQL   : localhost/your_db
INFO | Uvicorn running on http://0.0.0.0:8000
INFO | Scheduler: low_stock every 60min | pending_orders every 30min
```

> **Windows:** always use `http://127.0.0.1:8000` instead of `http://localhost:8000` in Postman and the browser.

---

## 7. Swagger UI

Open in your browser:

```
http://127.0.0.1:8000/docs
```

### 7.1 Login

1. Find the `POST /auth/login` endpoint
2. Click **Try it out**
3. Enter `username` and `password`
4. Click **Execute**
5. Copy the `access_token` from the response

### 7.2 Authorize

1. Click the **Authorize** button (top right)
2. Enter `Bearer YOUR_ACCESS_TOKEN`
3. Click **Authorize**

You can now use all endpoints from Swagger.

---

## 8. Postman

### 8.1 Base URL

Always use `http://127.0.0.1:8000` in all requests (not `localhost`).

### 8.2 Login

```
POST http://127.0.0.1:8000/auth/login?username=admin&password=YOUR_PASSWORD
```

The response includes `access_token` and `refresh_token`.

### 8.3 Authentication on each request

- **Authorization** tab → type **Bearer Token**
- Paste the received `access_token`

> The token lasts 240 minutes by default. Refresh it with `POST /auth/refresh`.

---

## 9. n8n

The `n8n_workflows/` folder contains 10 workflows ready to import. Each one includes a sticky note with specific configuration instructions.

### 9.1 Import a workflow

1. In n8n go to **Workflows → Import from file**
2. Select the corresponding `.json` file
3. Read the sticky note in the workflow for setup instructions
4. Configure the indicated credentials
5. Activate the workflow

### 9.2 Required credentials

| Credential | Used by |
|---|---|
| **Gmail OAuth2** | All workflows |
| **Bearer Token API** (Header Auth) | `03_Factura PS` only |
| **Twilio** (optional) | `01_Nuevo Pedido`, `07_Alerta Devolución`, `08_Stock Bajo` |

### 9.3 Configure Bearer Token for n8n

The invoice workflow needs to call the microservice. To authenticate:

1. In n8n go to **Credentials → New → Header Auth**
2. **Name:** `Authorization`
3. **Value:** `Bearer YOUR_ACCESS_TOKEN`

To avoid token expiry, change in `.env`:
```env
JWT_ACCESS_TOKEN_EXPIRE_MINUTES=525600  # 1 year
```

### 9.4 Microservice URL in n8n

If n8n is in the cloud and the microservice is local, you need a public IP or tunnel. In each workflow that calls the API, find the `YOUR_IP:8000` placeholder and replace it with your real URL.

### 9.5 Key endpoints for n8n

| Use | Endpoint | Method |
|---|---|---|
| Low stock | `GET /stock/low?threshold=5` | GET |
| Pending orders | `GET /orders?filter_state=1` | GET |
| Daily summary | `GET /reports/daily-sales` | GET |
| Order detail | `GET /orders/{id}` | GET |
| Invoice PDF | `GET /orders/{id}/invoice/pdf` | GET |
| Change status | `PUT /orders/{id}/state/{state_id}` | PUT |

---

## 10. Verify everything works

```bash
# Health check (no token needed)
curl http://127.0.0.1:8000/health

# Login
curl -X POST "http://127.0.0.1:8000/auth/login?username=admin&password=YOUR_PASSWORD"

# List products (with token)
curl -H "Authorization: Bearer YOUR_TOKEN" http://127.0.0.1:8000/products

# Circuit breaker status
curl -H "Authorization: Bearer YOUR_TOKEN" http://127.0.0.1:8000/system/circuit-breakers
```

---

## 11. Run tests

```bash
# All tests
python -m pytest tests/ -v

# Unit and API only — no real connections, fast
python -m pytest -m "unit or api" -v

# With coverage
python -m pytest -m "unit or api" --cov=. --cov-report=term-missing

# Integration tests — require MySQL and PS running
python -m pytest -m integration -v
```

---

## Troubleshooting

### Port 8000 already in use

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

### ECONNRESET in Postman or browser

Use `http://127.0.0.1:8000` instead of `http://localhost:8000`. On Windows 11, `localhost` resolves to IPv6 (`::1`) while the server listens on IPv4.

### Access violation with MySQL on Windows

The project includes `use_pure=True` on all connections. This is a known bug in `mysql-connector-python 9.x` with its C extension on Python 3.13 on Windows. If the error persists, verify that you have the updated `core/auth.py` and `database/db_handler.py` files.

### Token expired

```json
{"detail": "El token ha expirado. Usa POST /auth/refresh para renovarlo."}
```

```bash
curl -X POST "http://127.0.0.1:8000/auth/refresh" \
  -H "Content-Type: application/json" \
  -d '{"refresh_token": "YOUR_REFRESH_TOKEN"}'
```

### Circuit breaker open

```json
{"detail": "Circuit breaker MySQL ABIERTO"}
```

MySQL unavailable or too many consecutive failures. Check connection and reset:

```bash
curl -X POST -H "Authorization: Bearer YOUR_TOKEN" \
  "http://127.0.0.1:8000/system/circuit-breakers/reset?target=all"
```
