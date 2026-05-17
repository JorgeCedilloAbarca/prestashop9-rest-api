# ============================================================
#  app.py — Punto de entrada del Microservicio PrestaShop
#  FastAPI con endpoints REST, logging y health check
# ============================================================

import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Path, Query, Request, UploadFile, Depends, status
from typing import List, Optional
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel

from config import settings
from core.auth import (
    create_access_token, create_refresh_token, decode_token,
    require_auth, require_admin, user_manager,
)
from core.ps_client import PrestashopClient
from core.resilience import prestashop_circuit, mysql_circuit
from database.db_handler import DatabaseHandler
from services.catalog_service import CatalogService, SyncResult
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger


# ──────────────────────────────────────────────────────────────
# Logging avanzado — 3 destinos con niveles y formatos distintos
# ──────────────────────────────────────────────────────────────

import json as _json
import os as _os
import pathlib as _pathlib

# Ruta absoluta basada en la ubicación de app.py — funciona desde cualquier directorio
_BASE_DIR = _pathlib.Path(__file__).parent.resolve()
_LOG_DIR  = _BASE_DIR / "logs"
_os.makedirs(_LOG_DIR, exist_ok=True)

logger.remove()  # Eliminar el handler por defecto de loguru

# ── 1. Consola — INFO+ con colores, formato legible ────────────
logger.add(
    sys.stdout,
    format=(
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{line}</cyan> — <level>{message}</level>"
    ),
    level="INFO",
    colorize=True,
    enqueue=True,   # thread-safe
)

# ── 2. Archivo general — DEBUG+ rotativo por fecha ─────────────
logger.add(
    str(_LOG_DIR / "app_{time:YYYY-MM-DD}.log"),
    rotation="00:00",          # nuevo archivo cada día a medianoche
    retention="30 days",       # conservar 30 días de histórico
    compression="gz",          # comprimir archivos antiguos
    level="DEBUG",
    encoding="utf-8",
    enqueue=True,
    format=(
        "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
        "{name}:{line} | {message}"
    ),
)

# ── 3. Archivo de errores — WARNING+ en JSON estructurado ──────
def _json_sink(message):
    """Serializa cada log de WARNING+ como JSON para análisis externo."""
    record = message.record
    entry = {
        "timestamp": record["time"].isoformat(),
        "level":     record["level"].name,
        "module":    record["name"],
        "line":      record["line"],
        "message":   record["message"],
        "exception": str(record["exception"]) if record["exception"] else None,
    }
    with open(str(_LOG_DIR / "errors.jsonl"), "a", encoding="utf-8") as f:
        f.write(_json.dumps(entry, ensure_ascii=False) + "\n")

logger.add(
    _json_sink,
    level="WARNING",
    enqueue=True,
)

# ── 4. Niveles por módulo ───────────────────────────────────────
# Silenciar logs de bibliotecas externas ruidosas
import logging as _std_logging
_std_logging.getLogger("uvicorn.access").setLevel(_std_logging.WARNING)
_std_logging.getLogger("uvicorn.error").setLevel(_std_logging.WARNING)

# ──────────────────────────────────────────────────────────────
# Inicialización de componentes (singleton por proceso)
# ──────────────────────────────────────────────────────────────

ps_client      = PrestashopClient(settings.ps_api_key, settings.ps_base_url)
db_handler     = DatabaseHandler()
catalog_service = CatalogService(ps_client, db_handler)

# ──────────────────────────────────────────────────────────────
# Scheduler — jobs de monitorización en background
# ──────────────────────────────────────────────────────────────

# Estado del scheduler (accesible desde los endpoints de control)
_scheduler = AsyncIOScheduler()
_scheduler_jobs_config = {
    "low_stock": {
        "enabled":   True,
        "interval":  60,      # minutos
        "threshold": 5,
        "last_run":  None,
        "last_count": None,
    },
    "pending_orders": {
        "enabled":  True,
        "interval": 30,       # minutos
        "last_run": None,
        "last_count": None,
    },
}


def _job_check_low_stock():
    """Job: comprueba productos con stock bajo y registra evento si encuentra alguno."""
    cfg = _scheduler_jobs_config["low_stock"]
    if not cfg["enabled"]:
        return
    try:
        from datetime import datetime, timezone
        productos = ps_client.get_low_stock_products(threshold=cfg["threshold"])
        cfg["last_run"]   = datetime.now(timezone.utc).isoformat()
        cfg["last_count"] = len(productos)
        if productos:
            payload = {
                "threshold": cfg["threshold"],
                "total":     len(productos),
                "productos": productos[:10],
            }
            logger.warning(
                "SCHEDULER | low_stock | {total} productos con stock ≤ {threshold}",
                total=len(productos),
                threshold=cfg["threshold"],
            )
        else:
            logger.info(
                "SCHEDULER | low_stock | Sin productos bajo umbral {threshold}",
                threshold=cfg["threshold"],
            )
    except Exception as exc:
        logger.error("SCHEDULER | low_stock | Error: {exc}", exc=exc)


def _job_check_pending_orders():
    """Job: comprueba pedidos en estado pendiente de pago y registra evento."""
    cfg = _scheduler_jobs_config["pending_orders"]
    if not cfg["enabled"]:
        return
    try:
        from datetime import datetime, timezone
        # Estado 1 = Pago pendiente en PrestaShop por defecto
        result = ps_client._request(
            "GET",
            "orders?filter[current_state]=1&display=[id,reference,total_paid,date_add]&limit=50",
        )
        pedidos = []
        if result is not None:
            for order in result.findall(".//order"):
                oid  = order.findtext("id") or ""
                ref  = order.findtext("reference") or ""
                total = order.findtext("total_paid") or ""
                date  = order.findtext("date_add") or ""
                if oid:
                    pedidos.append({"id": oid, "referencia": ref, "total": total, "fecha": date})

        cfg["last_run"]   = datetime.now(timezone.utc).isoformat()
        cfg["last_count"] = len(pedidos)

        if pedidos:
            payload = {"estado": "Pago pendiente", "total": len(pedidos), "pedidos": pedidos[:10]}
            logger.warning(
                "SCHEDULER | pending_orders | {total} pedidos pendientes de pago",
                total=len(pedidos),
            )
        else:
            logger.info("SCHEDULER | pending_orders | Sin pedidos pendientes de pago")
    except Exception as exc:
        logger.error("SCHEDULER | pending_orders | Error: {exc}", exc=exc)


# ──────────────────────────────────────────────────────────────
# Lifespan
# ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 60)
    logger.info("  MICROSERVICIO PRESTASHOP — ARRANQUE")
    logger.info("=" * 60)
    logger.info("  PS API  : {url}", url=settings.ps_base_url)
    logger.info("  MySQL   : {host}/{db}", host=settings.db_host, db=settings.db_name)
    logger.info("  Logs    : logs/app_YYYY-MM-DD.log | logs/errors.jsonl")
    logger.info("=" * 60)

    # Arrancar scheduler directamente en el lifespan
    try:
        cfg_ls = _scheduler_jobs_config["low_stock"]
        cfg_po = _scheduler_jobs_config["pending_orders"]
        _scheduler.add_job(
            _job_check_low_stock,
            trigger=IntervalTrigger(minutes=cfg_ls["interval"]),
            id="low_stock", replace_existing=True,
        )
        _scheduler.add_job(
            _job_check_pending_orders,
            trigger=IntervalTrigger(minutes=cfg_po["interval"]),
            id="pending_orders", replace_existing=True,
        )
        _scheduler.start()
        logger.info(
            "Scheduler: low_stock cada {ls}min | pending_orders cada {po}min",
            ls=cfg_ls["interval"], po=cfg_po["interval"],
        )
    except Exception as _e:
        logger.warning("⚠ Scheduler no pudo arrancar: {e}", e=_e)

    yield

    try:
        _scheduler.shutdown(wait=False)
    except Exception:
        pass
    logger.info("Microservicio PrestaShop detenido correctamente.")

# ──────────────────────────────────────────────────────────────
# Aplicación FastAPI
# ──────────────────────────────────────────────────────────────

app = FastAPI(
    title="PrestaShop Management Microservice",
    description=(
        "API REST para sincronizar y gestionar el catálogo de productos "
        "entre una base de datos MySQL local y una tienda PrestaShop 9.\n\n"
        "**Autenticación:** Haz login en `POST /auth/login` para obtener el token, "
        "luego pulsa **Authorize** e introduce `Bearer <tu_token>`."
    ),
    version="3.0.0",
    lifespan=lifespan,
    docs_url=None,
)


# ──────────────────────────────────────────────────────────────
# Middleware de autenticación JWT
# ──────────────────────────────────────────────────────────────
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse as StarletteJSON
import jwt as pyjwt

# Rutas completamente públicas — no requieren token
_PUBLIC_PATHS = {
    "/",
    "/docs",
    "/openapi.json",
    "/auth/login",
    "/auth/refresh",
}

# Prefijos públicos — cualquier subruta que empiece por estos no requiere token.
_PUBLIC_PREFIXES: tuple = ()

class JWTMiddleware:
    """
    Middleware ASGI puro que valida el JWT en todas las rutas excepto las públicas.
    Lee headers directamente del scope — compatible con Python 3.13 + Windows.
    """
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        # Leer path, method y headers DIRECTAMENTE del scope — sin StarletteRequest
        path   = scope.get("path", "")
        method = scope.get("method", "GET").upper()
        headers = dict(scope.get("headers", []))

        # Rutas públicas
        _AUTH_PUBLIC = {"/auth/login", "/auth/refresh"}
        if path in _PUBLIC_PATHS or path in _AUTH_PUBLIC or path.startswith(_PUBLIC_PREFIXES):
            await self.app(scope, receive, send)
            return

        # Extraer token del header Authorization
        auth_bytes = headers.get(b"authorization", b"")
        auth_header = auth_bytes.decode("latin-1") if isinstance(auth_bytes, bytes) else auth_bytes

        if not auth_header.startswith("Bearer "):
            response = StarletteJSON(
                {"detail": "Token de autenticación requerido. Haz login en POST /auth/login."},
                status_code=401,
            )
            await response(scope, receive, send)
            return

        token = auth_header[7:]
        try:
            payload = pyjwt.decode(
                token,
                settings.jwt_secret_key,
                algorithms=[settings.jwt_algorithm],
            )
            if payload.get("type") != "access":
                raise pyjwt.PyJWTError("Tipo de token incorrecto")
        except pyjwt.ExpiredSignatureError:
            response = StarletteJSON(
                {"detail": "El token ha expirado. Usa POST /auth/refresh para renovarlo."},
                status_code=401,
            )
            await response(scope, receive, send)
            return
        except pyjwt.PyJWTError as exc:
            response = StarletteJSON(
                {"detail": f"Token inválido: {exc}"},
                status_code=401,
            )
            await response(scope, receive, send)
            return

        # Verificar rol para escritura
        if method in ("POST", "PUT", "DELETE", "PATCH"):
            if path not in ("/auth/change-password",) and payload.get("role") != "admin":
                response = StarletteJSON(
                    {"detail": "Acceso denegado. Se requiere rol 'admin' para esta operación."},
                    status_code=403,
                )
                await response(scope, receive, send)
                return

        scope.setdefault("state", {})["user"] = payload
        await self.app(scope, receive, send)

app.add_middleware(JWTMiddleware)

# CORS — permite peticiones desde el dashboard HTML local (file://) y localhost
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # En producción limita a tu dominio
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# ──────────────────────────────────────────────────────────────
# OpenAPI security scheme — necesario para el botón Authorize
# ──────────────────────────────────────────────────────────────

from fastapi.openapi.utils import get_openapi

def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    # Dejar solo BearerAuth — eliminar HTTPBearer auto-generado por FastAPI
    schemes = schema.setdefault("components", {}).setdefault("securitySchemes", {})
    schemes.clear()
    schemes["BearerAuth"] = {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": "Introduce el access_token obtenido en POST /auth/login",
    }
    # Limpiar security de todos los paths y aplicar solo BearerAuth donde corresponde
    public_paths = {"/auth/login", "/auth/refresh", "/docs", "/openapi.json", "/", "/health"}
    for path, methods in schema.get("paths", {}).items():
        for method_data in methods.values():
            # Quitar cualquier security auto-generado
            method_data.pop("security", None)
        if path in public_paths or path.startswith(public_prefixes):
            continue
        for method_data in methods.values():
            method_data["security"] = [{"BearerAuth": []}]
    app.openapi_schema = schema
    return schema

app.openapi = custom_openapi

# ──────────────────────────────────────────────────────────────
# Swagger UI personalizado con botones "Consultar"
# ──────────────────────────────────────────────────────────────

from fastapi.responses import HTMLResponse

