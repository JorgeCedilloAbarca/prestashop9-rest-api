# ============================================================
#  database/db_handler.py — Capa de acceso a datos MySQL
#  Usa un pool de conexiones para eficiencia y thread-safety
# ============================================================

from typing import Any, Optional

import mysql.connector
from mysql.connector.pooling import MySQLConnectionPool
from loguru import logger

from config import settings
from core.resilience import mysql_circuit


class DatabaseHandler:
    """
    Gestiona el acceso a la base de datos MySQL mediante un pool
    de conexiones reutilizables.

    El pool se inicializa de forma lazy — si MySQL no está disponible
    al arrancar, el microservicio sigue funcionando y reintenta
    la conexión en cada operación.
    """

    _POOL_NAME = "ps_pool"
    _POOL_SIZE = 5

    def __init__(self) -> None:
        # Pool completamente lazy — NO se conecta al arrancar
        # Se inicializa en la primera operación que lo necesite
        self._pool: Optional[MySQLConnectionPool] = None
        logger.info(
            "DatabaseHandler inicializado (pool lazy, db={db})",
            db=settings.db_name,
        )

    def _init_pool(self) -> None:
        """
        Inicializa el pool de conexiones bajo demanda.
        Si falla, registra warning y deja _pool en None.
        """
        try:
            self._pool = MySQLConnectionPool(
                pool_name=self._POOL_NAME,
                pool_size=self._POOL_SIZE,
                pool_reset_session=True,
                connect_timeout=5,
                use_pure=True,
                **settings.db_config,
            )
            logger.info(
                "Pool de conexiones MySQL iniciado (size={size}, db={db})",
                size=self._POOL_SIZE,
                db=settings.db_name,
            )
        except mysql.connector.Error as exc:
            logger.warning(
                "⚠ Pool MySQL no disponible: {exc}. Se reintentará.",
                exc=exc,
            )
            self._pool = None

    def _ensure_pool(self) -> None:
        """Crea el pool si no existe todavía."""
        if self._pool is None:
            self._init_pool()

    def _get_connection(self) -> mysql.connector.connection.MySQLConnection:
        """
        Obtiene una conexión del pool.
        Comprueba el circuit breaker de MySQL antes de intentarlo.
        """
        self._ensure_pool()

        if self._pool is None:
            raise mysql.connector.Error("Pool de MySQL no disponible.")

        if not mysql_circuit.should_allow_request():
            status = mysql_circuit.status()
            raise mysql.connector.Error(
                f"Circuit breaker MySQL ABIERTO. "
                f"Recuperación en {status.get('recovery_in','?')}s "
                f"({status['failure_count']} fallos recientes)."
            )
        try:
            conn = self._pool.get_connection()
            mysql_circuit.record_success()
            return conn
        except mysql.connector.Error as exc:
            mysql_circuit.record_failure()
            self._pool = None  # Forzar reinicio del pool en el próximo intento
            raise

    # ------------------------------------------------------------------
    # Consultas de lectura
    # ------------------------------------------------------------------

    def obtener_datos_completos(self) -> list[dict[str, Any]]:
        """
        Devuelve todos los productos activos con sus datos completos:
        nombre, precio, referencia, categoría, proveedor y stock.
        """
        query = """
            SELECT
                p.id_product          AS id,
                pl.name               AS nombre,
                p.price               AS precio,
                p.reference           AS referencia,
                p.prestashop_id       AS prestashop_id,
                cl.name               AS nombre_categoria,
                s.name                AS nombre_proveedor,
                COALESCE(sa.quantity, 0) AS stock
            FROM       40ovr_product p
            LEFT JOIN  40ovr_product_lang pl
                       ON  p.id_product = pl.id_product
                       AND pl.id_lang = 1
            LEFT JOIN  40ovr_category_lang cl
                       ON  p.id_category_default = cl.id_category
                       AND cl.id_lang = 1
            LEFT JOIN  40ovr_supplier s
                       ON  p.id_supplier = s.id_supplier
            LEFT JOIN  40ovr_stock_available sa
                       ON  p.id_product = sa.id_product
                       AND sa.id_product_attribute = 0
            WHERE p.active = 1
            ORDER BY p.id_product ASC
        """
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor(dictionary=True)
            cursor.execute(query)
            resultados = cursor.fetchall()
            cursor.close()
            logger.debug(
                "Consulta datos_completos: {n} productos encontrados.",
                n=len(resultados),
            )
            return resultados
        except mysql.connector.Error as exc:
            logger.error("Error en obtener_datos_completos: {exc}", exc=exc)
            return []
        finally:
            if conn and conn.is_connected():
                conn.close()

    def obtener_producto_por_id(self, id_local: int) -> Optional[dict[str, Any]]:
        """Devuelve los datos de un producto concreto por su ID local."""
        query = """
            SELECT
                p.id_product    AS id,
                pl.name         AS nombre,
                p.price         AS precio,
                p.reference     AS referencia,
                p.prestashop_id AS prestashop_id,
                COALESCE(sa.quantity, 0) AS stock
            FROM       40ovr_product p
            LEFT JOIN  40ovr_product_lang pl
                       ON  p.id_product = pl.id_product
                       AND pl.id_lang = 1
            LEFT JOIN  40ovr_stock_available sa
                       ON  p.id_product = sa.id_product
                       AND sa.id_product_attribute = 0
            WHERE p.id_product = %s
              AND p.active = 1
            LIMIT 1
        """
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor(dictionary=True)
            cursor.execute(query, (id_local,))
            resultado = cursor.fetchone()
            cursor.close()
            return resultado
        except mysql.connector.Error as exc:
            logger.error(
                "Error en obtener_producto_por_id(id={id}): {exc}",
                id=id_local, exc=exc,
            )
            return None
        finally:
            if conn and conn.is_connected():
                conn.close()

    def guardar_vinculacion(self, id_local: int, id_ps: str) -> bool:
        """Actualiza el campo prestashop_id del producto local."""
        query = "UPDATE 40ovr_product SET prestashop_id = %s WHERE id_product = %s"
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(query, (id_ps, id_local))
            conn.commit()
            filas = cursor.rowcount
            cursor.close()
            if filas == 1:
                logger.debug(
                    "Vinculación guardada: local ID {local} → PS ID {ps}",
                    local=id_local, ps=id_ps,
                )
                return True
            logger.warning(
                "guardar_vinculacion afectó {n} filas (esperado 1). "
                "local_id={local}, ps_id={ps}",
                n=filas, local=id_local, ps=id_ps,
            )
            return False
        except mysql.connector.Error as exc:
            logger.error(
                "Error en guardar_vinculacion(local={local}, ps={ps}): {exc}",
                local=id_local, ps=id_ps, exc=exc,
            )
            return False
        finally:
            if conn and conn.is_connected():
                conn.close()

    def obtener_producto_por_referencia(self, referencia: str) -> Optional[dict[str, Any]]:
        """Devuelve los datos de un producto concreto por su referencia."""
        query = """
            SELECT
                p.id_product    AS id,
                pl.name         AS nombre,
                p.price         AS precio,
                p.reference     AS referencia,
                p.prestashop_id AS prestashop_id,
                COALESCE(sa.quantity, 0) AS stock
            FROM       40ovr_product p
            LEFT JOIN  40ovr_product_lang pl
                       ON  p.id_product = pl.id_product
                       AND pl.id_lang = 1
            LEFT JOIN  40ovr_stock_available sa
                       ON  p.id_product = sa.id_product
                       AND sa.id_product_attribute = 0
            WHERE p.reference = %s
              AND p.active = 1
            LIMIT 1
        """
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor(dictionary=True)
            cursor.execute(query, (referencia,))
            resultado = cursor.fetchone()
            cursor.close()
            return resultado
        except mysql.connector.Error as exc:
            logger.error(
                "Error en obtener_producto_por_referencia(ref={ref}): {exc}",
                ref=referencia, exc=exc,
            )
            return None
        finally:
            if conn and conn.is_connected():
                conn.close()

    def obtener_cliente_local(self, email: str) -> Optional[dict[str, Any]]:
        """Busca en la BD local si existe un cliente por email."""
        query = """
            SELECT
                c.id_customer AS id,
                c.firstname   AS nombre,
                c.lastname    AS apellido,
                c.email       AS email,
                c.date_add    AS registro
            FROM 40ovr_customer c
            WHERE c.email = %s AND c.deleted = 0
            LIMIT 1
        """
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor(dictionary=True)
            cursor.execute(query, (email,))
            resultado = cursor.fetchone()
            cursor.close()
            return resultado
        except mysql.connector.Error as exc:
            logger.error("Error en obtener_cliente_local(email={email}): {exc}", email=email, exc=exc)
            return None
        finally:
            if conn and conn.is_connected():
                conn.close()

    def obtener_resumen_pedidos(self) -> dict[str, Any]:
        """Devuelve un resumen agregado de pedidos desde la BD local."""
        query = """
            SELECT
                COUNT(*)            AS total_pedidos,
                COALESCE(SUM(total_paid), 0) AS importe_total,
                current_state       AS estado,
                COUNT(*)            AS cantidad
            FROM 40ovr_orders
            GROUP BY current_state
        """
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor(dictionary=True)
            cursor.execute(query)
            rows = cursor.fetchall()
            cursor.close()
            for r in rows:
                if r.get("importe_total"):
                    r["importe_total"] = float(r["importe_total"])
            return {"por_estado": rows}
        except mysql.connector.Error as exc:
            logger.error("Error en obtener_resumen_pedidos: {exc}", exc=exc)
            return {"por_estado": []}
        finally:
            if conn and conn.is_connected():
                conn.close()

    def health_check(self) -> bool:
        """Comprueba que la conexión a la base de datos está activa."""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            cursor.fetchone()
            cursor.close()
            return True
        except mysql.connector.Error:
            return False
        finally:
            if conn and conn.is_connected():
                conn.close()
