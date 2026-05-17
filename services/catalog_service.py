# ============================================================
#  services/catalog_service.py — Servicio de sincronización de catálogo
#  Orquesta la sincronización entre la BD local y PrestaShop 9
# ============================================================

import os
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from config import settings
from core.ps_client import PrestashopClient
from database.db_handler import DatabaseHandler


@dataclass
class SyncResult:
    """
    Resultado detallado de una sincronización completa del catálogo.
    Permite conocer exactamente qué ocurrió durante el proceso.
    """
    total: int = 0
    creados: int = 0
    stock_actualizados: int = 0
    imagenes_subidas: int = 0
    errores: list[str] = field(default_factory=list)

    @property
    def exitoso(self) -> bool:
        return len(self.errores) == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_productos": self.total,
            "productos_creados": self.creados,
            "stocks_actualizados": self.stock_actualizados,
            "imagenes_subidas": self.imagenes_subidas,
            "errores": self.errores,
            "exitoso": self.exitoso,
        }


class CatalogService:
    """
    Servicio que orquesta la sincronización del catálogo de productos
    entre la base de datos local y la tienda PrestaShop 9.

    Proceso por cada producto:
      1. Resolver categoría (buscar o crear en PS con jerarquía correcta)
      2. Resolver proveedor (buscar o crear en PS)
      3. Si el producto NO está vinculado a PS → crearlo y guardar el ID
      4. Actualizar stock en PS
      5. Subir imagen si existe en disco
    """

    # IDs fijos de las categorías raíz ya existentes en PrestaShop
    # (se configuran una sola vez en la tienda y no cambian)
    _CATEGORY_ROOT_IDS: dict[str, str] = {
        "Ropa": "3",
        "Accesorios": "6",
        "Arte": "9",
    }

    # Jerarquía: subcategoría → categoría raíz
    _CATEGORY_HIERARCHY: dict[str, str] = {
        "Hombre": "Ropa",
        "Mujer": "Ropa",
        "Accesorios para el hogar": "Accesorios",
        "Papelería": "Accesorios",
    }

    def __init__(self, ps_client: PrestashopClient, db_handler: DatabaseHandler) -> None:
        self.ps = ps_client
        self.db = db_handler

    def _resolver_categoria(
        self, nombre_cat: str, cats_cache: dict[str, str]
    ) -> str:
        """
        Devuelve el ID de la categoría en PrestaShop, creándola si es necesario.
        Usa el caché para evitar llamadas redundantes a la API de PS.
        """
        if nombre_cat in cats_cache:
            return cats_cache[nombre_cat]

        nombre_padre = self._CATEGORY_HIERARCHY.get(nombre_cat)
        id_padre = cats_cache.get(nombre_padre, "2") if nombre_padre else "2"
        id_cat = self.ps.handle_resource("categories", nombre_cat, parent_id=id_padre)
        cats_cache[nombre_cat] = id_cat
        return id_cat

    def _resolver_proveedor(
        self, nombre_supp: str, supps_cache: dict[str, str]
    ) -> str:
        """
        Devuelve el ID del proveedor en PrestaShop, creándolo si es necesario.
        Usa el caché para evitar llamadas redundantes a la API de PS.
        """
        if not nombre_supp:
            return "0"
        if nombre_supp in supps_cache:
            return supps_cache[nombre_supp]

        id_supp = self.ps.handle_resource("suppliers", nombre_supp)
        supps_cache[nombre_supp] = id_supp
        return id_supp

    def _sincronizar_producto(
        self,
        producto: dict[str, Any],
        id_categoria: str,
        id_proveedor: str,
        resultado: SyncResult,
    ) -> str | None:
        """
        Crea el producto en PS si no está vinculado. Devuelve el ID de PS.
        El ID puede venir del registro local (ya vinculado) o ser recién creado.
        """
        id_ps = producto.get("prestashop_id")
        if id_ps:
            return str(id_ps)

        # Producto nuevo: crear en PrestaShop
        datos = {
            "name": producto["nombre"],
            "price": producto["precio"],
            "reference": producto["referencia"],
            "id_category": id_categoria,
            "id_supplier": id_proveedor,
        }
        nuevo_id = self.ps.create_product(datos)
        if not nuevo_id:
            msg = (
                f"No se pudo crear el producto ID local {producto['id']} "
                f"({producto.get('nombre', 'sin nombre')})"
            )
            logger.error(msg)
            resultado.errores.append(msg)
            return None

        vinculado = self.db.guardar_vinculacion(producto["id"], nuevo_id)
        if not vinculado:
            msg = (
                f"Producto PS {nuevo_id} creado pero no se pudo "
                f"guardar la vinculación local (ID {producto['id']})"
            )
            logger.warning(msg)
            resultado.errores.append(msg)

        resultado.creados += 1
        return nuevo_id

    def sincronizar_todo(self) -> SyncResult:
        """
        Ejecuta la sincronización completa del catálogo.

        Recorre todos los productos activos de la BD local y los sincroniza
        con PrestaShop: crea los que faltan, actualiza stock y sube imágenes.

        Devuelve un SyncResult con el resumen detallado del proceso.
        """
        resultado = SyncResult()
        productos = self.db.obtener_datos_completos()
        resultado.total = len(productos)

        if not productos:
            logger.warning("No se encontraron productos activos en la BD local.")
            return resultado

        logger.info("Iniciando sincronización de {n} productos...", n=resultado.total)

        # Cachés en memoria para evitar llamadas repetidas a la API de PS
        cats_cache: dict[str, str] = self._CATEGORY_ROOT_IDS.copy()
        supps_cache: dict[str, str] = {}

        for producto in productos:
            nombre_log = producto.get("nombre", f"ID {producto['id']}")
            try:
                # 1. Resolver categoría
                id_categoria = self._resolver_categoria(
                    producto.get("nombre_categoria") or "General", cats_cache
                )

                # 2. Resolver proveedor
                id_proveedor = self._resolver_proveedor(
                    producto.get("nombre_proveedor") or "", supps_cache
                )

                # 3. Crear o recuperar el ID de PS
                id_ps = self._sincronizar_producto(
                    producto, id_categoria, id_proveedor, resultado
                )
                if not id_ps:
                    continue  # Error ya registrado en resultado.errores

                # 4. Actualizar stock
                stock_ok = self.ps.update_stock(id_ps, int(producto.get("stock", 0)))
                if stock_ok:
                    resultado.stock_actualizados += 1
                else:
                    resultado.errores.append(
                        f"No se pudo actualizar stock del producto PS ID {id_ps}"
                    )

                # 5. Subir imagen si existe
                referencia = producto.get("referencia", "")
                if referencia:
                    ruta_img = os.path.join(settings.img_path, f"{referencia}.jpg")
                    if os.path.exists(ruta_img):
                        img_ok = self.ps.upload_image(id_ps, ruta_img)
                        if img_ok:
                            resultado.imagenes_subidas += 1

                logger.info(
                    "✓ Sincronizado: {nombre} (PS ID: {id})",
                    nombre=nombre_log, id=id_ps,
                )

            except Exception as exc:
                # Captura genérica para que un producto con error no detenga
                # la sincronización del resto del catálogo
                msg = f"Error inesperado en producto '{nombre_log}': {exc}"
                logger.exception(msg)
                resultado.errores.append(msg)

        logger.info(
            "Sincronización finalizada. "
            "Creados: {c} | Stock: {s} | Imágenes: {i} | Errores: {e}",
            c=resultado.creados,
            s=resultado.stock_actualizados,
            i=resultado.imagenes_subidas,
            e=len(resultado.errores),
        )
        return resultado

    def sincronizar_producto_unico(self, id_local: int) -> SyncResult:
        """
        Sincroniza un único producto por su ID local.
        Útil para forzar la sincronización de un producto concreto
        sin lanzar la sincronización completa del catálogo.
        """
        resultado = SyncResult()
        producto = self.db.obtener_producto_por_id(id_local)

        if not producto:
            msg = f"Producto con ID local {id_local} no encontrado o no activo."
            logger.warning(msg)
            resultado.errores.append(msg)
            return resultado

        resultado.total = 1
        cats_cache: dict[str, str] = self._CATEGORY_ROOT_IDS.copy()
        supps_cache: dict[str, str] = {}

        id_categoria = self._resolver_categoria("General", cats_cache)
        id_proveedor = self._resolver_proveedor(
            producto.get("nombre_proveedor") or "", supps_cache
        )
        id_ps = self._sincronizar_producto(producto, id_categoria, id_proveedor, resultado)

        if id_ps:
            stock_ok = self.ps.update_stock(id_ps, int(producto.get("stock", 0)))
            if stock_ok:
                resultado.stock_actualizados += 1

        return resultado