@app.get("/docs", include_in_schema=False)
async def custom_swagger() -> HTMLResponse:
    return HTMLResponse("""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <title>PrestaShop Microservice — API Docs</title>
  <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    /* ── Login banner ── */
    #ps-login-banner {
      display: flex; align-items: center; gap: 12px;
      background: #fffbeb; border: 1px solid #fcd34d;
      border-radius: 10px; padding: 12px 20px; margin: 12px 16px 0;
      font-family: 'Inter', sans-serif; font-size: 13px; color: #92400e;
    }
    #ps-login-banner.hidden { display: none; }
    #ps-login-banner svg { flex-shrink: 0; }
    #ps-login-banner a {
      color: #2563eb; font-weight: 600; cursor: pointer; text-decoration: underline;
    }
    #ps-logged-banner {
      display: none; align-items: center; gap: 10px;
      background: #f0fdf4; border: 1px solid #86efac;
      border-radius: 10px; padding: 10px 20px; margin: 12px 16px 0;
      font-family: 'Inter', sans-serif; font-size: 13px; color: #166534;
    }
    #ps-logged-banner.visible { display: flex; }
    #ps-token-role {
      background: #dcfce7; color: #166534; border-radius: 4px;
      padding: 1px 8px; font-weight: 600; font-size: 12px;
    }
    #ps-logout-btn {
      margin-left: auto; background: none; border: 1px solid #86efac;
      border-radius: 6px; padding: 3px 10px; color: #166534;
      cursor: pointer; font-size: 12px; font-family: 'Inter', sans-serif;
    }
    #ps-logout-btn:hover { background: #dcfce7; }
  </style>
  <style>
    /* ── Variables globales ── */
    :root {
      --ps-blue:       #2563eb;
      --ps-blue-dark:  #1d4ed8;
      --ps-blue-light: #eff6ff;
      --ps-border:     #e2e8f0;
      --ps-text:       #1e293b;
      --ps-muted:      #64748b;
      --ps-success:    #16a34a;
      --ps-radius:     10px;
      --ps-shadow:     0 20px 60px rgba(0,0,0,.18), 0 4px 16px rgba(0,0,0,.08);
    }

    /* ── Overlay ── */
    #ps-overlay {
      display: none; position: fixed; inset: 0;
      background: rgba(15,23,42,.45);
      backdrop-filter: blur(3px);
      z-index: 9999;
      align-items: center; justify-content: center;
      animation: fadeIn .18s ease;
    }
    #ps-overlay.open { display: flex; }
    @keyframes fadeIn { from { opacity:0 } to { opacity:1 } }

    /* ── Modal ── */
    #ps-modal {
      background: #fff;
      border-radius: 16px;
      width: 90%; max-width: 620px; max-height: 82vh;
      display: flex; flex-direction: column;
      box-shadow: var(--ps-shadow);
      animation: slideUp .2s cubic-bezier(.16,1,.3,1);
      overflow: hidden;
    }
    @keyframes slideUp {
      from { opacity:0; transform: translateY(18px) scale(.97) }
      to   { opacity:1; transform: translateY(0)    scale(1)   }
    }

    /* ── Modal header ── */
    #ps-modal-header {
      display: flex; align-items: center; justify-content: space-between;
      padding: 20px 24px 16px;
      border-bottom: 1px solid var(--ps-border);
    }
    #ps-modal-icon {
      width: 34px; height: 34px; border-radius: 8px;
      background: var(--ps-blue-light);
      display: flex; align-items: center; justify-content: center;
      margin-right: 12px; flex-shrink: 0;
      font-size: 16px;
    }
    #ps-modal-title-wrap { flex: 1; }
    #ps-modal-title {
      font-family: 'Inter', sans-serif;
      font-size: 15px; font-weight: 600; color: var(--ps-text);
      margin: 0;
    }
    #ps-modal-subtitle {
      font-family: 'Inter', sans-serif;
      font-size: 12px; color: var(--ps-muted); margin-top: 2px;
    }
    #ps-modal-close {
      width: 30px; height: 30px; border-radius: 8px;
      border: 1px solid var(--ps-border); background: #fff;
      cursor: pointer; color: var(--ps-muted);
      display: flex; align-items: center; justify-content: center;
      font-size: 16px; transition: all .15s;
    }
    #ps-modal-close:hover { background: #f8fafc; color: var(--ps-text); border-color: #cbd5e1; }

    /* ── Search bar ── */
    #ps-search-wrap {
      padding: 14px 24px 10px;
      border-bottom: 1px solid var(--ps-border);
    }
    #ps-search {
      width: 100%; box-sizing: border-box;
      padding: 9px 14px 9px 36px;
      border: 1px solid var(--ps-border); border-radius: 8px;
      font-family: 'Inter', sans-serif; font-size: 13.5px;
      color: var(--ps-text); outline: none; background: #f8fafc;
      background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='15' height='15' viewBox='0 0 24 24' fill='none' stroke='%2394a3b8' stroke-width='2'%3E%3Ccircle cx='11' cy='11' r='8'/%3E%3Cpath d='m21 21-4.35-4.35'/%3E%3C/svg%3E");
      background-repeat: no-repeat; background-position: 11px center;
      transition: border-color .15s, box-shadow .15s;
    }
    #ps-search:focus { border-color: var(--ps-blue); box-shadow: 0 0 0 3px rgba(37,99,235,.12); background-color: #fff; }

    /* ── Cuerpo scrollable ── */
    #ps-modal-body { overflow-y: auto; flex: 1; padding: 0; }

    /* ── Tabla ── */
    #ps-modal-body table {
      width: 100%; border-collapse: collapse;
      font-family: 'Inter', sans-serif; font-size: 13.5px;
    }
    #ps-modal-body thead th {
      background: #f8fafc; text-align: left;
      padding: 11px 24px; color: var(--ps-muted);
      font-weight: 600; font-size: 11.5px; letter-spacing: .04em;
      text-transform: uppercase; border-bottom: 1px solid var(--ps-border);
      position: sticky; top: 0;
    }
    #ps-modal-body tbody td {
      padding: 11px 24px; border-bottom: 1px solid #f1f5f9; color: var(--ps-text);
      vertical-align: middle;
    }
    #ps-modal-body tbody tr:last-child td { border-bottom: none; }
    #ps-modal-body tbody tr { transition: background .1s; cursor: pointer; }
    #ps-modal-body tbody tr:hover td { background: #f8fbff; }

    /* ── ID chip ── */
    .ps-id-chip {
      display: inline-flex; align-items: center; gap: 5px;
      background: var(--ps-blue-light); color: var(--ps-blue);
      border: 1px solid #bfdbfe; border-radius: 6px;
      padding: 3px 10px; font-weight: 600; font-size: 13px;
      cursor: pointer; transition: all .15s; user-select: none;
      white-space: nowrap;
    }
    .ps-id-chip svg { opacity: .6; }
    .ps-id-chip:hover { background: var(--ps-blue); color: #fff; border-color: var(--ps-blue); }
    .ps-id-chip:hover svg { opacity: 1; }
    .ps-id-chip.copied { background: #dcfce7; color: var(--ps-success); border-color: #86efac; }

    /* ── Footer del modal ── */
    #ps-modal-footer {
      padding: 12px 24px;
      border-top: 1px solid var(--ps-border);
      display: flex; align-items: center; justify-content: space-between;
    }
    #ps-modal-count { font-family: 'Inter', sans-serif; font-size: 12px; color: var(--ps-muted); }
    #ps-modal-hint  { font-family: 'Inter', sans-serif; font-size: 12px; color: var(--ps-muted);
      display: flex; align-items: center; gap: 5px; }

    /* ── Loading skeleton ── */
    .ps-skeleton { padding: 20px 24px; }
    .ps-skeleton-row {
      height: 16px; background: linear-gradient(90deg, #f1f5f9 25%, #e2e8f0 50%, #f1f5f9 75%);
      background-size: 200% 100%; border-radius: 6px; margin-bottom: 14px;
      animation: shimmer 1.4s infinite;
    }
    @keyframes shimmer { to { background-position: -200% 0 } }

    /* ── Botón Consultar ── */
    /* ── Wrapper posicionado alrededor del input ── */
    .ps-input-wrap {
      position: relative; display: inline-block; width: 100%;
    }
    .ps-input-wrap input {
      padding-right: 152px !important;
    }
    .ps-btn {
      position: absolute; right: 6px; top: 50%; transform: translateY(-50%);
      display: inline-flex; align-items: center; gap: 5px;
      padding: 5px 12px;
      background: var(--ps-blue-light); color: var(--ps-blue);
      border: 1.5px solid #bfdbfe; border-radius: 6px;
      font-family: 'Inter', sans-serif; font-size: 12px; font-weight: 500;
      cursor: pointer; transition: all .15s; white-space: nowrap;
      box-shadow: none; line-height: 1.4;
    }
    .ps-btn:hover {
      background: var(--ps-blue); color: #fff;
      border-color: var(--ps-blue);
    }
    .ps-btn svg { flex-shrink: 0; }

    /* ── Toast ── */
    #ps-toast {
      position: fixed; bottom: 28px; left: 50%; transform: translateX(-50%) translateY(10px);
      background: #1e293b; color: #fff;
      padding: 10px 20px; border-radius: 10px;
      font-family: 'Inter', sans-serif; font-size: 13px;
      display: flex; align-items: center; gap: 8px;
      opacity: 0; pointer-events: none; z-index: 99999;
      transition: opacity .25s, transform .25s;
      box-shadow: 0 4px 20px rgba(0,0,0,.2);
    }
    #ps-toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }
  </style>
</head>
<body>
<!-- Banner: no autenticado -->
<div id="ps-login-banner">
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
  <span>Esta API requiere autenticación. Haz login en <a onclick="scrollToLogin()">POST /auth/login</a> y luego pulsa <strong>Authorize</strong>.</span>
</div>
<!-- Banner: autenticado -->
<div id="ps-logged-banner">
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#16a34a" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>
  <span>Autenticado como <strong id="ps-logged-user">—</strong></span>
  <span id="ps-token-role">—</span>
  <button id="ps-logout-btn" onclick="psLogout()">Cerrar sesión</button>
</div>
<div id="swagger-ui"></div>

<!-- Modal -->
<div id="ps-overlay">
  <div id="ps-modal">
    <div id="ps-modal-header">
      <div id="ps-modal-icon">🏷️</div>
      <div id="ps-modal-title-wrap">
        <div id="ps-modal-title">Opciones disponibles</div>
        <div id="ps-modal-subtitle">Haz clic en un ID para copiarlo</div>
      </div>
      <button id="ps-modal-close" onclick="closeModal()" title="Cerrar">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M18 6 6 18M6 6l12 12"/></svg>
      </button>
    </div>
    <div id="ps-search-wrap">
      <input id="ps-search" type="text" placeholder="Buscar por nombre o ID…" oninput="filterTable(this.value)">
    </div>
    <div id="ps-modal-body">
      <div class="ps-skeleton">
        <div class="ps-skeleton-row" style="width:80%"></div>
        <div class="ps-skeleton-row" style="width:60%"></div>
        <div class="ps-skeleton-row" style="width:70%"></div>
      </div>
    </div>
    <div id="ps-modal-footer">
      <span id="ps-modal-count"></span>
      <span id="ps-modal-hint">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
        Clic en el ID para copiar
      </span>
    </div>
  </div>
</div>

<!-- Toast -->
<div id="ps-toast">
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#4ade80" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>
  <span id="ps-toast-text">ID copiado</span>
</div>

<script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script>
const ui = SwaggerUIBundle({
  url: "/openapi.json",
  dom_id: "#swagger-ui",
  presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset],
  layout: "BaseLayout",
  deepLinking: true,
  onComplete: () => setTimeout(injectButtons, 800),
});

function scrollToLogin() {
  const ops = document.querySelectorAll('.opblock-summary-path');
  for (const op of ops) {
    if (op.textContent.includes('/auth/login')) {
      op.closest('.opblock').scrollIntoView({behavior:'smooth'});
      op.closest('.opblock').querySelector('.opblock-summary').click();
      break;
    }
  }
}

function applyToken(token, user, role) {
  ui.preauthorizeApiKey ? ui.preauthorizeApiKey('BearerAuth', token) : null;
  // Inyectar en el Authorize de Swagger UI
  ui.authActions?.authorize({
    BearerAuth: { name:'BearerAuth', schema:{type:'http',scheme:'bearer'}, value: token }
  });
  window._psToken = token;  // guardado para los botones del modal
  document.getElementById('ps-login-banner').classList.add('hidden');
  document.getElementById('ps-logged-banner').classList.add('visible');
  document.getElementById('ps-logged-user').textContent = user;
  document.getElementById('ps-token-role').textContent  = role || '';
}

function psLogout() {
  window._psToken = null;
  document.getElementById('ps-login-banner').classList.remove('hidden');
  document.getElementById('ps-logged-banner').classList.remove('visible');
  ui.authActions?.logout(['BearerAuth']);
}

// Interceptar la respuesta de /auth/login para capturar el token automáticamente
const origFetch = window.fetch;
window.fetch = async function(...args) {
  const res = await origFetch(...args);
  try {
    const url = typeof args[0] === 'string' ? args[0] : args[0]?.url || '';
    if (url.includes('/auth/login') && res.ok) {
      const clone = res.clone();
      const data  = await clone.json();
      if (data.access_token) {
        applyToken(data.access_token, new URLSearchParams(url.split('?')[1]||'').get('username') || '?', data.role);
        showToast('Login correcto — token aplicado automáticamente');
      }
    }
  } catch(_) {}
  return res;
};

const BUTTONS = [
  {
    field: "id_category",
    label: "Ver categorías",
    icon: "🗂️",
    subtitle: "Categorías disponibles en PrestaShop",
    endpoint: "/categories",
    dataKey: "categorias",
    columns: [
      { key: "id",        header: "ID"       },
      { key: "nombre",    header: "Nombre"   },
      { key: "id_parent", header: "Padre"    },
    ],
  },
  {
    field: "id_supplier",
    label: "Ver proveedores",
    icon: "🏭",
    subtitle: "Proveedores disponibles en PrestaShop",
    endpoint: "/suppliers",
    dataKey: "proveedores",
    columns: [
      { key: "id",     header: "ID"     },
      { key: "nombre", header: "Nombre" },
    ],
  },
  {
    field: "parent_id",
    label: "Ver categorías",
    icon: "🗂️",
    subtitle: "Categorías disponibles en PrestaShop",
    endpoint: "/categories",
    dataKey: "categorias",
    columns: [
      { key: "id",        header: "ID"    },
      { key: "nombre",    header: "Nombre"},
      { key: "id_parent", header: "Padre" },
    ],
  },
  {
    field: "state_id",
    label: "Ver estados",
    icon: "📦",
    subtitle: "Estados de pedido disponibles",
    endpoint: "/orders/states",
    dataKey: "estados",
    columns: [
      { key: "id",     header: "ID"     },
      { key: "nombre", header: "Estado" },
    ],
  },
  {
    field: "tax_rule_group_id",
    label: "Ver grupos IVA",
    icon: "🧾",
    subtitle: "Grupos de reglas de impuesto",
    endpoint: "/taxes/rules",
    dataKey: "grupos",
    columns: [
      { key: "id",     header: "ID"     },
      { key: "nombre", header: "Nombre" },
    ],
  },
  {
    field: "feature_id",
    label: "Ver características",
    icon: "🏷️",
    subtitle: "Características disponibles",
    endpoint: "/features",
    dataKey: "caracteristicas",
    columns: [
      { key: "id",     header: "ID"     },
      { key: "nombre", header: "Nombre" },
    ],
  },
  {
    field: "feature_value_id",
    label: "Ver valores",
    icon: "📋",
    subtitle: "Consulta GET /features/{id}/values con el feature_id que necesites",
    endpoint: "/features",
    dataKey: "caracteristicas",
    columns: [
      { key: "id",     header: "ID característica" },
      { key: "nombre", header: "Nombre"             },
    ],
  },
  {
    field: "group_id",
    label: "Ver grupos",
    icon: "👥",
    subtitle: "Grupos de clientes disponibles",
    endpoint: "/customer-groups",
    dataKey: "grupos",
    columns: [
      { key: "id",         header: "ID"         },
      { key: "nombre",     header: "Nombre"     },
      { key: "descuento",  header: "Descuento %" },
    ],
  },
  {
    field: "id_country",
    label: "Ver países",
    icon: "🌍",
    subtitle: "Países disponibles para envío",
    endpoint: "/countries",
    dataKey: "paises",
    columns: [
      { key: "id",       header: "ID"       },
      { key: "nombre",   header: "País"     },
      { key: "iso_code", header: "ISO"      },
    ],
  },
];

let _allRows = [], _allCols = [];

function injectButtons() {
  const observer = new MutationObserver(doInject);
  observer.observe(document.getElementById("swagger-ui"), { childList:true, subtree:true });
  doInject();
}

function doInject() {
  BUTTONS.forEach(cfg => {
    document.querySelectorAll(".parameter__name").forEach(label => {
      const text = label.textContent.trim().replace(/[\\s]*[*].*/,"");
      if (text !== cfg.field) return;
      const row = label.closest("tr") || label.closest(".parameter-item") || label.parentElement;
      if (!row || row.dataset.psInjected) return;
      row.dataset.psInjected = "1";
      const input = row.querySelector("input");
      if (!input) return;
      // Envolvemos el input en un wrapper relativo para posicionar el botón dentro
      const parent = input.parentElement;
      const wrap = document.createElement("div");
      wrap.className = "ps-input-wrap";
      parent.insertBefore(wrap, input);
      wrap.appendChild(input);
      const btn = document.createElement("button");
      btn.className = "ps-btn";
      btn.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>${cfg.label}`;
      btn.onclick = e => { e.preventDefault(); openModal(cfg); };
      wrap.appendChild(btn);
    });
  });
}

async function openModal(cfg) {
  document.getElementById("ps-modal-icon").textContent   = cfg.icon;
  document.getElementById("ps-modal-title").textContent  = cfg.subtitle;
  document.getElementById("ps-modal-count").textContent  = "";
  document.getElementById("ps-search").value             = "";
  document.getElementById("ps-modal-body").innerHTML     =
    `<div class="ps-skeleton">
      <div class="ps-skeleton-row" style="width:75%"></div>
      <div class="ps-skeleton-row" style="width:55%"></div>
      <div class="ps-skeleton-row" style="width:65%"></div>
    </div>`;
  document.getElementById("ps-overlay").classList.add("open");

  try {
    // Incluir el token JWT si está disponible
    const token = (window._psToken || "");
    const headers = token ? { "Authorization": "Bearer " + token } : {};
    const res  = await fetch(cfg.endpoint, { headers });
    if (res.status === 401) {
      document.getElementById("ps-modal-body").innerHTML =
        "<p style='padding:24px;color:#f59e0b;font-family:Inter,sans-serif'>⚠️ Necesitas autenticarte primero. Haz login en <b>POST /auth/login</b> y pulsa <b>Authorize</b>.</p>";
      return;
    }
    const json = await res.json();
    _allRows = json[cfg.dataKey] || [];
    _allCols = cfg.columns;
    renderTable(_allRows, _allCols);
    document.getElementById("ps-modal-count").textContent = `${_allRows.length} registros`;
  } catch(e) {
    document.getElementById("ps-modal-body").innerHTML =
      "<p style='padding:24px;color:#ef4444;font-family:Inter,sans-serif'>Error al cargar los datos.</p>";
  }
}

function filterTable(q) {
  const filtered = q
    ? _allRows.filter(r => Object.values(r).some(v => String(v).toLowerCase().includes(q.toLowerCase())))
    : _allRows;
  renderTable(filtered, _allCols);
  document.getElementById("ps-modal-count").textContent =
    filtered.length === _allRows.length
      ? `${_allRows.length} registros`
      : `${filtered.length} de ${_allRows.length}`;
}

function renderTable(rows, cols) {
  if (!rows.length) {
    document.getElementById("ps-modal-body").innerHTML =
      "<p style='padding:24px;color:#94a3b8;font-family:Inter,sans-serif;text-align:center'>Sin resultados</p>";
    return;
  }
  let h = "<table><thead><tr>";
  cols.forEach(c => { h += `<th>${c.header}</th>`; });
  h += "</tr></thead><tbody>";
  rows.forEach(r => {
    h += `<tr onclick="copyId('${r[cols[0].key]}', this)">`;
    cols.forEach((c,i) => {
      if (i === 0) {
        h += `<td><span class="ps-id-chip">
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
          ${r[c.key]}</span></td>`;
      } else {
        h += `<td>${r[c.key] ?? "—"}</td>`;
      }
    });
    h += "</tr>";
  });
  h += "</tbody></table>";
  document.getElementById("ps-modal-body").innerHTML = h;
}

function closeModal() {
  document.getElementById("ps-overlay").classList.remove("open");
}
document.getElementById("ps-overlay").addEventListener("click", e => {
  if (e.target === document.getElementById("ps-overlay")) closeModal();
});

function copyId(id, row) {
  navigator.clipboard.writeText(String(id)).then(() => {
    // Feedback visual en la fila
    const chip = row.querySelector(".ps-id-chip");
    if (chip) {
      chip.classList.add("copied");
      chip.innerHTML = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg> ${id}`;
      setTimeout(() => {
        chip.classList.remove("copied");
        chip.innerHTML = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg> ${id}`;
      }, 1800);
    }
    // Toast
    document.getElementById("ps-toast-text").textContent = `ID ${id} copiado al portapapeles`;
    const t = document.getElementById("ps-toast");
    t.classList.add("show");
    setTimeout(() => t.classList.remove("show"), 2400);
  });
}
</script>
</body>
</html>""")

# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def _decimal_safe(productos: list) -> list:
    """Convierte campos Decimal a float para serialización JSON."""
    for p in productos:
        if p.get("precio") is not None:
            p["precio"] = float(p["precio"])
    return productos



# ══════════════════════════════════════════════════════════════
# TAG: Autenticación
# ══════════════════════════════════════════════════════════════

@app.post("/auth/login", summary="Iniciar sesión y obtener token JWT", tags=["Autenticación"])
async def login(
    username: str = Query(..., description="Nombre de usuario"),
    password: str = Query(..., description="Contraseña"),
) -> JSONResponse:
    """
    Autentica al usuario y devuelve un **access_token** y un **refresh_token**.

    1. Copia el `access_token` de la respuesta
    2. Pulsa el botón **Authorize** arriba a la derecha del Swagger
    3. Introduce `Bearer <access_token>` en el campo
    4. Ya puedes ejecutar todos los endpoints protegidos

    El access_token expira según `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` del .env.
    """
    import asyncio as _asyncio
    loop = _asyncio.get_running_loop()
    try:
        user = await loop.run_in_executor(None, user_manager.authenticate, username, password)
    except Exception as exc:
        logger.error("Login error inesperado: {e}", e=exc)
        raise HTTPException(status_code=503, detail="Servicio temporalmente no disponible.")
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuario o contraseña incorrectos.",
        )
    access_token  = create_access_token(user["username"], user["role"])
    refresh_token = create_refresh_token(user["username"])
    return JSONResponse({
        "access_token":  access_token,
        "refresh_token": refresh_token,
        "token_type":    "bearer",
        "role":          user["role"],
        "expires_in":    settings.jwt_access_token_expire_minutes * 60,
    })


@app.post("/auth/refresh", summary="Renovar access token con refresh token", tags=["Autenticación"])
async def refresh_token(
    refresh_token: str = Query(..., description="Refresh token obtenido en /auth/login"),
) -> JSONResponse:
    """
    Genera un nuevo access_token usando el refresh_token.
    No requiere introducir usuario y contraseña de nuevo.
    """
    try:
        payload = decode_token(refresh_token)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token inválido o expirado. Haz login de nuevo.",
        )
    if payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido. Debes usar el refresh_token.",
        )
    user = user_manager.get_user(payload["sub"])
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuario no encontrado o desactivado.",
        )
    new_access = create_access_token(user["username"], user["role"])
    return JSONResponse({
        "access_token": new_access,
        "token_type":   "bearer",
        "expires_in":   settings.jwt_access_token_expire_minutes * 60,
    })


@app.post("/auth/change-password", summary="Cambiar contraseña", tags=["Autenticación"])
async def change_password(
    current_password: str = Query(..., description="Contraseña actual"),
    new_password:     str = Query(..., description="Nueva contraseña (mín. 8 caracteres)"),
    user = Depends(require_auth),
) -> JSONResponse:
    """Cambia la contraseña del usuario autenticado."""
    if len(new_password) < 8:
        raise HTTPException(status_code=400, detail="La nueva contraseña debe tener al menos 8 caracteres.")
    # Verificar contraseña actual
    authenticated = user_manager.authenticate(user["sub"], current_password)
    if not authenticated:
        raise HTTPException(status_code=401, detail="La contraseña actual es incorrecta.")
    success = user_manager.change_password(user["sub"], new_password)
    if not success:
        raise HTTPException(status_code=502, detail="No se pudo cambiar la contraseña.")
    return JSONResponse({"status": "ok", "message": "Contraseña cambiada correctamente."})


# ══════════════════════════════════════════════════════════════
# TAG: Usuarios (solo admin)
# ══════════════════════════════════════════════════════════════

@app.get("/users", summary="Listar usuarios de la API", tags=["Usuarios"])
async def list_users(user=Depends(require_admin)) -> JSONResponse:
    """Lista todos los usuarios registrados. Solo accesible por admins."""
    usuarios = user_manager.list_users()
    # Convertir datetimes a string para JSON
    for u in usuarios:
        for campo in ("created_at", "last_login"):
            if u.get(campo) and hasattr(u[campo], "isoformat"):
                u[campo] = u[campo].isoformat()
    return JSONResponse({"total": len(usuarios), "usuarios": usuarios})


@app.post("/users", summary="Crear nuevo usuario de la API", tags=["Usuarios"])
async def create_user(
    username: str = Query(..., description="Nombre de usuario (único)"),
    password: str = Query(..., description="Contraseña (mín. 8 caracteres)"),
    role:     str = Query(default="readonly", description="Rol: 'admin' o 'readonly'"),
) -> JSONResponse:
    """Crea un nuevo usuario. Solo accesible por admins."""
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="La contraseña debe tener al menos 8 caracteres.")
    if role not in ("admin", "readonly"):
        raise HTTPException(status_code=400, detail="El rol debe ser 'admin' o 'readonly'.")
    success = user_manager.create_user(username, password, role)
    if not success:
        raise HTTPException(status_code=409, detail=f"El usuario '{username}' ya existe.")
    return JSONResponse({"status": "created", "username": username, "role": role})


@app.put("/users/{username}", summary="Modificar usuario de la API", tags=["Usuarios"])
async def update_user(
    request:  Request,
    username: str  = Path(..., description="Nombre o ID numérico del usuario a modificar"),
    new_username: str  = Query(default=None, description="Nuevo nombre de usuario"),
    role:     str  = Query(default=None, description="Nuevo rol: 'admin' o 'readonly'"),
    active:   bool = Query(default=None, description="Activar (true) o desactivar (false) el usuario"),
) -> JSONResponse:
    """
    Modifica los datos de un usuario existente.
    Puedes cambiar el nombre, el rol o el estado activo/inactivo.
    Solo accesible por admins.
    """
    if role is not None and role not in ("admin", "readonly"):
        raise HTTPException(status_code=400, detail="El rol debe ser 'admin' o 'readonly'.")

    updated = []
    conn = None
    try:
        conn = user_manager._conn()
        cursor = conn.cursor()

        # Determinar si se busca por ID numérico o por nombre
        if username.isdigit():
            where_col = "id"
            where_val = int(username)
        else:
            where_col = "username"
            where_val = username

        if new_username is not None:
            cursor.execute(
                f"UPDATE api_users SET username = %s WHERE {where_col} = %s",
                (new_username, where_val),
            )
            if cursor.rowcount:
                updated.append(f"username → {new_username}")

        if role is not None:
            cursor.execute(
                f"UPDATE api_users SET role = %s WHERE {where_col} = %s",
                (role, where_val),
            )
            if cursor.rowcount:
                updated.append(f"role → {role}")

        if active is not None:
            cursor.execute(
                f"UPDATE api_users SET active = %s WHERE {where_col} = %s",
                (1 if active else 0, where_val),
            )
            if cursor.rowcount:
                updated.append(f"active → {active}")

        conn.commit()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Error actualizando usuario: {exc}")
    finally:
        if conn and conn.is_connected():
            conn.close()

    if not updated:
        raise HTTPException(status_code=404, detail=f"Usuario '{username}' no encontrado o sin cambios.")

    return JSONResponse({
        "status":   "updated",
        "username": new_username or username,
        "cambios":  updated,
    })


@app.delete("/users/{username}", summary="Desactivar usuario de la API", tags=["Usuarios"])
async def deactivate_user(
    request: Request,
    username: str = Path(..., description="Nombre del usuario a desactivar"),
) -> JSONResponse:
    """
    Desactiva un usuario (no lo elimina de la BD).
    El usuario desactivado no podrá hacer login pero sus datos se conservan.
    """
    current_user = getattr(request.state, "user", {})
    if username == current_user.get("sub"):
        raise HTTPException(status_code=400, detail="No puedes desactivar tu propio usuario.")
    success = user_manager.deactivate_user(username)
    if not success:
        raise HTTPException(status_code=404, detail=f"Usuario '{username}' no encontrado.")
    return JSONResponse({"status": "deactivated", "username": username})


# ══════════════════════════════════════════════════════════════
# TAG: General
# ══════════════════════════════════════════════════════════════

@app.get("/", summary="Estado del microservicio", tags=["General"])
async def root() -> JSONResponse:
    """Comprueba que el microservicio está activo."""
    return JSONResponse({"status": "ok", "message": "Microservicio PrestaShop activo y funcionando."})


@app.get("/health", summary="Health check detallado", tags=["General"])
async def health_check() -> JSONResponse:
    """
    Verifica la conectividad con MySQL y con el webservice de PrestaShop.
    Incluye el estado de los circuit breakers para diagnóstico.
    """
    db_ok  = db_handler.health_check()
    ps_res = ps_client._request("GET", "")
    ps_ok  = ps_res is not None

    ps_cb  = prestashop_circuit.status()
    db_cb  = mysql_circuit.status()
    status = "ok" if (db_ok and ps_ok) else "degraded"

    return JSONResponse({
        "status":     status,
        "database":   "ok" if db_ok else "error",
        "prestashop": "ok" if ps_ok else "error",
        "circuit_breakers": {
            "prestashop": ps_cb,
            "mysql":      db_cb,
        },
    })


# ══════════════════════════════════════════════════════════════
# TAG: Catálogo (sincronización)
# ══════════════════════════════════════════════════════════════

@app.post("/catalog/sync", summary="Sincronización completa — asíncrona", tags=["Catálogo"])
async def full_sync(background_tasks: BackgroundTasks
) -> JSONResponse:
    """
    Lanza la sincronización completa del catálogo en segundo plano.
    Respuesta inmediata. Consulta los logs del servidor para ver el progreso.
    """
    background_tasks.add_task(catalog_service.sincronizar_todo)
    logger.info("Sincronización completa lanzada en segundo plano.")
    return JSONResponse({
        "status": "accepted",
        "message": "Sincronización iniciada. Consulta los logs para ver el progreso.",
    })


@app.post("/catalog/sync/now", summary="Sincronización completa — síncrona", tags=["Catálogo"])
async def full_sync_now(
) -> JSONResponse:
    """
    Ejecuta la sincronización completa de forma síncrona y devuelve el resultado.
    ⚠️ Puede tardar varios minutos con catálogos grandes.
    """
    resultado: SyncResult = catalog_service.sincronizar_todo()
    return JSONResponse({
        "status": "ok" if resultado.exitoso else "partial_error",
        "resultado": resultado.to_dict(),
    })


@app.post(
    "/catalog/sync/product/{product_id}",
    summary="Sincronizar un producto concreto por ID local",
    tags=["Catálogo"],
)
async def sync_single_product(
    product_id: int = Path(..., ge=1, description="ID local del producto en la BD"),
) -> JSONResponse:
    """Crea en PS si no existe, actualiza stock y sube imagen del producto indicado."""
    resultado: SyncResult = catalog_service.sincronizar_producto_unico(product_id)
    if not resultado.total:
        raise HTTPException(status_code=404, detail=f"Producto ID local {product_id} no encontrado.")
    return JSONResponse({
        "status": "ok" if resultado.exitoso else "partial_error",
        "resultado": resultado.to_dict(),
    })


# ══════════════════════════════════════════════════════════════
# TAG: Productos
# ══════════════════════════════════════════════════════════════

@app.get("/products", summary="Listar productos de la BD local", tags=["Productos"])
async def list_products(
    limit: int = Query(default=50, ge=1, le=500, description="Máximo de productos a devolver"),
) -> JSONResponse:
    """Devuelve los productos activos de la BD local con su estado de vinculación con PS."""
    productos = _decimal_safe(db_handler.obtener_datos_completos())
    data = productos[:limit]
    return JSONResponse({"total": len(productos), "devueltos": len(data), "productos": data})


@app.get(
    "/products/reference/{reference}",
    summary="Buscar producto por referencia en PrestaShop",
    tags=["Productos"],
)
async def get_product_by_reference(
    reference: str = Path(..., description="Referencia del producto (ej: demo_1)"),
) -> JSONResponse:
    """
    Busca un producto en PrestaShop por su referencia.
    También devuelve los datos locales si existe en la BD.
    """
    ps_data  = ps_client.get_product_by_reference(reference)
    db_data  = db_handler.obtener_producto_por_referencia(reference)

    if not ps_data and not db_data:
        raise HTTPException(status_code=404, detail=f"Producto con referencia '{reference}' no encontrado.")

    if db_data and db_data.get("precio") is not None:
        db_data["precio"] = float(db_data["precio"])

    return JSONResponse({
        "prestashop": ps_data,
        "local": db_data,
    })



@app.post("/products", summary="Crear producto en PrestaShop", tags=["Productos"])
async def create_product(
    name:               str   = Query(...,         description="Nombre del producto"),
    price:              float = Query(...,  ge=0,  description="Precio sin IVA"),
    reference:          str   = Query(...,         description="Referencia única del producto"),
    id_category:        int   = Query(default=2,  ge=1, description="ID de categoría en PS — usa el botón Ver categorías"),
    id_supplier:        int   = Query(default=0,  ge=0, description="ID de proveedor en PS — usa el botón Ver proveedores (0 = ninguno)"),
    active:             bool  = Query(default=True,      description="Producto activo (visible en tienda)"),
    stock:              int   = Query(default=0,  ge=0,  description="Stock inicial"),
    description:        str   = Query(default=None,      description="Descripción larga (admite HTML)"),
    description_short:  str   = Query(default=None,      description="Descripción corta (admite HTML)"),
    weight:             float = Query(default=None, ge=0, description="Peso en kg"),
    ean13:              str   = Query(default=None,      description="Código EAN-13 / código de barras"),
    minimal_quantity:   int   = Query(default=1,  ge=1,  description="Cantidad mínima de pedido"),
) -> JSONResponse:
    """
    Crea un producto en PrestaShop con todos los campos disponibles.

    Usa los botones **Ver categorías** y **Ver proveedores** para consultar los IDs.
    El stock inicial se aplica automáticamente tras la creación.
    """
    data = {
        "name":             name,
        "price":            price,
        "reference":        reference,
        "id_category":      id_category,
        "id_supplier":      id_supplier,
        "active":           active,
        "stock":            stock,
        "minimal_quantity": minimal_quantity,
    }
    if description:       data["description"]       = description
    if description_short: data["description_short"] = description_short
    if weight is not None: data["weight"]           = weight
    if ean13:              data["ean13"]             = ean13

    new_id = ps_client.create_product(data)
    if not new_id:
        raise HTTPException(status_code=502, detail="No se pudo crear el producto en PrestaShop.")
    return JSONResponse({"status": "created", "prestashop_id": new_id, "nombre": name})


@app.put("/products/{product_id}", summary="Editar producto en PrestaShop", tags=["Productos"])
async def update_product(
    product_id:         int   = Path(..., ge=1,   description="ID del producto en PrestaShop"),
    name:               str   = Query(default=None, description="Nuevo nombre"),
    price:              float = Query(default=None, ge=0, description="Nuevo precio sin IVA"),
    reference:          str   = Query(default=None, description="Nueva referencia"),
    active:             bool  = Query(default=None, description="true = activo, false = inactivo"),
    id_category:        int   = Query(default=None, ge=1, description="Nueva categoría — usa el botón Ver categorías"),
    id_supplier:        int   = Query(default=None, ge=0, description="Nuevo proveedor — usa el botón Ver proveedores"),
    stock:              int   = Query(default=None, ge=0, description="Nuevo stock"),
    weight:             float = Query(default=None, ge=0, description="Nuevo peso en kg"),
    ean13:              str   = Query(default=None, description="Nuevo código EAN-13"),
    minimal_quantity:   int   = Query(default=None, ge=1, description="Nueva cantidad mínima de pedido"),
    description:        str   = Query(default=None, description="Nueva descripción larga (admite HTML)"),
    description_short:  str   = Query(default=None, description="Nueva descripción corta (admite HTML)"),
) -> JSONResponse:
    """
    Actualiza uno o varios campos de un producto existente.
    Solo se modifican los campos que se incluyan — el resto permanece igual.
    """
    data = {}
    if name              is not None: data["name"]               = name
    if price             is not None: data["price"]              = price
    if reference         is not None: data["reference"]          = reference
    if active            is not None: data["active"]             = active
    if id_category       is not None: data["id_category_default"]= id_category
    if id_supplier       is not None: data["id_supplier"]        = id_supplier
    if stock             is not None: data["stock"]              = stock
    if weight            is not None: data["weight"]             = weight
    if ean13             is not None: data["ean13"]              = ean13
    if minimal_quantity  is not None: data["minimal_quantity"]   = minimal_quantity
    if description       is not None: data["description"]        = description
    if description_short is not None: data["description_short"]  = description_short

    if not data:
        raise HTTPException(status_code=400, detail="Debes indicar al menos un campo a actualizar.")

    success = ps_client.update_product(str(product_id), data)
    if not success:
        raise HTTPException(status_code=502, detail=f"No se pudo actualizar el producto ID {product_id}.")
    return JSONResponse({"status": "updated", "product_id": product_id, "campos": list(data.keys())})


@app.get(
    "/products/{product_id}/description",
    summary="Ver descripción de un producto",
    tags=["Descripción"],
)
async def get_product_description(
    product_id: int = Path(..., ge=1, description="ID del producto en PrestaShop"),
) -> JSONResponse:
    """Devuelve la descripción HTML actual de un producto."""
    description = ps_client.get_product_description(str(product_id))
    if description is None:
        raise HTTPException(status_code=404, detail=f"Producto PS ID {product_id} no encontrado.")
    return JSONResponse({
        "product_id": product_id,
        "description": description,
    })


@app.put(
    "/products/{product_id}/description",
    summary="Actualizar descripción de un producto",
    tags=["Descripción"],
)
async def update_product_description(
    product_id:  int = Path(..., ge=1, description="ID del producto en PrestaShop"),
    description: str = Query(..., description="Nueva descripción (admite HTML básico: <b>, <ul>, <li>, <p>)"),
) -> JSONResponse:
    """
    Actualiza la descripción de un producto existente en PrestaShop.

    La descripción admite HTML básico para dar formato al texto:
    - `<p>Párrafo</p>` — párrafos
    - `<b>Negrita</b>` — texto en negrita
    - `<ul><li>Item</li></ul>` — listas
    - `<br>` — salto de línea
    """
    success = ps_client.update_description(str(product_id), description)
    if not success:
        raise HTTPException(
            status_code=502,
            detail=f"No se pudo actualizar la descripción del producto PS ID {product_id}.",
        )
    return JSONResponse({
        "status": "updated",
        "product_id": product_id,
    })


@app.delete("/products/{product_id}", summary="Eliminar producto de PrestaShop", tags=["Productos"])
async def delete_product(
    product_id: int = Path(..., ge=1, description="ID del producto en PrestaShop"),
) -> JSONResponse:
    """
    Elimina un producto de PrestaShop por su ID.
    ⚠️ Esta acción es irreversible. No elimina el producto de la BD local.
    """
    success = ps_client.delete_product(str(product_id))
    if not success:
        raise HTTPException(status_code=502, detail=f"No se pudo eliminar el producto PS ID {product_id}.")
    return JSONResponse({"status": "deleted", "product_id": product_id})


# ══════════════════════════════════════════════════════════════
# TAG: Stock
# ══════════════════════════════════════════════════════════════

# IMPORTANTE: /stock/low debe declararse ANTES de /stock/{product_id}
# para que FastAPI no capture "low" como un product_id entero.
@app.get(
    "/stock/low",
    summary="Productos con stock bajo",
    tags=["Stock"],
)
async def get_low_stock(
    threshold: int = Query(default=5, ge=0, le=1000, description="Umbral — productos con stock ≤ a este valor. Por defecto: 5"),
) -> JSONResponse:
    """
    Devuelve los productos con stock igual o por debajo del umbral indicado.

    - `threshold=0` → solo productos **sin stock**
    - `threshold=5` → productos con 0, 1, 2, 3, 4 o 5 unidades
    - `threshold=10` → productos que necesitan revisión de reposición

    Cada producto incluye su `alerta`: `sin_stock` o `stock_bajo`.
    Ordenados de menor a mayor stock.
    """
    productos = ps_client.get_low_stock_products(threshold=threshold)
    sin_stock  = sum(1 for p in productos if p["alerta"] == "sin_stock")
    stock_bajo = sum(1 for p in productos if p["alerta"] == "stock_bajo")
    return JSONResponse({
        "umbral":     threshold,
        "total":      len(productos),
        "sin_stock":  sin_stock,
        "stock_bajo": stock_bajo,
        "productos":  productos,
    })


@app.get(
    "/stock/{product_id}",
    summary="Consultar stock de un producto en PrestaShop",
    tags=["Stock"],
)
async def get_stock(
    product_id: int = Path(..., ge=1, description="ID del producto en PrestaShop"),
) -> JSONResponse:
    """Devuelve el stock disponible de un producto, incluyendo el nombre del producto."""
    stock = ps_client.get_stock(str(product_id))
    if not stock:
        raise HTTPException(status_code=404, detail=f"No se encontró stock para el producto PS ID {product_id}.")
    # Enriquecer con nombre del producto
    try:
        prod = ps_client._request("GET", f"products/{product_id}?display=[id,name]")
        if prod is not None:
            name_node = prod.find(".//name/language")
            if name_node is not None:
                stock["nombre_producto"] = name_node.text
    except Exception:
        pass
    return JSONResponse(stock)


@app.put(
    "/stock/{product_id}/{qty}",
    summary="Actualizar stock de un producto en PrestaShop",
    tags=["Stock"],
)
async def update_stock(
    product_id: int = Path(..., ge=1, description="ID del producto en PrestaShop"),
    qty: int        = Path(..., ge=0, description="Nueva cantidad de stock (≥0)"),
) -> JSONResponse:
    """Actualiza el stock disponible de un producto en PrestaShop."""
    success = ps_client.update_stock(str(product_id), qty)
    if not success:
        raise HTTPException(status_code=502, detail=f"No se pudo actualizar el stock del producto PS ID {product_id}.")
    return JSONResponse({"status": "updated", "product_id": product_id, "quantity": qty})


# ══════════════════════════════════════════════════════════════
# TAG: Categorías
# ══════════════════════════════════════════════════════════════

@app.get("/categories", summary="Listar categorías de PrestaShop", tags=["Categorías"])
async def get_categories(
) -> JSONResponse:
    """Devuelve todas las categorías existentes en PrestaShop con su ID, nombre y padre."""
    categorias = ps_client.get_categories()
    return JSONResponse({"total": len(categorias), "categorias": categorias})


@app.post("/categories", summary="Crear categoría en PrestaShop", tags=["Categorías"])
async def create_category(
    name:      str  = Query(...,          description="Nombre de la nueva categoría"),
    parent_id: int  = Query(default=2, ge=1, description="ID de la categoría padre (2 = raíz) — usa el botón Ver categorías"),
    active:    bool = Query(default=True, description="Categoría activa y visible en la tienda"),
) -> JSONResponse:
    """Crea una nueva categoría en PrestaShop bajo el padre indicado."""
    new_id = ps_client.create_category(name, str(parent_id), active=active)
    if not new_id:
        raise HTTPException(status_code=502, detail=f"No se pudo crear la categoría '{name}'.")
    return JSONResponse({"status": "created", "category_id": new_id, "nombre": name, "parent_id": parent_id, "active": active})


@app.delete("/categories/{category_id}", summary="Eliminar categoría de PrestaShop", tags=["Categorías"])
async def delete_category(
    category_id: int = Path(..., ge=1, description="ID de la categoría a eliminar"),
) -> JSONResponse:
    """
    Elimina una categoría de PrestaShop.
    ⚠️ No elimines categorías raíz del sistema (IDs 1 y 2).
    """
    if category_id in (1, 2):
        raise HTTPException(status_code=400, detail="No se pueden eliminar las categorías raíz del sistema (ID 1 y 2).")
    success = ps_client.delete_category(str(category_id))
    if not success:
        raise HTTPException(status_code=502, detail=f"No se pudo eliminar la categoría ID {category_id}.")
    return JSONResponse({"status": "deleted", "category_id": category_id})


# ══════════════════════════════════════════════════════════════
# TAG: Proveedores
# ══════════════════════════════════════════════════════════════

@app.get("/suppliers", summary="Listar proveedores de PrestaShop", tags=["Proveedores"])
async def get_suppliers(
) -> JSONResponse:
    """Devuelve todos los proveedores existentes en PrestaShop."""
    proveedores = ps_client.get_suppliers()
    return JSONResponse({"total": len(proveedores), "proveedores": proveedores})


@app.post("/suppliers", summary="Crear proveedor en PrestaShop", tags=["Proveedores"])
async def create_supplier(
    name: str = Query(..., description="Nombre del nuevo proveedor"),
) -> JSONResponse:
    """Crea un nuevo proveedor en PrestaShop."""
    new_id = ps_client.create_supplier(name)
    if not new_id:
        raise HTTPException(status_code=502, detail=f"No se pudo crear el proveedor '{name}'.")
    return JSONResponse({"status": "created", "supplier_id": new_id, "nombre": name})


@app.delete("/suppliers/{supplier_id}", summary="Eliminar proveedor de PrestaShop", tags=["Proveedores"])
async def delete_supplier(
    supplier_id: int = Path(..., ge=1, description="ID del proveedor a eliminar"),
) -> JSONResponse:
    """Elimina un proveedor de PrestaShop por su ID."""
    success = ps_client.delete_supplier(str(supplier_id))
    if not success:
        raise HTTPException(status_code=502, detail=f"No se pudo eliminar el proveedor ID {supplier_id}.")
    return JSONResponse({"status": "deleted", "supplier_id": supplier_id})


# ══════════════════════════════════════════════════════════════
# TAG: Imágenes
# ══════════════════════════════════════════════════════════════

@app.get(
    "/images/{product_id}",
    summary="Ver imágenes de un producto",
    tags=["Imágenes"],
)
async def get_product_images(
    product_id: int = Path(..., ge=1, description="ID del producto en PrestaShop"),
) -> JSONResponse:
    """Devuelve la lista de imágenes asociadas a un producto con sus IDs y URLs."""
    imagenes = ps_client.get_product_images(str(product_id))
    return JSONResponse({
        "product_id": product_id,
        "total": len(imagenes),
        "imagenes": imagenes,
    })


@app.post(
    "/images/{product_id}",
    summary="Subir una o varias imágenes a un producto",
    tags=["Imágenes"],
)
async def upload_images(
    product_id: int         = Path(..., ge=1, description="ID del producto en PrestaShop"),
    files: List[UploadFile] = File(..., description="Imágenes a subir (JPEG, PNG, WEBP). Puedes seleccionar varios archivos."),
    max_width:  int         = Query(default=0, ge=0, le=4000, description="Ancho máximo en píxeles (0 = auto: usa el tamaño de las imágenes existentes, o 1200px si no hay ninguna)"),
    max_height: int         = Query(default=0, ge=0, le=4000, description="Alto máximo en píxeles (0 = auto: usa el tamaño de las imágenes existentes, o 1200px si no hay ninguna)"),
    quality:    int         = Query(default=85, ge=10, le=100, description="Calidad JPEG 10-100 (85 = óptimo calidad/peso)"),
) -> JSONResponse:
    """
    Sube una o varias imágenes al producto en PrestaShop.

    **Procesamiento automático antes de subir:**
    - Convierte a JPEG (PrestaShop solo acepta JPEG)
    - Corrige orientación EXIF (fotos tomadas con el móvil)
    - Si `max_width`/`max_height` son 0 (por defecto), detecta automáticamente
      las dimensiones de las imágenes existentes del producto y las usa.
      Si no hay imágenes previas, usa 1200×1200px.
    - Optimiza el peso según la `quality` indicada

    Selecciona varios archivos a la vez para subirlos todos de una.
    """
    from core.image_processor import process_image, validate_image
    from PIL import Image as _Image
    import io as _io
    import requests as _req

    from core.image_processor import validate_image
    from PIL import Image as _Image
    import io as _io

    resultados = []
    errores    = []
    w, h       = max_width, max_height  # 0 = auto-detectar desde ps_client

    for file in files:
        try:
            raw_bytes = await file.read()

            # Validar que es una imagen real
            valid, error_msg = validate_image(raw_bytes)
            if not valid:
                errores.append({"filename": file.filename, "error": error_msg})
                continue

            # Info original
            orig_img  = _Image.open(_io.BytesIO(raw_bytes))
            orig_info = {
                "width":   orig_img.width,
                "height":  orig_img.height,
                "formato": orig_img.format or "?",
                "kb":      round(len(raw_bytes) / 1024, 1),
            }

            # upload_image_bytes detecta las dimensiones existentes si w==0 o h==0
            success = ps_client.upload_image_bytes(
                str(product_id), raw_bytes, file.filename,
                max_width=w, max_height=h,
            )

            if success:
                # Obtener info de la imagen ya procesada
                imagenes = ps_client.get_product_images(str(product_id))
                resultados.append({
                    "filename": file.filename,
                    "status":   "uploaded",
                    "original": orig_info,
                    "imagen_ps_id": imagenes[-1]["id"] if imagenes else None,
                })
            else:
                errores.append({
                    "filename": file.filename,
                    "error":    "Error al subir al webservice de PS",
                })

        except Exception as exc:
            errores.append({"filename": file.filename, "error": str(exc)})

    if not resultados and errores:
        raise HTTPException(
            status_code=502,
            detail={"mensaje": "No se pudo subir ninguna imagen.", "errores": errores},
        )

    return JSONResponse({
        "product_id":      product_id,
        "subidas":         len(resultados),
        "errores":         len(errores),
        "resultados":      resultados,
        "errores_detalle": errores,
    })


@app.delete(
    "/images/{product_id}/{image_id}",
    summary="Eliminar una imagen de un producto",
    tags=["Imágenes"],
)
async def delete_product_image(
    product_id: int = Path(..., ge=1, description="ID del producto en PrestaShop"),
    image_id:   int = Path(..., ge=1, description="ID de la imagen a eliminar — consulta GET /images/{product_id}"),
) -> JSONResponse:
    """
    Elimina una imagen concreta de un producto.
    Usa `GET /images/{product_id}` para ver los IDs de las imágenes disponibles.
    """
    success = ps_client.delete_product_image(str(product_id), str(image_id))
    if not success:
        raise HTTPException(
            status_code=502,
            detail=f"No se pudo eliminar la imagen ID {image_id} del producto PS ID {product_id}.",
        )
    return JSONResponse({
        "status":     "deleted",
        "product_id": product_id,
        "image_id":   image_id,
    })



# ══════════════════════════════════════════════════════════════
# TAG: Pedidos
# ══════════════════════════════════════════════════════════════

@app.get("/orders", summary="Listar pedidos de la tienda", tags=["Pedidos"])
async def get_orders(
    limit:  int = Query(default=50, ge=1, le=200, description="Número máximo de pedidos"),
    offset: int = Query(default=0,  ge=0,         description="Desplazamiento para paginación"),
) -> JSONResponse:
    """
    Devuelve los pedidos ordenados del más reciente al más antiguo.
    Incluye referencia, cliente, total, estado y fecha.
    """
    pedidos = ps_client.get_orders(limit=limit, offset=offset)
    return JSONResponse({"total": len(pedidos), "pedidos": pedidos})


@app.get("/orders/states", summary="Listar estados de pedido disponibles", tags=["Pedidos"])
async def get_order_states(
) -> JSONResponse:
    """
    Devuelve todos los estados de pedido configurados en la tienda.
    Consulta este endpoint para saber qué ID usar en PUT /orders/{id}/state.
    """
    estados = ps_client.get_order_states()
    return JSONResponse({
        "uso": "Usa el 'id' del estado en PUT /orders/{order_id}/state/{state_id}",
        "total": len(estados),
        "estados": estados,
    })


@app.get("/orders/{order_id}", summary="Ver detalle de un pedido", tags=["Pedidos"])
async def get_order(
    order_id: int = Path(..., ge=1, description="ID del pedido en PrestaShop"),
) -> JSONResponse:
    """
    Devuelve el detalle completo de un pedido: cliente, líneas de producto,
    totales, método de pago, estado y fechas.
    """
    pedido = ps_client.get_order(str(order_id))
    if not pedido:
        raise HTTPException(status_code=404, detail=f"Pedido ID {order_id} no encontrado.")
    return JSONResponse(pedido)


@app.put(
    "/orders/{order_id}/state/{state_id}",
    summary="Cambiar estado de un pedido",
    tags=["Pedidos"],
)
async def update_order_state(
    order_id: int = Path(..., ge=1, description="ID del pedido en PrestaShop"),
    state_id: int = Path(..., ge=1, description="ID del nuevo estado — consulta GET /orders/states"),
) -> JSONResponse:
    """
    Cambia el estado de un pedido y registra el cambio en el historial.

    **Antes de ejecutar**, consulta `GET /orders/states` para ver los IDs disponibles.
    Estados habituales: 2 = pago aceptado, 3 = en preparación, 4 = enviado, 5 = entregado.
    """
    success = ps_client.update_order_state(str(order_id), str(state_id))
    if not success:
        raise HTTPException(
            status_code=502,
            detail=f"No se pudo cambiar el estado del pedido ID {order_id}.",
        )
    return JSONResponse({
        "status": "updated",
        "order_id": order_id,
        "new_state_id": state_id,
    })


# ══════════════════════════════════════════════════════════════
# TAG: Clientes
# ══════════════════════════════════════════════════════════════

@app.get("/customers", summary="Listar clientes de la tienda", tags=["Clientes"])
async def get_customers(
    limit:  int = Query(default=50, ge=1, le=200, description="Número máximo de clientes"),
    offset: int = Query(default=0,  ge=0,         description="Desplazamiento para paginación"),
) -> JSONResponse:
    """Devuelve los clientes ordenados por fecha de registro descendente."""
    clientes = ps_client.get_customers(limit=limit, offset=offset)
    return JSONResponse({"total": len(clientes), "clientes": clientes})


@app.get("/customers/search", summary="Buscar cliente por email", tags=["Clientes"])
async def search_customers(
    email: str = Query(..., description="Email o parte del email del cliente"),
) -> JSONResponse:
    """
    Busca clientes cuyo email contenga el texto indicado.
    Devuelve todos los clientes que coincidan.
    """
    clientes = ps_client.search_customers(email)
    if not clientes:
        raise HTTPException(
            status_code=404,
            detail=f"No se encontraron clientes con email que contenga '{email}'.",
        )
    return JSONResponse({"total": len(clientes), "clientes": clientes})


@app.get("/customers/{customer_id}", summary="Ver datos de un cliente", tags=["Clientes"])
async def get_customer(
    customer_id: int = Path(..., ge=1, description="ID del cliente en PrestaShop"),
) -> JSONResponse:
    """Devuelve los datos completos de un cliente: nombre, email, teléfono, grupo y fecha de registro."""
    cliente = ps_client.get_customer(str(customer_id))
    if not cliente:
        raise HTTPException(status_code=404, detail=f"Cliente ID {customer_id} no encontrado.")
    return JSONResponse(cliente)


@app.get(
    "/customers/{customer_id}/orders",
    summary="Ver historial de pedidos de un cliente",
    tags=["Clientes"],
)
async def get_customer_orders(
    customer_id: int = Path(..., ge=1, description="ID del cliente en PrestaShop"),
) -> JSONResponse:
    """
    Devuelve todos los pedidos realizados por un cliente concreto,
    ordenados del más reciente al más antiguo.
    """
    pedidos = ps_client.get_customer_orders(str(customer_id))
    return JSONResponse({
        "customer_id": customer_id,
        "total_pedidos": len(pedidos),
        "pedidos": pedidos,
    })


# ══════════════════════════════════════════════════════════════
# TAG: Impuestos
# ══════════════════════════════════════════════════════════════

@app.get("/taxes", summary="Listar tipos de impuesto", tags=["Impuestos"])
async def get_taxes(
) -> JSONResponse:
    """
    Devuelve todos los tipos de impuesto con su porcentaje.
    Estos son los impuestos base (ej: IVA 21%, IVA 10%, IVA 4%).
    """
    impuestos = ps_client.get_taxes()
    return JSONResponse({"total": len(impuestos), "impuestos": impuestos})


@app.get("/taxes/rules", summary="Listar grupos de reglas de impuesto", tags=["Impuestos"])
async def get_tax_rules(
) -> JSONResponse:
    """
    Devuelve los grupos de reglas de impuesto disponibles.
    Son los que se asignan directamente a los productos.
    Consulta este endpoint para saber el ID que necesitas en
    PUT /taxes/assign o al crear un producto.
    """
    grupos = ps_client.get_tax_rules()
    return JSONResponse({
        "uso": "Usa el 'id' del grupo en PUT /taxes/assign para asignarlo a un producto.",
        "total": len(grupos),
        "grupos": grupos,
    })


@app.post("/taxes/rules", summary="Crear grupo de reglas de impuesto", tags=["Impuestos"])
async def create_tax_rule_group(
    name: str = Query(..., description="Nombre del grupo (ej: IVA 21% ES)"),
) -> JSONResponse:
    """
    Crea un nuevo grupo de reglas de impuesto en PrestaShop.
    Tras crearlo, configura las reglas concretas desde el panel de administración.
    """
    new_id = ps_client.create_tax_rule_group(name)
    if not new_id:
        raise HTTPException(status_code=502, detail=f"No se pudo crear el grupo de impuesto '{name}'.")
    return JSONResponse({"status": "created", "tax_rule_group_id": new_id, "nombre": name})


@app.put(
    "/taxes/assign",
    summary="Asignar impuesto a un producto",
    tags=["Impuestos"],
)
async def assign_tax_to_product(
    product_id:        int = Query(..., ge=1, description="ID del producto en PrestaShop"),
    tax_rule_group_id: int = Query(..., ge=0, description="ID del grupo de impuesto — consulta GET /taxes/rules"),
) -> JSONResponse:
    """
    Asigna un grupo de reglas de impuesto a un producto.
    Esto determina el IVA que se aplica al precio del producto en la tienda.

    **Antes de ejecutar**, consulta `GET /taxes/rules` para ver los IDs disponibles.
    """
    success = ps_client.assign_tax_to_product(str(product_id), str(tax_rule_group_id))
    if not success:
        raise HTTPException(
            status_code=502,
            detail=f"No se pudo asignar el impuesto al producto ID {product_id}.",
        )
    return JSONResponse({
        "status": "updated",
        "product_id": product_id,
        "tax_rule_group_id": tax_rule_group_id,
    })



# ══════════════════════════════════════════════════════════════
# TAG: Combinaciones
# ══════════════════════════════════════════════════════════════

@app.get(
    "/combinations/attributes",
    summary="Listar grupos de atributos y sus valores",
    tags=["Combinaciones"],
)
async def get_attributes(
) -> JSONResponse:
    """
    Devuelve todos los grupos de atributos disponibles (Talla, Color, etc.)
    con sus valores posibles (S, M, L / Rojo, Azul...).
    Consulta este endpoint para saber los IDs al crear combinaciones.
    """
    grupos = ps_client.get_product_attributes()
    return JSONResponse({"total": len(grupos), "atributos": grupos})


@app.get(
    "/combinations/product/{product_id}",
    summary="Ver combinaciones de un producto",
    tags=["Combinaciones"],
)
async def get_product_combinations(
    product_id: int = Path(..., ge=1, description="ID del producto en PrestaShop"),
) -> JSONResponse:
    """Devuelve todas las variantes (talla, color, etc.) de un producto con su stock."""
    combis = ps_client.get_product_combinations(str(product_id))
    return JSONResponse({"product_id": product_id, "total": len(combis), "combinaciones": combis})


@app.post(
    "/combinations/product/{product_id}",
    summary="Crear combinación para un producto",
    tags=["Combinaciones"],
)
async def create_combination(
    product_id:          int   = Path(..., ge=1, description="ID del producto en PrestaShop"),
    reference:           str   = Query(..., description="Referencia única de la variante (ej: CAMISETA-M-ROJO)"),
    price_extra:         float = Query(default=0.0, description="Precio adicional sobre el producto base"),
    id_attribute_values: str   = Query(..., description="IDs de valores de atributo separados por comas (ej: 1,5) — consulta GET /combinations/attributes"),
) -> JSONResponse:
    """
    Crea una variante de producto (ej: Talla M + Color Rojo).

    **Antes de ejecutar**, consulta `GET /combinations/attributes` para obtener
    los IDs de los valores de atributo que necesitas.
    """
    try:
        attr_ids = [v.strip() for v in id_attribute_values.split(",") if v.strip()]
    except Exception:
        raise HTTPException(status_code=400, detail="El campo id_attribute_values debe ser una lista de IDs separados por comas.")

    data = {"reference": reference, "price_extra": price_extra, "id_attribute_values": attr_ids}
    new_id = ps_client.create_combination(str(product_id), data)
    if not new_id:
        raise HTTPException(status_code=502, detail="No se pudo crear la combinación en PrestaShop.")
    return JSONResponse({"status": "created", "combination_id": new_id, "product_id": product_id})


@app.put(
    "/combinations/{combination_id}/stock/{qty}",
    summary="Actualizar stock de una combinación",
    tags=["Combinaciones"],
)
async def update_combination_stock(
    combination_id: int = Path(..., ge=1, description="ID de la combinación en PrestaShop"),
    qty:            int = Path(..., ge=0, description="Nueva cantidad de stock (≥0)"),
) -> JSONResponse:
    """Actualiza el stock disponible de una variante concreta de producto."""
    success = ps_client.update_combination_stock(str(combination_id), qty)
    if not success:
        raise HTTPException(status_code=502, detail=f"No se pudo actualizar el stock de la combinación ID {combination_id}.")
    return JSONResponse({"status": "updated", "combination_id": combination_id, "quantity": qty})


# ══════════════════════════════════════════════════════════════
# TAG: Características
# ══════════════════════════════════════════════════════════════

@app.get("/features", summary="Listar características disponibles", tags=["Características"])
async def get_features(
) -> JSONResponse:
    """
    Devuelve todas las características configuradas en la tienda (Material, Peso, Origen...).
    Son los campos de la ficha técnica del producto.
    """
    features = ps_client.get_features()
    return JSONResponse({"total": len(features), "caracteristicas": features})


@app.get(
    "/features/{feature_id}/values",
    summary="Listar valores de una característica",
    tags=["Características"],
)
async def get_feature_values(
    feature_id: int = Path(..., ge=1, description="ID de la característica"),
) -> JSONResponse:
    """Devuelve los valores posibles de una característica concreta."""
    valores = ps_client.get_feature_values(str(feature_id))
    return JSONResponse({"feature_id": feature_id, "total": len(valores), "valores": valores})


@app.put(
    "/features/assign",
    summary="Asignar característica a un producto",
    tags=["Características"],
)
async def assign_feature(
    product_id:       int = Query(..., ge=1, description="ID del producto en PrestaShop"),
    feature_id:       int = Query(..., ge=1, description="ID de la característica — consulta GET /features"),
    feature_value_id: int = Query(..., ge=1, description="ID del valor de la característica — consulta GET /features/{id}/values"),
) -> JSONResponse:
    """
    Asigna una característica con un valor concreto a un producto.
    Ejemplo: Material (ID 1) = Algodón (ID 3) → producto ID 10.

    **Antes de ejecutar**, consulta:
    - `GET /features` para ver las características y sus IDs
    - `GET /features/{feature_id}/values` para ver los valores disponibles
    """
    success = ps_client.assign_feature_to_product(
        str(product_id), str(feature_id), str(feature_value_id)
    )
    if not success:
        raise HTTPException(status_code=502, detail=f"No se pudo asignar la característica al producto ID {product_id}.")
    return JSONResponse({
        "status": "updated",
        "product_id": product_id,
        "feature_id": feature_id,
        "feature_value_id": feature_value_id,
    })


# ══════════════════════════════════════════════════════════════
# TAG: Descuentos
# ══════════════════════════════════════════════════════════════

@app.get("/discounts", summary="Listar cupones y descuentos activos", tags=["Descuentos"])
async def get_discounts(
    limit: int = Query(default=50, ge=1, le=200, description="Número máximo de cupones"),
) -> JSONResponse:
    """Devuelve todos los cupones de descuento (cart rules) configurados en la tienda."""
    cupones = ps_client.get_cart_rules(limit=limit)
    return JSONResponse({"total": len(cupones), "cupones": cupones})


@app.post("/discounts", summary="Crear cupón de descuento", tags=["Descuentos"])
async def create_discount(
    name:             str   = Query(..., description="Nombre del cupón"),
    code:             str   = Query(..., description="Código que introduce el cliente (ej: VERANO20)"),
    reduction_percent: float = Query(default=0.0, ge=0, le=100, description="Descuento en % (0 si usas importe fijo)"),
    reduction_amount:  float = Query(default=0.0, ge=0, description="Descuento en importe fijo (0 si usas porcentaje)"),
    quantity:         int   = Query(default=1000, ge=1, description="Número máximo de usos totales"),
    quantity_per_user: int  = Query(default=1, ge=1, description="Usos máximos por cliente"),
    date_from:        str   = Query(default="2024-01-01 00:00:00", description="Válido desde (YYYY-MM-DD HH:MM:SS)"),
    date_to:          str   = Query(default="2099-12-31 23:59:59", description="Válido hasta (YYYY-MM-DD HH:MM:SS)"),
) -> JSONResponse:
    """
    Crea un cupón de descuento que los clientes pueden introducir en el carrito.
    Usa `reduction_percent` para descuento porcentual o `reduction_amount` para importe fijo.
    """
    if reduction_percent == 0 and reduction_amount == 0:
        raise HTTPException(status_code=400, detail="Debes indicar reduction_percent o reduction_amount (al menos uno > 0).")

    data = {
        "name": name, "code": code,
        "reduction_percent": reduction_percent,
        "reduction_amount": reduction_amount,
        "quantity": quantity, "quantity_per_user": quantity_per_user,
        "date_from": date_from, "date_to": date_to,
    }
    new_id = ps_client.create_cart_rule(data)
    if not new_id:
        raise HTTPException(status_code=502, detail=f"No se pudo crear el cupón '{code}'.")
    return JSONResponse({"status": "created", "cart_rule_id": new_id, "codigo": code})


@app.delete("/discounts/{rule_id}", summary="Eliminar cupón de descuento", tags=["Descuentos"])
async def delete_discount(
    rule_id: int = Path(..., ge=1, description="ID del cupón a eliminar"),
) -> JSONResponse:
    """Elimina un cupón de descuento por su ID. Esta acción es irreversible."""
    success = ps_client.delete_cart_rule(str(rule_id))
    if not success:
        raise HTTPException(status_code=502, detail=f"No se pudo eliminar el cupón ID {rule_id}.")
    return JSONResponse({"status": "deleted", "cart_rule_id": rule_id})


@app.get(
    "/discounts/product/{product_id}",
    summary="Ver precios específicos de un producto",
    tags=["Descuentos"],
)
async def get_specific_prices(
    product_id: int = Path(..., ge=1, description="ID del producto en PrestaShop"),
) -> JSONResponse:
    """
    Devuelve las reglas de precio específico de un producto:
    descuentos por cantidad, por grupo de cliente, por fechas, etc.
    """
    precios = ps_client.get_specific_prices(str(product_id))
    return JSONResponse({"product_id": product_id, "total": len(precios), "precios_especificos": precios})


# ══════════════════════════════════════════════════════════════
# TAG: Transportistas
# ══════════════════════════════════════════════════════════════

@app.get("/carriers", summary="Listar transportistas", tags=["Transportistas"])
async def get_carriers() -> JSONResponse:
    """Devuelve todos los transportistas configurados en la tienda con sus tarifas y estado."""
    transportistas = ps_client.get_carriers()
    return JSONResponse({"total": len(transportistas), "transportistas": transportistas})


@app.get("/carriers/{carrier_id}", summary="Ver detalle de un transportista", tags=["Transportistas"])
async def get_carrier(
    carrier_id: int = Path(..., ge=1, description="ID del transportista"),
) -> JSONResponse:
    """Devuelve todos los datos de un transportista: nombre, delay, tarifas, tracking, etc."""
    transportista = ps_client.get_carrier(str(carrier_id))
    if not transportista:
        raise HTTPException(status_code=404, detail=f"Transportista ID {carrier_id} no encontrado.")
    return JSONResponse(transportista)


@app.post("/carriers", summary="Crear transportista", tags=["Transportistas"])
async def create_carrier(
    nombre:      str   = Query(...,            description="Nombre del transportista"),
    delay:       str   = Query(...,            description="Plazo de entrega (ej: 3-5 días hábiles)"),
    activo:      bool  = Query(default=True,   description="Activo desde el inicio"),
    gratis:      bool  = Query(default=False,  description="Envío gratuito"),
    url_tracking:str   = Query(default=None,   description="URL de seguimiento (usa @) para el número)"),
    max_peso:    float = Query(default=None, ge=0, description="Peso máximo en kg que admite este transportista"),
) -> JSONResponse:
    """
    Crea un nuevo transportista en PrestaShop.

    Tras crearlo, configura sus rangos de precio/peso desde el panel de administración
    de PS (Transporte → Transportistas → Editar) para que aparezca en el checkout.
    """
    data = {
        "name":   nombre,
        "delay":  delay,
        "active": activo,
        "is_free": gratis,
    }
    if url_tracking: data["url"]        = url_tracking
    if max_peso is not None: data["max_weight"] = max_peso

    new_id = ps_client.create_carrier(data)
    if not new_id:
        raise HTTPException(status_code=502, detail="No se pudo crear el transportista.")
    return JSONResponse({"status": "created", "carrier_id": new_id, "nombre": nombre})


@app.put("/carriers/{carrier_id}", summary="Actualizar transportista", tags=["Transportistas"])
async def update_carrier(
    carrier_id:   int   = Path(..., ge=1,  description="ID del transportista"),
    nombre:       str   = Query(default=None, description="Nuevo nombre"),
    activo:       bool  = Query(default=None, description="true = activo, false = inactivo"),
    gratis:       bool  = Query(default=None, description="true = envío gratis"),
    delay:        str   = Query(default=None, description="Nuevo plazo de entrega (ej: 24-48h)"),
    url_tracking: str   = Query(default=None, description="Nueva URL de seguimiento"),
    max_peso:     float = Query(default=None, ge=0, description="Nuevo peso máximo en kg"),
) -> JSONResponse:
    """
    Actualiza uno o varios campos de un transportista existente.
    Solo se modifican los campos que se incluyan.
    """
    data = {}
    if nombre       is not None: data["name"]       = nombre
    if activo       is not None: data["active"]      = activo
    if gratis       is not None: data["is_free"]     = gratis
    if delay        is not None: data["delay"]       = delay
    if url_tracking is not None: data["url"]         = url_tracking
    if max_peso     is not None: data["max_weight"]  = max_peso

    if not data:
        raise HTTPException(status_code=400, detail="Debes indicar al menos un campo a actualizar.")

    success = ps_client.update_carrier(str(carrier_id), data)
    if not success:
        raise HTTPException(status_code=502, detail=f"No se pudo actualizar el transportista ID {carrier_id}.")
    return JSONResponse({"status": "updated", "carrier_id": carrier_id, "campos": list(data.keys())})


@app.delete("/carriers/{carrier_id}", summary="Eliminar transportista", tags=["Transportistas"])
async def delete_carrier(
    carrier_id: int = Path(..., ge=1, description="ID del transportista a eliminar"),
) -> JSONResponse:
    """
    Elimina un transportista de PrestaShop.

    ⚠️ Esta acción es permanente. Asegúrate de que el transportista no está
    asignado a ningún pedido activo antes de eliminarlo.
    """
    success = ps_client.delete_carrier(str(carrier_id))
    if not success:
        raise HTTPException(status_code=502, detail=f"No se pudo eliminar el transportista ID {carrier_id}.")
    return JSONResponse({"status": "deleted", "carrier_id": carrier_id})




# ══════════════════════════════════════════════════════════════
# TAG: Devoluciones
# ══════════════════════════════════════════════════════════════

@app.get(
    "/returns",
    summary="Listar devoluciones",
    tags=["Devoluciones"],
)
async def get_returns(
    limit: int = Query(default=50, ge=1, le=200, description="Número máximo de devoluciones"),
) -> JSONResponse:
    """
    Devuelve todas las devoluciones de la tienda ordenadas por fecha descendente.

    **Estados de devolución:**
    - 1 = En espera de confirmación
    - 2 = En espera de paquete
    - 3 = Paquete recibido
    - 4 = Devuelto
    - 5 = Cerrado
    """
    devoluciones = ps_client.get_returns(limit=limit)
    return JSONResponse({
        "total":        len(devoluciones),
        "devoluciones": devoluciones,
    })


@app.get(
    "/returns/{return_id}",
    summary="Ver detalle de una devolución",
    tags=["Devoluciones"],
)
async def get_return(
    return_id: int = Path(..., ge=1, description="ID de la devolución"),
) -> JSONResponse:
    """Devuelve el detalle completo de una devolución con sus líneas de producto."""
    devolucion = ps_client.get_return(str(return_id))
    if not devolucion:
        raise HTTPException(status_code=404, detail=f"Devolución ID {return_id} no encontrada.")
    return JSONResponse(devolucion)


@app.put(
    "/returns/{return_id}/state/{state_id}",
    summary="Actualizar estado de una devolución",
    tags=["Devoluciones"],
)
async def update_return_state(
    return_id: int = Path(..., ge=1, description="ID de la devolución"),
    state_id:  int = Path(..., ge=1, le=5,  description="Nuevo estado: 1=Espera confirmación, 2=Espera paquete, 3=Recibido, 4=Devuelto, 5=Cerrado"),
) -> JSONResponse:
    """
    Actualiza el estado de una devolución.

    Flujo habitual:
    1 → **Espera confirmación** (cliente solicita devolución)
    2 → **Espera paquete** (confirmada, esperando recibir)
    3 → **Paquete recibido** (mercancía en almacén)
    4 → **Devuelto** (reembolso procesado)
    5 → **Cerrado** (proceso completado)
    """
    estados = {1: "Espera confirmación", 2: "Espera paquete", 3: "Paquete recibido",
               4: "Devuelto", 5: "Cerrado"}
    success = ps_client.update_return_state(str(return_id), str(state_id))
    if not success:
        raise HTTPException(status_code=502, detail=f"No se pudo actualizar la devolución ID {return_id}.")
    return JSONResponse({
        "status":     "updated",
        "return_id":  return_id,
        "state_id":   state_id,
        "estado":     estados[state_id],
    })

# ══════════════════════════════════════════════════════════════
# TAG: Facturas y Documentos
# ══════════════════════════════════════════════════════════════

@app.get(
    "/orders/{order_id}/invoice/pdf",
    summary="Descargar factura PDF de un pedido",
    tags=["Facturas y Documentos"],
)
async def get_order_invoice_pdf(
    order_id: int = Path(..., ge=1, description="ID del pedido en PrestaShop"),
    request: Request = None,
):
    """
    Descarga el PDF de la factura de un pedido directamente desde PrestaShop.

    Devuelve el PDF como archivo descargable (application/pdf).
    Útil para adjuntarlo en emails desde n8n u otros sistemas.

    Ejemplo: `http://localhost:8000/orders/35/invoice/pdf`
    """
    from fastapi.responses import Response
    from core.invoice_generator import generar_factura_pdf

    # 1. Obtener datos de la factura desde PS
    facturas = ps_client.get_order_invoices(str(order_id))
    if not facturas:
        raise HTTPException(
            status_code=404,
            detail=f"No se encontró factura para el pedido {order_id}. "
                   "El pedido puede no haber sido autorizado aún."
        )
    factura = facturas[0]
    numero  = factura.get("numero", factura.get("id", "?"))

    # 2. Obtener datos del pedido y cliente
    customer_name = ""
    reference     = ""
    date_add      = ""
    try:
        pedido_raw = ps_client._request("GET", f"orders/{order_id}?display=full")
        order_node = pedido_raw.find(".//order") if pedido_raw is not None else None
        if order_node is not None:
            reference   = order_node.findtext("reference") or ""
            date_add    = order_node.findtext("date_add") or ""
            customer_id = order_node.findtext("id_customer")
            if customer_id:
                cust = ps_client._request("GET", f"customers/{customer_id}?display=[firstname,lastname]")
                if cust is not None:
                    fn = cust.findtext(".//firstname") or ""
                    ln = cust.findtext(".//lastname") or ""
                    customer_name = f"{fn} {ln}".strip()
    except Exception:
        pass

    # 3. Obtener líneas de productos del pedido
    productos = []
    try:
        rows = ps_client._request("GET", f"order_details?filter[id_order]={order_id}&display=full")
        if rows is not None:
            for od in rows.findall(".//order_detail"):
                productos.append({
                    "nombre":      od.findtext("product_name") or "",
                    "referencia":  od.findtext("product_reference") or "",
                    "cantidad":    int(od.findtext("product_quantity") or 0),
                    "precio_unit": round(float(od.findtext("unit_price_tax_incl") or 0), 2),
                    "total":       round(float(od.findtext("total_price_tax_incl") or 0), 2),
                    "tasa_iva":    round(float(od.findtext("tax_rate") or 21), 0),
                })
    except Exception:
        pass

    # 4. Generar PDF con reportlab
    pdf_bytes = generar_factura_pdf(
        factura={
            "id":            factura.get("id"),
            "numero":        numero,
            "fecha":         factura.get("fecha", date_add),
            "total_sin_iva": float(factura.get("total_sin_iva", 0)),
            "total_con_iva": float(factura.get("total_con_iva", 0)),
            "total_envio":   float(factura.get("total_envio", 0)),
        },
        pedido={
            "order_id":  order_id,
            "reference": reference,
            "customer":  customer_name,
            "date_add":  date_add,
            "currency":  "EUR",
        },
        productos=productos,
        tienda={"nombre": "Mi Tienda"},
    )

    filename = f"factura_FA{str(numero).zfill(6)}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get(
    "/orders/{order_id}/invoices",
    summary="Ver facturas de un pedido",
    tags=["Facturas y Documentos"],
)
async def get_order_invoices(
    order_id: int = Path(..., ge=1, description="ID del pedido"),
) -> JSONResponse:
    """
    Devuelve las facturas generadas para un pedido.

    PrestaShop genera la factura automáticamente cuando el pedido
    alcanza el estado **Pago aceptado** (o según la configuración de la tienda).
    Incluye la URL del PDF de cada factura.
    """
    facturas = ps_client.get_order_invoices(str(order_id))
    return JSONResponse({
        "order_id": order_id,
        "total":    len(facturas),
        "facturas": facturas,
    })


@app.get(
    "/invoices",
    summary="Listar todas las facturas",
    tags=["Facturas y Documentos"],
)
async def get_all_invoices(
    date_from: str = Query(default=None, description="Fecha inicio (YYYY-MM-DD HH:MM:SS)"),
    date_to:   str = Query(default=None, description="Fecha fin (YYYY-MM-DD HH:MM:SS)"),
    limit:     int = Query(default=50, ge=1, le=500, description="Máximo de facturas a devolver"),
) -> JSONResponse:
    """
    Lista todas las facturas de la tienda con filtro de fecha opcional.

    - Sin fechas → devuelve las últimas `limit` facturas
    - Con `date_from` y `date_to` → filtra por periodo
    """
    facturas = ps_client.get_all_invoices(
        date_from=date_from,
        date_to=date_to,
        limit=limit,
    )
    return JSONResponse({
        "total":    len(facturas),
        "facturas": facturas,
    })


@app.get(
    "/invoices/financial-summary",
    summary="Resumen financiero por periodo",
    tags=["Facturas y Documentos"],
)
async def get_financial_summary(
    date_from: str = Query(default=None, description="Inicio del periodo (YYYY-MM-DD HH:MM:SS). Sin valor = mes en curso"),
    date_to:   str = Query(default=None, description="Fin del periodo (YYYY-MM-DD HH:MM:SS). Sin valor = hoy"),
) -> JSONResponse:
    """
    Genera un resumen financiero del periodo indicado a partir de las facturas.

    Devuelve:
    - Número de facturas emitidas
    - Total facturado sin IVA
    - Total IVA
    - Total facturado con IVA
    - Listado completo de facturas del periodo

    Sin parámetros devuelve el resumen del mes en curso.
    """
    resumen = ps_client.get_financial_summary(date_from=date_from, date_to=date_to)
    return JSONResponse(resumen)


@app.get(
    "/orders/{order_id}/slips",
    summary="Ver albaranes / notas de crédito de un pedido",
    tags=["Facturas y Documentos"],
)
async def get_order_slips(
    order_id: int = Path(..., ge=1, description="ID del pedido"),
) -> JSONResponse:
    """
    Devuelve los albaranes o notas de crédito (devoluciones) de un pedido.

    PrestaShop genera un albarán automáticamente cuando se procesa
    una devolución parcial o total del pedido.
    """
    albaranes = ps_client.get_order_slips(str(order_id))
    return JSONResponse({
        "order_id":  order_id,
        "total":     len(albaranes),
        "albaranes": albaranes,
    })


@app.get(
    "/orders/{order_id}/history",
    summary="Historial de estados de un pedido",
    tags=["Facturas y Documentos"],
)
async def get_order_history(
    order_id: int = Path(..., ge=1, description="ID del pedido"),
) -> JSONResponse:
    """
    Devuelve el historial completo de cambios de estado de un pedido,
    del más reciente al más antiguo.

    Permite al administrador ver exactamente cuándo y en qué orden
    se procesaron los distintos estados: pago, preparación, envío, entrega.
    """
    historial = ps_client.get_order_history(str(order_id))
    if not historial:
        raise HTTPException(
            status_code=404,
            detail=f"No se encontró historial para el pedido ID {order_id}.",
        )
    return JSONResponse({
        "order_id":  order_id,
        "total":     len(historial),
        "historial": historial,
    })

# ══════════════════════════════════════════════════════════════
# TAG: Pagos
# ══════════════════════════════════════════════════════════════

@app.get(
    "/payments/order/{order_reference}",
    summary="Ver pagos de un pedido",
    tags=["Pagos"],
)
async def get_order_payments(
    order_reference: str = Path(..., description="Referencia del pedido (ej: BGFZYHKF1)"),
) -> JSONResponse:
    """
    Devuelve los pagos registrados para un pedido concreto.
    Incluye método de pago, importe, divisa y fecha de transacción.
    """
    pagos = ps_client.get_order_payments(order_reference)
    if not pagos:
        raise HTTPException(
            status_code=404,
            detail=f"No se encontraron pagos para el pedido '{order_reference}'.",
        )
    return JSONResponse({"order_reference": order_reference, "total": len(pagos), "pagos": pagos})


# ══════════════════════════════════════════════════════════════
# TAG: Divisas e Idiomas
# ══════════════════════════════════════════════════════════════

@app.get("/currencies", summary="Listar divisas de la tienda", tags=["Divisas e Idiomas"])
async def get_currencies(
) -> JSONResponse:
    """Devuelve todas las divisas configuradas con su símbolo, tasa de cambio y estado."""
    divisas = ps_client.get_currencies()
    return JSONResponse({"total": len(divisas), "divisas": divisas})


@app.put(
    "/currencies/{currency_id}/rate",
    summary="Actualizar tasa de cambio de una divisa",
    tags=["Divisas e Idiomas"],
)
async def update_currency_rate(
    currency_id: int   = Path(..., ge=1, description="ID de la divisa en PrestaShop"),
    rate:        float = Query(..., gt=0, description="Nueva tasa de cambio respecto al euro (ej: 1.08)"),
) -> JSONResponse:
    """
    Actualiza la tasa de cambio de una divisa.
    La divisa base de tu tienda siempre tiene tasa 1.0.
    """
    success = ps_client.update_currency_rate(str(currency_id), rate)
    if not success:
        raise HTTPException(status_code=502, detail=f"No se pudo actualizar la tasa de la divisa ID {currency_id}.")
    return JSONResponse({"status": "updated", "currency_id": currency_id, "rate": rate})


@app.get("/languages", summary="Listar idiomas instalados", tags=["Divisas e Idiomas"])
async def get_languages(
) -> JSONResponse:
    """Devuelve todos los idiomas instalados en la tienda con su código ISO y estado."""
    idiomas = ps_client.get_languages()
    return JSONResponse({"total": len(idiomas), "idiomas": idiomas})



# ══════════════════════════════════════════════════════════════
# TAG: Direcciones
# ══════════════════════════════════════════════════════════════

@app.get(
    "/addresses/customer/{customer_id}",
    summary="Ver direcciones de un cliente",
    tags=["Direcciones"],
)
async def get_customer_addresses(
    customer_id: int = Path(..., ge=1, description="ID del cliente en PrestaShop"),
) -> JSONResponse:
    """Devuelve todas las direcciones de envío y facturación de un cliente."""
    direcciones = ps_client.get_customer_addresses(str(customer_id))
    return JSONResponse({"customer_id": customer_id, "total": len(direcciones), "direcciones": direcciones})


@app.post("/addresses", summary="Crear dirección para un cliente", tags=["Direcciones"])
async def create_address(
    id_customer: int = Query(..., ge=1, description="ID del cliente en PrestaShop"),
    alias:       str = Query(default="Casa", description="Alias de la dirección (ej: Casa, Trabajo)"),
    firstname:   str = Query(..., description="Nombre"),
    lastname:    str = Query(..., description="Apellidos"),
    address1:    str = Query(..., description="Dirección línea 1"),
    address2:    str = Query(default="", description="Dirección línea 2 (opcional)"),
    city:        str = Query(..., description="Ciudad"),
    postcode:    str = Query(..., description="Código postal"),
    id_country:  int = Query(default=6, description="ID del país — consulta GET /countries (España=6)"),
    phone:       str = Query(default="", description="Teléfono de contacto"),
    company:     str = Query(default="", description="Empresa (opcional)"),
) -> JSONResponse:
    """Crea una dirección nueva asociada a un cliente existente."""
    data = {
        "id_customer": id_customer, "alias": alias,
        "firstname": firstname, "lastname": lastname,
        "address1": address1, "address2": address2,
        "city": city, "postcode": postcode,
        "id_country": id_country, "phone": phone, "company": company,
    }
    new_id = ps_client.create_address(data)
    if not new_id:
        raise HTTPException(status_code=502, detail="No se pudo crear la dirección.")
    return JSONResponse({"status": "created", "address_id": new_id, "customer_id": id_customer})


@app.delete("/addresses/{address_id}", summary="Eliminar dirección", tags=["Direcciones"])
async def delete_address(
    address_id: int = Path(..., ge=1, description="ID de la dirección a eliminar"),
) -> JSONResponse:
    """Elimina una dirección de la tienda. Esta acción es irreversible."""
    success = ps_client.delete_address(str(address_id))
    if not success:
        raise HTTPException(status_code=502, detail=f"No se pudo eliminar la dirección ID {address_id}.")
    return JSONResponse({"status": "deleted", "address_id": address_id})


# ══════════════════════════════════════════════════════════════
# TAG: Grupos de cliente
# ══════════════════════════════════════════════════════════════

@app.get("/customer-groups", summary="Listar grupos de clientes", tags=["Grupos de cliente"])
async def get_customer_groups(
) -> JSONResponse:
    """
    Devuelve todos los grupos de clientes configurados en la tienda
    (Cliente, Invitado, Mayorista, VIP...) con su descuento por defecto.
    """
    grupos = ps_client.get_customer_groups()
    return JSONResponse({"total": len(grupos), "grupos": grupos})


@app.put(
    "/customer-groups/assign",
    summary="Asignar cliente a un grupo",
    tags=["Grupos de cliente"],
)
async def assign_customer_group(
    customer_id: int = Query(..., ge=1, description="ID del cliente en PrestaShop"),
    group_id:    int = Query(..., ge=1, description="ID del grupo — consulta GET /customer-groups"),
) -> JSONResponse:
    """
    Asigna un cliente a un grupo concreto.
    Esto determina los precios y descuentos que ve ese cliente en la tienda.

    **Antes de ejecutar**, consulta `GET /customer-groups` para ver los IDs disponibles.
    """
    success = ps_client.assign_customer_group(str(customer_id), str(group_id))
    if not success:
        raise HTTPException(status_code=502, detail=f"No se pudo asignar el grupo {group_id} al cliente {customer_id}.")
    return JSONResponse({"status": "updated", "customer_id": customer_id, "group_id": group_id})


# ══════════════════════════════════════════════════════════════
# TAG: CMS
# ══════════════════════════════════════════════════════════════

@app.get("/cms", summary="Listar páginas CMS", tags=["CMS"])
async def get_cms_pages(
) -> JSONResponse:
    """
    Devuelve todas las páginas CMS de la tienda con una preview del contenido.
    Ejemplos: Aviso legal, Política de privacidad, FAQ, Sobre nosotros.
    """
    paginas = ps_client.get_cms_pages()
    return JSONResponse({"total": len(paginas), "paginas": paginas})


@app.get("/cms/{page_id}", summary="Ver contenido completo de una página CMS", tags=["CMS"])
async def get_cms_page(
    page_id: int = Path(..., ge=1, description="ID de la página CMS"),
) -> JSONResponse:
    """Devuelve el contenido HTML completo de una página CMS."""
    pagina = ps_client.get_cms_page(str(page_id))
    if not pagina:
        raise HTTPException(status_code=404, detail=f"Página CMS ID {page_id} no encontrada.")
    return JSONResponse(pagina)


@app.put("/cms/{page_id}", summary="Actualizar página CMS", tags=["CMS"])
async def update_cms_page(
    page_id: int = Path(..., ge=1, description="ID de la página CMS"),
    title:   str = Query(default=None, description="Nuevo título de la página"),
    content: str = Query(default=None, description="Nuevo contenido HTML de la página"),
    active:  int = Query(default=None, ge=0, le=1, description="1 = visible, 0 = oculta"),
) -> JSONResponse:
    """Actualiza el título, contenido o visibilidad de una página CMS."""
    data = {}
    if title   is not None: data["title"]   = title
    if content is not None: data["content"] = content
    if active  is not None: data["active"]  = active

    if not data:
        raise HTTPException(status_code=400, detail="Debes indicar al menos un campo a actualizar.")

    success = ps_client.update_cms_page(str(page_id), data)
    if not success:
        raise HTTPException(status_code=502, detail=f"No se pudo actualizar la página CMS ID {page_id}.")
    return JSONResponse({"status": "updated", "page_id": page_id, "campos": list(data.keys())})


# ══════════════════════════════════════════════════════════════
# TAG: Etiquetas
# ══════════════════════════════════════════════════════════════

@app.get("/tags", summary="Listar etiquetas de búsqueda", tags=["Etiquetas"])
async def get_tags(
    limit: int = Query(default=100, ge=1, le=500, description="Número máximo de etiquetas"),
) -> JSONResponse:
    """Devuelve todas las etiquetas de búsqueda creadas en la tienda."""
    etiquetas = ps_client.get_tags(limit=limit)
    return JSONResponse({"total": len(etiquetas), "etiquetas": etiquetas})


@app.get("/tags/product/{product_id}", summary="Ver etiquetas de un producto", tags=["Etiquetas"])
async def get_product_tags(
    product_id: int = Path(..., ge=1, description="ID del producto en PrestaShop"),
) -> JSONResponse:
    """Devuelve las etiquetas asignadas a un producto concreto."""
    tags = ps_client.get_product_tags(str(product_id))
    return JSONResponse({"product_id": product_id, "total": len(tags), "etiquetas": tags})


@app.post("/tags", summary="Crear etiqueta de búsqueda", tags=["Etiquetas"])
async def create_tag(
    name:    str = Query(..., description="Nombre de la etiqueta"),
    id_lang: int = Query(default=1, ge=1, description="ID del idioma (1 = español por defecto)"),
) -> JSONResponse:
    """Crea una nueva etiqueta de búsqueda en la tienda."""
    new_id = ps_client.create_tag(name, str(id_lang))
    if not new_id:
        raise HTTPException(status_code=502, detail=f"No se pudo crear la etiqueta '{name}'.")
    return JSONResponse({"status": "created", "tag_id": new_id, "nombre": name})


# ══════════════════════════════════════════════════════════════
# TAG: Países y Zonas
# ══════════════════════════════════════════════════════════════

@app.get("/countries", summary="Listar países disponibles", tags=["Países y Zonas"])
async def get_countries(
    all_countries: bool = Query(default=False, description="False = solo activos, True = todos"),
) -> JSONResponse:
    """
    Devuelve los países configurados en la tienda.
    Por defecto devuelve solo los activos para envíos.
    Útil para saber el ID de país al crear direcciones.
    """
    paises = ps_client.get_countries(active_only=not all_countries)
    return JSONResponse({"total": len(paises), "paises": paises})


@app.get("/zones", summary="Listar zonas geográficas", tags=["Países y Zonas"])
async def get_zones(
) -> JSONResponse:
    """Devuelve todas las zonas geográficas configuradas para cálculo de envíos."""
    zonas = ps_client.get_zones()
    return JSONResponse({"total": len(zonas), "zonas": zonas})


# ══════════════════════════════════════════════════════════════
# TAG: Reseñas
# ══════════════════════════════════════════════════════════════

@app.get(
    "/reviews/product/{product_id}",
    summary="Ver reseñas de un producto",
    tags=["Reseñas"],
)
async def get_product_reviews(
    product_id: int = Path(..., ge=1, description="ID del producto en PrestaShop"),
) -> JSONResponse:
    """
    Devuelve las reseñas y valoraciones de un producto.
    ⚠️ Requiere el módulo 'Product Comments' instalado y activado en PrestaShop.
    Si el módulo no está activo devolverá lista vacía.
    """
    resenas = ps_client.get_product_reviews(str(product_id))
    return JSONResponse({"product_id": product_id, "total": len(resenas), "resenas": resenas})


# ══════════════════════════════════════════════════════════════
# TAG: Configuración
# ══════════════════════════════════════════════════════════════

@app.get("/config", summary="Leer configuración de la tienda", tags=["Configuración"])
async def get_configuration(
    keys: str = Query(
        default="PS_SHOP_NAME,PS_SHOP_EMAIL,PS_CURRENCY_DEFAULT,PS_LANG_DEFAULT,PS_WEIGHT_UNIT,PS_DIMENSION_UNIT",
        description="Claves de configuración separadas por comas",
    ),
) -> JSONResponse:
    """
    Lee uno o varios valores de configuración global de la tienda.

    **Claves habituales:**
    - `PS_SHOP_NAME` — nombre de la tienda
    - `PS_SHOP_EMAIL` — email de contacto
    - `PS_CURRENCY_DEFAULT` — ID de la divisa por defecto
    - `PS_LANG_DEFAULT` — ID del idioma por defecto
    - `PS_WEIGHT_UNIT` — unidad de peso (kg, g, lb...)
    - `PS_DIMENSION_UNIT` — unidad de dimensión (cm, in...)
    - `PS_ORDER_RETURN` — 1 si las devoluciones están activas
    """
    key_list = [k.strip() for k in keys.split(",") if k.strip()]
    if not key_list:
        raise HTTPException(status_code=400, detail="Debes indicar al menos una clave de configuración.")
    valores = ps_client.get_configurations(key_list)
    return JSONResponse({"total": len(valores), "configuracion": valores})


@app.put("/config", summary="Actualizar configuración de la tienda", tags=["Configuración"])
async def update_configuration(
    key:   str = Query(..., description="Clave de configuración (ej: PS_SHOP_NAME)"),
    value: str = Query(..., description="Nuevo valor"),
) -> JSONResponse:
    """
    Actualiza un valor de configuración global de la tienda.

    ⚠️ Esta operación modifica ajustes globales de PrestaShop.
    Úsala con precaución — un valor incorrecto puede afectar al funcionamiento de la tienda.
    """
    success = ps_client.update_configuration(key, value)
    if not success:
        raise HTTPException(
            status_code=502,
            detail=f"No se pudo actualizar la configuración '{key}'. Verifica que la clave existe.",
        )
    return JSONResponse({"status": "updated", "key": key, "value": value})



# ══════════════════════════════════════════════════════════════
# TAG: Sistema
# ══════════════════════════════════════════════════════════════

@app.get(
    "/system/circuit-breakers",
    summary="Estado de los circuit breakers",
    tags=["Sistema"],
)
async def get_circuit_breakers() -> JSONResponse:
    """
    Devuelve el estado actual de todos los circuit breakers del sistema.

    Estados posibles:
    - `closed` — funcionando con normalidad
    - `open` — bloqueado por demasiados fallos, las llamadas se rechazan
    - `half_open` — en período de prueba tras recuperación
    """
    return JSONResponse({
        "circuit_breakers": {
            "prestashop": prestashop_circuit.status(),
            "mysql":      mysql_circuit.status(),
        }
    })


@app.post(
    "/system/circuit-breakers/reset",
    summary="Resetear circuit breakers manualmente",
    tags=["Sistema"],
)
async def reset_circuit_breakers(
    target: str = Query(
        default="all",
        description="Circuit breaker a resetear: 'prestashop', 'mysql' o 'all'",
    ),
) -> JSONResponse:
    """
    Resetea uno o todos los circuit breakers a estado CLOSED.

    Útil cuando el servicio externo ha sido restaurado manualmente
    y no quieres esperar el tiempo de recuperación automática.
    """
    reseteados = []
    if target in ("prestashop", "all"):
        prestashop_circuit.reset()
        reseteados.append("prestashop")
    if target in ("mysql", "all"):
        mysql_circuit.reset()
        reseteados.append("mysql")
    if not reseteados:
        raise HTTPException(
            status_code=400,
            detail=f"Target '{target}' no válido. Usa 'prestashop', 'mysql' o 'all'.",
        )
    return JSONResponse({
        "status": "reset",
        "reseteados": reseteados,
        "circuit_breakers": {
            "prestashop": prestashop_circuit.status(),
            "mysql":      mysql_circuit.status(),
        },
    })



# ══════════════════════════════════════════════════════════════
# TAG: Estadísticas
# ══════════════════════════════════════════════════════════════

@app.get("/stats", summary="Resumen general de la tienda", tags=["Estadísticas"])
async def get_stats_overview() -> JSONResponse:
    """
    Devuelve un resumen completo de las métricas principales de la tienda:
    pedidos, productos, clientes y categorías en una sola llamada.
    """
    pedidos    = ps_client.get_orders_stats()
    productos  = ps_client.get_products_stats()
    clientes   = ps_client.get_customers_stats()
    categorias = ps_client.get_categories_stats()

    return JSONResponse({
        "pedidos":    pedidos,
        "productos":  productos,
        "clientes":   clientes,
        "categorias": categorias,
    })


@app.get("/stats/orders", summary="Estadísticas de pedidos", tags=["Estadísticas"])
async def get_orders_stats() -> JSONResponse:
    """
    Devuelve estadísticas detalladas de pedidos:
    total, importe acumulado y desglose por estado.

    Los estados más habituales en PrestaShop son:
    - 1 = Pendiente de pago
    - 2 = Pago aceptado
    - 3 = En preparación
    - 4 = Enviado
    - 5 = Entregado
    """
    stats = ps_client.get_orders_stats()
    if not stats:
        raise HTTPException(status_code=502, detail="No se pudieron obtener las estadísticas de pedidos.")
    return JSONResponse(stats)


@app.get("/stats/products", summary="Estadísticas del catálogo", tags=["Estadísticas"])
async def get_products_stats() -> JSONResponse:
    """
    Devuelve estadísticas del catálogo de productos:
    total, activos/inactivos, sin stock y estadísticas de precios.
    """
    stats = ps_client.get_products_stats()
    if not stats:
        raise HTTPException(status_code=502, detail="No se pudieron obtener las estadísticas de productos.")
    return JSONResponse(stats)


@app.get("/stats/customers", summary="Estadísticas de clientes", tags=["Estadísticas"])
async def get_customers_stats() -> JSONResponse:
    """
    Devuelve estadísticas de clientes:
    total, activos/inactivos y nuevos registros en los últimos 30 días.
    """
    stats = ps_client.get_customers_stats()
    if not stats:
        raise HTTPException(status_code=502, detail="No se pudieron obtener las estadísticas de clientes.")
    return JSONResponse(stats)


@app.get("/stats/categories", summary="Estadísticas de categorías", tags=["Estadísticas"])
async def get_categories_stats() -> JSONResponse:
    """Devuelve el total de categorías y cuántas están activas."""
    stats = ps_client.get_categories_stats()
    if not stats:
        raise HTTPException(status_code=502, detail="No se pudieron obtener las estadísticas de categorías.")
    return JSONResponse(stats)




@app.get(
    "/catalog/reconcile",
    summary="Informe de reconciliación BD ↔ PrestaShop",
    tags=["Catálogo"],
)
async def reconcile_catalog(
    solo_diferencias: bool = Query(
        default=False,
        description="Si True, devuelve solo los productos con diferencias o sin sincronizar",
    ),
) -> JSONResponse:
    """
    Compara el estado actual de la BD local con PrestaShop y genera un
    informe detallado **sin modificar nada**.

    Cada producto recibe una `accion_sugerida`:
    - **`ok`** — BD y PS están sincronizados, no hay que hacer nada
    - **`crear`** — producto existe en BD pero no en PS todavía
    - **`actualizar`** — producto vinculado pero hay diferencias (precio, stock, nombre)
    - **`revisar`** — existe en PS pero no tiene registro en la BD local

    Usa este endpoint antes de lanzar `POST /catalog/sync` para anticipar
    qué cambios se aplicarán.
    """
    # 1. Cargar datos locales
    productos_bd = db_handler.obtener_datos_completos()
    if productos_bd is None:
        raise HTTPException(status_code=502, detail="No se pudo acceder a la BD local.")

    # 2. Cargar snapshot de PS (solo productos que ya están vinculados)
    # Para no hacer N llamadas, traemos todos los productos de PS de una vez
    ps_res = ps_client._request(
        "GET", "products?display=[id,name,reference,price,active]"
    )
    ps_por_id = {}
    if ps_res is not None:
        for p in ps_res.findall(".//product"):
            pid = p.findtext("id")
            if not pid:
                continue
            name_node = p.find(".//name/language")
            ps_por_id[pid] = {
                "nombre":     name_node.text if name_node is not None else "",
                "referencia": p.findtext("reference") or "",
                "precio":     float(p.findtext("price") or 0),
                "activo":     p.findtext("active") == "1",
            }

    # Traer stocks de PS en una sola llamada
    stock_res = ps_client._request(
        "GET",
        "stock_availables?filter[id_product_attribute]=0&display=[id_product,quantity]"
    )
    stocks_ps = {}
    if stock_res is not None:
        for s in stock_res.findall(".//stock_available"):
            pid   = s.findtext("id_product")
            qty   = s.findtext("quantity")
            if pid:
                try:
                    stocks_ps[pid] = int(qty or 0)
                except ValueError:
                    stocks_ps[pid] = 0

    # 3. Construir informe por producto
    informe    = []
    ps_ids_vistos = set()

    UMBRAL_PRECIO = 0.01  # diferencia mínima de precio que se considera desincronizada

    for prod in productos_bd:
        ps_id = str(prod.get("prestashop_id") or "")
        entrada = {
            "id_local":        prod["id"],
            "nombre_local":    prod["nombre"],
            "referencia":      prod.get("referencia") or "",
            "precio_local":    float(prod.get("precio") or 0),
            "stock_local":     int(prod.get("stock") or 0),
            "prestashop_id":   ps_id or None,
            "diferencias":     [],
            "accion_sugerida": "ok",
        }

        if not ps_id:
            # Producto local sin vincular a PS
            entrada["accion_sugerida"] = "crear"
            entrada["ps_datos"]        = None
        else:
            ps_ids_vistos.add(ps_id)
            ps_prod = ps_por_id.get(ps_id)

            if ps_prod is None:
                # Tiene prestashop_id pero PS ya no lo tiene (borrado en PS)
                entrada["accion_sugerida"] = "revisar"
                entrada["ps_datos"]        = None
                entrada["diferencias"].append("producto no encontrado en PS")
            else:
                entrada["ps_datos"] = {
                    "nombre":     ps_prod["nombre"],
                    "referencia": ps_prod["referencia"],
                    "precio":     ps_prod["precio"],
                    "stock":      stocks_ps.get(ps_id, 0),
                    "activo":     ps_prod["activo"],
                }

                # Comparar campos
                diffs = []

                if abs(entrada["precio_local"] - ps_prod["precio"]) > UMBRAL_PRECIO:
                    diffs.append({
                        "campo":     "precio",
                        "bd_local":  entrada["precio_local"],
                        "prestashop": ps_prod["precio"],
                    })

                if entrada["stock_local"] != stocks_ps.get(ps_id, 0):
                    diffs.append({
                        "campo":     "stock",
                        "bd_local":  entrada["stock_local"],
                        "prestashop": stocks_ps.get(ps_id, 0),
                    })

                if entrada["nombre_local"].strip().lower() != ps_prod["nombre"].strip().lower():
                    diffs.append({
                        "campo":     "nombre",
                        "bd_local":  entrada["nombre_local"],
                        "prestashop": ps_prod["nombre"],
                    })

                if entrada["referencia"].strip() != ps_prod["referencia"].strip():
                    diffs.append({
                        "campo":     "referencia",
                        "bd_local":  entrada["referencia"],
                        "prestashop": ps_prod["referencia"],
                    })

                entrada["diferencias"]     = diffs
                entrada["accion_sugerida"] = "actualizar" if diffs else "ok"

        informe.append(entrada)

    # 4. Productos en PS que no están en BD local
    ids_locales_ps = {str(p.get("prestashop_id")) for p in productos_bd if p.get("prestashop_id")}
    for ps_id, ps_prod in ps_por_id.items():
        if ps_id not in ids_locales_ps:
            informe.append({
                "id_local":        None,
                "nombre_local":    None,
                "referencia":      ps_prod["referencia"],
                "precio_local":    None,
                "stock_local":     None,
                "prestashop_id":   ps_id,
                "ps_datos": {
                    "nombre":     ps_prod["nombre"],
                    "referencia": ps_prod["referencia"],
                    "precio":     ps_prod["precio"],
                    "stock":      stocks_ps.get(ps_id, 0),
                    "activo":     ps_prod["activo"],
                },
                "diferencias":     ["sin registro en BD local"],
                "accion_sugerida": "revisar",
            })

    # 5. Filtrar si solo_diferencias=True
    if solo_diferencias:
        informe = [e for e in informe if e["accion_sugerida"] != "ok"]

    # 6. Resumen ejecutivo
    resumen = {
        "ok":         sum(1 for e in informe if e["accion_sugerida"] == "ok"),
        "crear":      sum(1 for e in informe if e["accion_sugerida"] == "crear"),
        "actualizar": sum(1 for e in informe if e["accion_sugerida"] == "actualizar"),
        "revisar":    sum(1 for e in informe if e["accion_sugerida"] == "revisar"),
    }

    return JSONResponse({
        "resumen":          resumen,
        "total_analizados": len(informe),
        "productos":        informe,
    })


# ══════════════════════════════════════════════════════════════
# TAG: Búsqueda
# ══════════════════════════════════════════════════════════════

@app.get("/search", summary="Búsqueda global en toda la tienda", tags=["Búsqueda"])
async def global_search(
    q:          str = Query(default=None, min_length=1, description="Texto, ID o referencia (nombre, apellido, ref. producto, ref. pedido)"),
    email:      str = Query(default=None, min_length=1, description="Buscar clientes por email (parcial)"),
    referencia: str = Query(default=None, min_length=1, description="Buscar productos o pedidos por referencia exacta"),
    tipo:       str = Query(
        default="all",
        description="Filtrar por tipo: 'productos', 'clientes', 'categorias', 'proveedores', 'pedidos' o 'all'",
    ),
) -> JSONResponse:
    """
    Busca en toda la tienda. Acepta varios modos:

    - `q=camiseta` → busca por nombre en todos los recursos (o solo en el `tipo` indicado)
    - `q=42` → busca por ID
    - `email=juan@tienda.com` → busca clientes por email (parcial)
    - `referencia=CAM-001` → busca productos y pedidos por referencia

    Usa `tipo` para limitar a: `productos`, `clientes`, `categorias`, `proveedores`, `pedidos`.
    """
    if not q and not email and not referencia:
        raise HTTPException(status_code=400, detail="Indica al menos un parámetro: q, email o referencia.")

    tipos_validos = {"all", "productos", "clientes", "categorias", "proveedores", "pedidos"}
    if tipo not in tipos_validos:
        raise HTTPException(
            status_code=400,
            detail=f"Tipo '{tipo}' no válido. Usa: {', '.join(sorted(tipos_validos))}",
        )

    resultados = {}
    termino = q or referencia or email

    if tipo in ("all", "productos"):
        if referencia:
            # Búsqueda por referencia exacta
            prod = ps_client.get_product_by_reference(referencia)
            resultados["productos"] = [prod] if prod else []
        else:
            resultados["productos"] = ps_client.search_products(termino)

    if tipo in ("all", "clientes"):
        if email:
            resultados["clientes"] = ps_client.search_customers(email)
        else:
            resultados["clientes"] = ps_client.search_customers_by_id_or_name(termino)

    if tipo in ("all", "categorias") and not email:
        resultados["categorias"] = ps_client.search_categories_by_id_or_name(termino)

    if tipo in ("all", "proveedores") and not email:
        resultados["proveedores"] = ps_client.search_suppliers_by_id_or_name(termino)

    if tipo in ("all", "pedidos") and not email:
        resultados["pedidos"] = ps_client.search_orders_by_id_or_ref(termino)

    total = sum(len(v) for v in resultados.values())
    return JSONResponse({
        "query":      termino,
        "tipo":       tipo,
        "total":      total,
        "resultados": resultados,
    })


@app.get("/search/products", summary="Buscar productos por nombre o ID", tags=["Búsqueda"])
async def search_products(
    q: str = Query(..., min_length=1, description="Nombre del producto (búsqueda parcial) o ID numérico"),
) -> JSONResponse:
    """
    Busca productos en PrestaShop por nombre o ID.
    - Si introduces un número busca por ID exacto
    - Si introduces texto busca por nombre (búsqueda parcial)
    """
    resultados = ps_client.search_products(q)
    return JSONResponse({"query": q, "total": len(resultados), "productos": resultados})


@app.get("/search/customers", summary="Buscar clientes por nombre, ID o email", tags=["Búsqueda"])
async def search_customers(
    q:     str = Query(default=None, min_length=1, description="Nombre, apellido o ID numérico"),
    email: str = Query(default=None, min_length=1, description="Email (búsqueda parcial)"),
) -> JSONResponse:
    """
    Busca clientes en PrestaShop. Puedes usar:
    - `q=Juan` → busca por nombre o apellido
    - `q=42` → busca por ID exacto
    - `email=pub@prestashop.com` → busca por email (parcial)
    """
    if not q and not email:
        raise HTTPException(status_code=400, detail="Indica al menos q o email.")
    if email:
        resultados = ps_client.search_customers(email)
    else:
        resultados = ps_client.search_customers_by_id_or_name(q)
    query = email or q
    return JSONResponse({"query": query, "total": len(resultados), "clientes": resultados})


@app.get("/search/categories", summary="Buscar categorías por nombre o ID", tags=["Búsqueda"])
async def search_categories(
    q: str = Query(..., min_length=1, description="Nombre de la categoría (búsqueda parcial) o ID numérico"),
) -> JSONResponse:
    """
    Busca categorías en PrestaShop por nombre o ID.
    """
    resultados = ps_client.search_categories_by_id_or_name(q)
    return JSONResponse({"query": q, "total": len(resultados), "categorias": resultados})


@app.get("/search/suppliers", summary="Buscar proveedores por nombre o ID", tags=["Búsqueda"])
async def search_suppliers(
    q: str = Query(..., min_length=1, description="Nombre del proveedor (búsqueda parcial) o ID numérico"),
) -> JSONResponse:
    """
    Busca proveedores en PrestaShop por nombre o ID.
    """
    resultados = ps_client.search_suppliers_by_id_or_name(q)
    return JSONResponse({"query": q, "total": len(resultados), "proveedores": resultados})


@app.get("/search/orders", summary="Buscar pedidos por referencia o ID", tags=["Búsqueda"])
async def search_orders(
    q: str = Query(..., min_length=1, description="Referencia del pedido (ej: BGFZYHKF1) o ID numérico"),
) -> JSONResponse:
    """
    Busca pedidos en PrestaShop por referencia o ID.
    - Si introduces un número busca por ID exacto
    - Si introduces texto busca por referencia (búsqueda parcial)
    """
    resultados = ps_client.search_orders_by_id_or_ref(q)
    return JSONResponse({"query": q, "total": len(resultados), "pedidos": resultados})


# ══════════════════════════════════════════════════════════════
# TAG: Scheduler — control de jobs de monitorización
# ══════════════════════════════════════════════════════════════
#
# Endpoints para consultar y controlar los jobs en background:
#   GET  http://localhost:8000/scheduler/status       → estado de todos los jobs
#   POST http://localhost:8000/scheduler/run/low-stock      → ejecutar ahora
#   POST http://localhost:8000/scheduler/run/pending-orders → ejecutar ahora
#   PUT  http://localhost:8000/scheduler/config/low-stock   → cambiar intervalo/umbral
#   PUT  http://localhost:8000/scheduler/config/pending-orders → cambiar intervalo
# ──────────────────────────────────────────────────────────────


@app.get(
    "/scheduler/status",
    summary="Estado de los jobs de monitorización",
    tags=["Scheduler"],
)
async def scheduler_status() -> JSONResponse:
    """
    Devuelve el estado actual de todos los jobs del scheduler:
    si están activos, cuándo se ejecutaron por última vez y cuántos
    resultados encontraron.

    Ejemplo: `http://localhost:8000/scheduler/status`
    """
    jobs = {}
    for job_id, cfg in _scheduler_jobs_config.items():
        apsjob = _scheduler.get_job(job_id)
        jobs[job_id] = {
            **cfg,
            "next_run": apsjob.next_run_time.isoformat() if apsjob and apsjob.next_run_time else None,
            "scheduler_running": _scheduler.running,
        }
    return JSONResponse({"scheduler_running": _scheduler.running, "jobs": jobs})


@app.post(
    "/scheduler/run/low-stock",
    summary="Ejecutar ahora el job de stock bajo",
    tags=["Scheduler"],
)
async def run_job_low_stock() -> JSONResponse:
    """
    Fuerza la ejecución inmediata del job de stock bajo sin esperar al intervalo.

    Útil para probar desde Postman que el job funciona correctamente.

    Ejemplo: `http://localhost:8000/scheduler/run/low-stock`
    """
    _job_check_low_stock()
    cfg = _scheduler_jobs_config["low_stock"]
    return JSONResponse({
        "status":    "executed",
        "job":       "low_stock",
        "last_run":  cfg["last_run"],
        "found":     cfg["last_count"],
    })


@app.post(
    "/scheduler/run/pending-orders",
    summary="Ejecutar ahora el job de pedidos pendientes",
    tags=["Scheduler"],
)
async def run_job_pending_orders() -> JSONResponse:
    """
    Fuerza la ejecución inmediata del job de pedidos pendientes sin esperar al intervalo.

    Ejemplo: `http://localhost:8000/scheduler/run/pending-orders`
    """
    _job_check_pending_orders()
    cfg = _scheduler_jobs_config["pending_orders"]
    return JSONResponse({
        "status":   "executed",
        "job":      "pending_orders",
        "last_run": cfg["last_run"],
        "found":    cfg["last_count"],
    })


class SchedulerConfigLowStock(BaseModel):
    interval:  Optional[int] = None    # minutos entre ejecuciones
    threshold: Optional[int] = None    # umbral de stock
    enabled:   Optional[bool] = None   # activar/desactivar


class SchedulerConfigPendingOrders(BaseModel):
    interval: Optional[int]  = None
    enabled:  Optional[bool] = None


@app.put(
    "/scheduler/config/low-stock",
    summary="Configurar el job de stock bajo",
    tags=["Scheduler"],
)
async def config_job_low_stock(body: SchedulerConfigLowStock) -> JSONResponse:
    """
    Modifica la configuración del job de stock bajo en caliente (sin reiniciar).

    - `interval` — minutos entre ejecuciones (mín. 1, máx. 1440)
    - `threshold` — nuevo umbral de stock (mín. 0, máx. 1000)
    - `enabled` — `true` para activar, `false` para pausar

    Ejemplo: `http://localhost:8000/scheduler/config/low-stock`
    """
    cfg = _scheduler_jobs_config["low_stock"]
    if body.enabled is not None:
        cfg["enabled"] = body.enabled
    if body.threshold is not None:
        cfg["threshold"] = max(0, min(1000, body.threshold))
    if body.interval is not None:
        cfg["interval"] = max(1, min(1440, body.interval))
        job = _scheduler.get_job("low_stock")
        if job:
            job.reschedule(trigger=IntervalTrigger(minutes=cfg["interval"]))
    logger.info("SCHEDULER | low_stock config actualizada: {cfg}", cfg=cfg)
    return JSONResponse({"status": "updated", "config": cfg})


@app.put(
    "/scheduler/config/pending-orders",
    summary="Configurar el job de pedidos pendientes",
    tags=["Scheduler"],
)
async def config_job_pending_orders(body: SchedulerConfigPendingOrders) -> JSONResponse:
    """
    Modifica la configuración del job de pedidos pendientes en caliente.

    - `interval` — minutos entre ejecuciones (mín. 1, máx. 1440)
    - `enabled` — `true` para activar, `false` para pausar

    Ejemplo: `http://localhost:8000/scheduler/config/pending-orders`
    """
    cfg = _scheduler_jobs_config["pending_orders"]
    if body.enabled is not None:
        cfg["enabled"] = body.enabled
    if body.interval is not None:
        cfg["interval"] = max(1, min(1440, body.interval))
        job = _scheduler.get_job("pending_orders")
        if job:
            job.reschedule(trigger=IntervalTrigger(minutes=cfg["interval"]))
    logger.info("SCHEDULER | pending_orders config actualizada: {cfg}", cfg=cfg)
    return JSONResponse({"status": "updated", "config": cfg})


# ══════════════════════════════════════════════════════════════
# TAG: Reports — Informes y resúmenes de ventas
# ══════════════════════════════════════════════════════════════


@app.get(
    "/reports/daily-sales",
    summary="Resumen de ventas del día anterior",
    tags=["Reports"],
)
async def get_daily_sales_report() -> JSONResponse:
    """
    Devuelve un resumen de las ventas del día anterior:
    - Total de pedidos
    - Facturación total
    - Ticket medio
    - Producto más vendido
    - Número de clientes nuevos

    Diseñado para ser consumido por n8n cada mañana via Schedule Trigger.

    Ejemplo: GET http://localhost:8000/reports/daily-sales
    """
    try:
        conn = db_handler._get_connection()
        cur  = conn.cursor(dictionary=True)
        prefix = "40ovr_"

        # ── Pedidos del día anterior ──────────────────────────
        cur.execute(f"""
            SELECT
                COUNT(*)                        AS total_pedidos,
                COALESCE(SUM(total_paid), 0)    AS facturacion_total,
                COALESCE(AVG(total_paid), 0)    AS ticket_medio
            FROM {prefix}orders
            WHERE DATE(date_add) = CURDATE() - INTERVAL 1 DAY
            AND current_state NOT IN (6, 7, 8)
        """)
        ventas = cur.fetchone()

        # ── Producto más vendido ──────────────────────────────
        cur.execute(f"""
            SELECT
                pl.name         AS nombre,
                p.reference     AS referencia,
                SUM(od.product_quantity) AS unidades_vendidas,
                SUM(od.total_price_tax_incl) AS total_generado
            FROM {prefix}order_detail od
            JOIN {prefix}orders o ON o.id_order = od.id_order
            JOIN {prefix}product p ON p.id_product = od.product_id
            JOIN {prefix}product_lang pl ON pl.id_product = od.product_id
                AND pl.id_lang = (SELECT value FROM {prefix}configuration WHERE name = 'PS_LANG_DEFAULT' LIMIT 1)
            WHERE DATE(o.date_add) = CURDATE() - INTERVAL 1 DAY
            AND o.current_state NOT IN (6, 7, 8)
            GROUP BY od.product_id
            ORDER BY unidades_vendidas DESC
            LIMIT 1
        """)
        top_producto = cur.fetchone()

        # ── Clientes nuevos ───────────────────────────────────
        cur.execute(f"""
            SELECT COUNT(*) AS clientes_nuevos
            FROM {prefix}customer
            WHERE DATE(date_add) = CURDATE() - INTERVAL 1 DAY
            AND active = 1
        """)
        nuevos = cur.fetchone()

        # ── Pedidos por estado ────────────────────────────────
        cur.execute(f"""
            SELECT
                osl.name    AS estado,
                COUNT(*)    AS total
            FROM {prefix}orders o
            JOIN {prefix}order_state_lang osl ON osl.id_order_state = o.current_state
                AND osl.id_lang = (SELECT value FROM {prefix}configuration WHERE name = 'PS_LANG_DEFAULT' LIMIT 1)
            WHERE DATE(o.date_add) = CURDATE() - INTERVAL 1 DAY
            GROUP BY o.current_state
            ORDER BY total DESC
        """)
        por_estado = cur.fetchall()

        cur.close()
        conn.close()

        from datetime import date, timedelta
        ayer = (date.today() - timedelta(days=1)).isoformat()

        return JSONResponse({
            "fecha":            ayer,
            "total_pedidos":    int(ventas["total_pedidos"]),
            "facturacion_total": round(float(ventas["facturacion_total"]), 2),
            "ticket_medio":     round(float(ventas["ticket_medio"]), 2),
            "clientes_nuevos":  int(nuevos["clientes_nuevos"]),
            "top_producto": {
                "nombre":           top_producto["nombre"] if top_producto else None,
                "referencia":       top_producto["referencia"] if top_producto else None,
                "unidades_vendidas": int(top_producto["unidades_vendidas"]) if top_producto else 0,
                "total_generado":   round(float(top_producto["total_generado"]), 2) if top_producto else 0,
            },
            "por_estado": [
                {"estado": e["estado"], "total": int(e["total"])}
                for e in por_estado
            ],
        })

    except Exception as exc:
        logger.error("REPORTS | daily-sales | Error: {exc}", exc=exc)
        raise HTTPException(status_code=500, detail=f"Error al generar el informe: {str(exc)}")


# ──────────────────────────────────────────────────────────────
# Entrypoint
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import asyncio as _asyncio
    import os as _os
    import sys as _sys

    _os.makedirs(str(_LOG_DIR), exist_ok=True)

    # Forzar UTF-8 para evitar UnicodeEncodeError con caracteres especiales en Windows
    if hasattr(_sys.stdout, 'reconfigure'):
        _sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(_sys.stderr, 'reconfigure'):
        _sys.stderr.reconfigure(encoding='utf-8', errors='replace')

    # WindowsSelectorEventLoop — requerido para mysql-connector use_pure=True
    # El ProactorEventLoop (default en Windows) causa access violation en la extensión C
    if _sys.platform == "win32":
        _asyncio.set_event_loop_policy(_asyncio.WindowsSelectorEventLoopPolicy())

    async def _main():
        _config = uvicorn.Config(
            app,
            host=settings.app_host,
            port=settings.app_port,
            log_level="info",
        )
        _server = uvicorn.Server(_config)
        await _server.serve()

    _asyncio.run(_main())
