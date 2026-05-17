# ============================================================
#  core/ps_client.py — Cliente para el Webservice de PrestaShop 9
#  Gestiona toda la comunicación HTTP/XML con la API REST de PS
# ============================================================

import io
import os
import re
import time
import xml.etree.ElementTree as ET
from typing import Optional

import requests
from loguru import logger

from core.resilience import prestashop_circuit, CircuitOpenError
from core.image_processor import process_image, validate_image, process_bytes


class PrestashopClient:
    # Timeout por defecto para todas las peticiones al webservice
    DEFAULT_TIMEOUT = (5, 25)  # (connect_timeout, read_timeout) en segundos

    def __init__(self, api_key: str, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key  = api_key
        self.session  = requests.Session()
        self.session.auth = (api_key, "")
        self.session.headers.update({"Output-Format": "XML"})
        # Adapter con retries y connect timeout corto
        from requests.adapters import HTTPAdapter
        adapter = HTTPAdapter(max_retries=0)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    # ------------------------------------------------------------------
    # Utilidades internas
    # ------------------------------------------------------------------

    def _slugify(self, texto: str) -> str:
        if not texto:
            return "sin-nombre"
        texto = texto.lower().strip()
        replacements = {
            "á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u",
            "ü": "u", "ñ": "n", "ç": "c",
        }
        for original, replacement in replacements.items():
            texto = texto.replace(original, replacement)
        texto = re.sub(r"[^a-z0-9\s-]", "", texto)
        texto = re.sub(r"[\s-]+", "-", texto).strip("-")
        return texto or "sin-nombre"

    # Códigos HTTP que son errores transitorios y merecen reintento
    _RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
    # Máximo de reintentos automáticos en _request
    _MAX_RETRIES = 3
    # Espera base en segundos (se duplica en cada reintento)
    _RETRY_BASE_DELAY = 1.0

    def _request(
        self,
        method: str,
        resource: str,
        data: Optional[bytes] = None,
        files: Optional[dict] = None,
    ) -> Optional[ET.Element]:
        """
        Realiza una petición HTTP al webservice con:
          - Circuit breaker: rechaza inmediatamente si PS acumula demasiados fallos
          - Reintentos automáticos con backoff exponencial en errores transitorios
          - Logging detallado diferenciado por tipo de error
        """
        # ── Circuit breaker ──────────────────────────────────
        if not prestashop_circuit.should_allow_request():
            status = prestashop_circuit.status()
            logger.error(
                "Circuit breaker ABIERTO para '{name}'. "
                "Recuperación en {r}s aprox. ({n} fallos recientes).",
                name=status["name"],
                r=status.get("recovery_in", "?"),
                n=status["failure_count"],
            )
            return None

        url = f"{self.base_url}/{resource}"
        last_exception = None

        for attempt in range(self._MAX_RETRIES + 1):
            try:
                response = self.session.request(
                    method, url, data=data, files=files, timeout=self.DEFAULT_TIMEOUT
                )

                # ── Éxito ────────────────────────────────────
                if response.status_code in (200, 201):
                    prestashop_circuit.record_success()
                    if method == "DELETE":
                        return ET.Element("ok")
                    try:
                        return ET.fromstring(response.content)
                    except ET.ParseError as exc:
                        logger.error(
                            "Error parseando XML de PS API ({method} /{resource}): {exc}",
                            method=method, resource=resource, exc=exc,
                        )
                        prestashop_circuit.record_failure()
                        return None

                # ── Error transitorio → reintento ────────────
                if response.status_code in self._RETRYABLE_STATUSES and attempt < self._MAX_RETRIES:
                    delay = min(self._RETRY_BASE_DELAY * (2 ** attempt), 16.0)
                    logger.warning(
                        "PS API {method} /{resource} → HTTP {status} "
                        "(intento {n}/{total}) — reintentando en {delay}s",
                        method=method, resource=resource,
                        status=response.status_code,
                        n=attempt + 1, total=self._MAX_RETRIES + 1,
                        delay=delay,
                    )
                    time.sleep(delay)
                    continue

                # ── Error no reintentable (4xx, etc.) ────────
                if response.status_code in self._RETRYABLE_STATUSES:
                    # Agotados los reintentos
                    prestashop_circuit.record_failure()
                    logger.error(
                        "PS API {method} /{resource} → HTTP {status} "
                        "tras {n} reintentos: {body}",
                        method=method, resource=resource,
                        status=response.status_code,
                        n=self._MAX_RETRIES,
                        body=response.text[:2000],
                    )
                else:
                    # 4xx — error del cliente, no reintentamos
                    logger.error(
                        "PS API {method} /{resource} → HTTP {status}: {body}",
                        method=method, resource=resource,
                        status=response.status_code,
                        body=response.text[:2000],
                    )
                return None

            except requests.exceptions.Timeout:
                last_exception = "Timeout"
                if attempt < self._MAX_RETRIES:
                    delay = min(self._RETRY_BASE_DELAY * (2 ** attempt), 16.0)
                    logger.warning(
                        "Timeout en PS API {method} /{resource} "
                        "(intento {n}/{total}) — reintentando en {delay}s",
                        method=method, resource=resource,
                        n=attempt + 1, total=self._MAX_RETRIES + 1,
                        delay=delay,
                    )
                    time.sleep(delay)
                else:
                    prestashop_circuit.record_failure()
                    logger.error(
                        "Timeout en PS API {method} /{resource} tras {n} reintentos.",
                        method=method, resource=resource, n=self._MAX_RETRIES,
                    )
                    return None

            except requests.exceptions.ConnectionError as exc:
                last_exception = exc
                if attempt < self._MAX_RETRIES:
                    delay = min(self._RETRY_BASE_DELAY * (2 ** attempt), 16.0)
                    logger.warning(
                        "Error de conexión PS API {method} /{resource} "
                        "(intento {n}/{total}) — reintentando en {delay}s: {exc}",
                        method=method, resource=resource,
                        n=attempt + 1, total=self._MAX_RETRIES + 1,
                        delay=delay, exc=exc,
                    )
                    time.sleep(delay)
                else:
                    prestashop_circuit.record_failure()
                    logger.error(
                        "Error de conexión PS API {method} /{resource} "
                        "tras {n} reintentos: {exc}",
                        method=method, resource=resource,
                        n=self._MAX_RETRIES, exc=exc,
                    )
                    return None

        return None

    def _get_first_available_id(self, resource: str) -> Optional[str]:
        res = self._request("GET", f"{resource}?display=[id]&sort=[id_ASC]")
        if res is None:
            return None
        ids_existentes = {int(node.text) for node in res.findall(".//id") if node.text}
        for candidate in range(10, 10_000):
            if candidate not in ids_existentes:
                return str(candidate)
        return None

    def handle_resource(self, resource: str, name: str, parent_id: str = "2") -> str:
        if not name:
            name = "General"
        display = "[id,id_parent]" if resource == "categories" else "[id]"
        existing = self._request("GET", f"{resource}?filter[name]={name}&display={display}")
        if existing is not None:
            singular = resource[:-1]
            found = existing.find(f".//{singular}/id") or existing.find(".//id")
            if found is not None:
                if resource == "categories":
                    actual_parent = existing.find(f".//{singular}/id_parent")
                    if actual_parent is not None and actual_parent.text == str(parent_id):
                        return found.text
                else:
                    return found.text

        root = ET.Element("prestashop")
        sub = ET.SubElement(root, resource[:-1])
        id_libre = self._get_first_available_id(resource)
        if id_libre:
            ET.SubElement(sub, "id").text = id_libre

        if resource == "categories":
            name_el = ET.SubElement(sub, "name")
            ET.SubElement(name_el, "language", {"id": "1"}).text = name
            link_el = ET.SubElement(sub, "link_rewrite")
            ET.SubElement(link_el, "language", {"id": "1"}).text = self._slugify(name)
            ET.SubElement(sub, "active").text = "1"
            ET.SubElement(sub, "id_parent").text = str(parent_id)
        else:
            ET.SubElement(sub, "name").text = name
            ET.SubElement(sub, "active").text = "1"

        res = self._request("POST", resource, data=ET.tostring(root, encoding="utf-8"))
        if res is not None:
            id_node = res.find(".//id")
            new_id = id_node.text if id_node is not None else "2"
            logger.info("Creado '{name}' en '{resource}' con ID {id}", name=name, resource=resource, id=new_id)
            return new_id
        logger.error("No se pudo crear '{name}' en '{resource}'.", name=name, resource=resource)
        return "2"

    # ------------------------------------------------------------------
    # Categorías
    # ------------------------------------------------------------------

    def get_categories(self) -> list:
        res = self._request("GET", "categories?display=[id,name,id_parent,active]")
        if res is None:
            return []
        categorias = []
        for cat in res.findall(".//category"):
            name_node = cat.find(".//language")
            categorias.append({
                "id": cat.findtext("id"),
                "nombre": name_node.text if name_node is not None else cat.findtext("name", ""),
                "id_parent": cat.findtext("id_parent"),
                "active": cat.findtext("active"),
            })
        return categorias

    def create_category(self, name: str, parent_id: str = "2", active: bool = True) -> Optional[str]:
        root = ET.Element("prestashop")
        cat  = ET.SubElement(root, "category")
        name_el = ET.SubElement(cat, "name")
        ET.SubElement(name_el, "language", {"id": "1"}).text = name
        link_el = ET.SubElement(cat, "link_rewrite")
        ET.SubElement(link_el, "language", {"id": "1"}).text = self._slugify(name)
        ET.SubElement(cat, "active").text    = "1" if active else "0"
        ET.SubElement(cat, "id_parent").text = str(parent_id)
        res = self._request("POST", "categories", data=ET.tostring(root, encoding="utf-8"))
        if res is not None:
            id_node = res.find(".//id")
            if id_node is not None:
                logger.info("Categoría '{name}' creada con ID {id}", name=name, id=id_node.text)
                return id_node.text
        logger.error("No se pudo crear la categoría '{name}'.", name=name)
        return None

    def delete_category(self, category_id: str) -> bool:
        res = self._request("DELETE", f"categories/{category_id}")
        success = res is not None
        if success:
            logger.info("Categoría ID {id} eliminada.", id=category_id)
        else:
            logger.error("No se pudo eliminar la categoría ID {id}.", id=category_id)
        return success

    # ------------------------------------------------------------------
    # Proveedores
    # ------------------------------------------------------------------

    def get_suppliers(self) -> list:
        res = self._request("GET", "suppliers?display=[id,name,active]")
        if res is None:
            return []
        proveedores = []
        for sup in res.findall(".//supplier"):
            proveedores.append({
                "id": sup.findtext("id"),
                "nombre": sup.findtext("name"),
                "active": sup.findtext("active"),
            })
        return proveedores

    def create_supplier(self, name: str) -> Optional[str]:
        """Crea un proveedor en PS. PS9 asigna el ID automáticamente."""
        root = ET.Element("prestashop")
        sup  = ET.SubElement(root, "supplier")
        ET.SubElement(sup, "name").text   = name
        ET.SubElement(sup, "active").text = "1"
        res = self._request("POST", "suppliers", data=ET.tostring(root, encoding="utf-8"))
        if res is not None:
            id_node = res.find(".//id")
            if id_node is not None:
                logger.info("Proveedor '{name}' creado con ID {id}", name=name, id=id_node.text)
                return id_node.text
        logger.error("No se pudo crear el proveedor '{name}'.", name=name)
        return None

    def delete_supplier(self, supplier_id: str) -> bool:
        res = self._request("DELETE", f"suppliers/{supplier_id}")
        success = res is not None
        if success:
            logger.info("Proveedor ID {id} eliminado.", id=supplier_id)
        else:
            logger.error("No se pudo eliminar el proveedor ID {id}.", id=supplier_id)
        return success

    # ------------------------------------------------------------------
    # Productos
    # ------------------------------------------------------------------

    def create_product(self, data: dict) -> Optional[str]:
        """
        Crea un producto en PrestaShop con todos los campos disponibles.
        Campos soportados en data:
          name, price, reference, id_category, id_supplier, active,
          description, description_short, weight, ean13, minimal_quantity,
          stock (se actualiza tras crear).
        """
        id_categoria = str(data.get("id_category", "2"))

        root = ET.Element("prestashop")
        p    = ET.SubElement(root, "product")

        # Campos básicos obligatorios
        campos = {
            "active":              str(int(data.get("active", True))),
            "visibility":          "both",
            "state":               "1",
            "id_shop_default":     "1",
            "price":               str(data.get("price", "0.000000")),
            "reference":           str(data.get("reference", "")),
            "id_category_default": id_categoria,
            "id_supplier":         str(data.get("id_supplier", "0")),
        }
        # Campos opcionales simples
        if data.get("weight") is not None:
            campos["weight"] = str(data["weight"])
        if data.get("ean13"):
            campos["ean13"] = str(data["ean13"])
        if data.get("minimal_quantity") is not None:
            campos["minimal_quantity"] = str(int(data["minimal_quantity"]))

        for key, val in campos.items():
            ET.SubElement(p, key).text = val

        # Nombre y slug
        name_el = ET.SubElement(p, "name")
        ET.SubElement(name_el, "language", {"id": "1"}).text = data.get("name", "Nuevo producto")
        link_el = ET.SubElement(p, "link_rewrite")
        ET.SubElement(link_el, "language", {"id": "1"}).text = self._slugify(data.get("name", "nuevo-producto"))

        # Descripción larga
        if data.get("description"):
            desc_el = ET.SubElement(p, "description")
            ET.SubElement(desc_el, "language", {"id": "1"}).text = data["description"]

        # Descripción corta
        if data.get("description_short"):
            desc_s_el = ET.SubElement(p, "description_short")
            ET.SubElement(desc_s_el, "language", {"id": "1"}).text = data["description_short"]

        # Asociación de categoría (necesario para que aparezca en el frontend)
        assoc   = ET.SubElement(p, "associations")
        cats_el = ET.SubElement(assoc, "categories")
        cat_el  = ET.SubElement(cats_el, "category")
        ET.SubElement(cat_el, "id").text = id_categoria

        res = self._request("POST", "products", data=ET.tostring(root, encoding="utf-8"))
        if res is None:
            logger.error("No se pudo crear el producto '{name}'.", name=data.get("name"))
            return None

        id_node = res.find(".//id")
        if id_node is None:
            logger.error("PS no devolvió ID al crear '{name}'.", name=data.get("name"))
            return None

        ps_id = id_node.text
        logger.info("Producto '{name}' creado con ID {id}", name=data.get("name"), id=ps_id)

        # Actualizar stock inicial si se proporcionó
        if data.get("stock") is not None:
            self.update_stock(ps_id, int(data["stock"]))

        return ps_id

    def update_product(self, product_id: str, data: dict) -> bool:
        """
        Actualiza los campos indicados de un producto en PS.
        Solo se envían los campos presentes en data — los ausentes no se tocan.
        Campos soportados: name, price, reference, active, id_category_default,
        id_supplier, weight, ean13, minimal_quantity, description, description_short.
        """
        root = ET.Element("prestashop")
        p    = ET.SubElement(root, "product")
        ET.SubElement(p, "id").text = str(product_id)

        # Campos simples
        campo_map = {
            "price":               str,
            "reference":           str,
            "active":              lambda v: str(int(v)),
            "id_category_default": str,
            "id_supplier":         str,
            "weight":              str,
            "ean13":               str,
            "minimal_quantity":    lambda v: str(int(v)),
            "id_tax_rules_group":  str,
        }
        for campo, transform in campo_map.items():
            if campo in data and data[campo] is not None:
                ET.SubElement(p, campo).text = transform(data[campo])

        # Nombre
        if "name" in data and data["name"]:
            name_el = ET.SubElement(p, "name")
            ET.SubElement(name_el, "language", {"id": "1"}).text = data["name"]
            link_el = ET.SubElement(p, "link_rewrite")
            ET.SubElement(link_el, "language", {"id": "1"}).text = self._slugify(data["name"])

        # Descripción larga
        if "description" in data and data["description"] is not None:
            desc_el = ET.SubElement(p, "description")
            ET.SubElement(desc_el, "language", {"id": "1"}).text = data["description"]

        # Descripción corta
        if "description_short" in data and data["description_short"] is not None:
            desc_s = ET.SubElement(p, "description_short")
            ET.SubElement(desc_s, "language", {"id": "1"}).text = data["description_short"]

        res = self._request("PUT", f"products/{product_id}", data=ET.tostring(root, encoding="utf-8"))
        success = res is not None
        if success:
            logger.info("Producto ID {id} actualizado.", id=product_id)
            # Actualizar stock si se indicó
            if "stock" in data and data["stock"] is not None:
                self.update_stock(str(product_id), int(data["stock"]))
        else:
            logger.error("No se pudo actualizar el producto ID {id}.", id=product_id)
        return success

    def update_description(self, product_id: str, description: str) -> bool:
        """
        Actualiza únicamente la descripción de un producto existente en PrestaShop.
        La descripción puede contener HTML básico (negrita, listas, párrafos).
        """
        root = ET.Element("prestashop")
        p    = ET.SubElement(root, "product")
        ET.SubElement(p, "id").text = str(product_id)
        desc_el = ET.SubElement(p, "description")
        ET.SubElement(desc_el, "language", {"id": "1"}).text = description

        res = self._request(
            "PUT", f"products/{product_id}",
            data=ET.tostring(root, encoding="utf-8"),
        )
        success = res is not None
        if success:
            logger.info("Descripción del producto ID {id} actualizada.", id=product_id)
        else:
            logger.error("No se pudo actualizar la descripción del producto ID {id}.", id=product_id)
        return success

    def get_product_description(self, product_id: str) -> Optional[str]:
        """
        Devuelve la descripción actual de un producto.
        Devuelve None si el producto no existe o no tiene descripción.
        """
        res = self._request("GET", f"products/{product_id}?display=[id,description]")
        if res is None:
            return None
        desc_node = res.find(".//description/language")
        if desc_node is None:
            desc_node = res.find(".//description")
        return desc_node.text if desc_node is not None else ""

    def delete_product(self, product_id: str) -> bool:
        res = self._request("DELETE", f"products/{product_id}")
        success = res is not None
        if success:
            logger.info("Producto ID {id} eliminado de PS.", id=product_id)
        else:
            logger.error("No se pudo eliminar el producto ID {id} de PS.", id=product_id)
        return success

    def get_product_by_reference(self, reference: str) -> Optional[dict]:
        res = self._request("GET", f"products?filter[reference]={reference}&display=[id,reference,active]")
        if res is None:
            return None
        product = res.find(".//product")
        if product is None:
            return None
        return {
            "id": product.findtext("id"),
            "reference": product.findtext("reference"),
            "active": product.findtext("active"),
        }

    # ------------------------------------------------------------------
    # Stock
    # ------------------------------------------------------------------

    def get_stock(self, product_id: str) -> Optional[dict]:
        res = self._request(
            "GET",
            f"stock_availables?filter[id_product]={product_id}"
            f"&filter[id_product_attribute]=0&display=[id,id_product,quantity]"
        )
        if res is None:
            return None
        stock = res.find(".//stock_available")
        if stock is None:
            return None
        return {
            "id_stock": stock.findtext("id"),
            "id_product": stock.findtext("id_product"),
            "quantity": int(stock.findtext("quantity") or 0),
        }

    def update_stock(self, product_id: str, quantity: int) -> bool:
        res_list = self._request("GET", f"stock_availables?filter[id_product]={product_id}&display=[id]")
        if res_list is None:
            logger.error("No se encontró stock_available para producto ID {id}.", id=product_id)
            return False
        stock_node = res_list.find(".//stock_available")
        if stock_node is None:
            logger.error("stock_available vacío para producto ID {id}.", id=product_id)
            return False
        id_stock = stock_node.findtext("id")
        if not id_stock:
            logger.error("No se pudo obtener id de stock_available para producto ID {id}.", id=product_id)
            return False
        qty_clean = max(0, int(float(quantity or 0)))
        root = ET.Element("prestashop")
        sa = ET.SubElement(root, "stock_available")
        ET.SubElement(sa, "id").text = id_stock
        ET.SubElement(sa, "id_product").text = str(product_id)
        ET.SubElement(sa, "quantity").text = str(qty_clean)
        ET.SubElement(sa, "id_product_attribute").text = "0"
        ET.SubElement(sa, "depends_on_stock").text = "0"
        ET.SubElement(sa, "out_of_stock").text = "0"
        res = self._request("PUT", f"stock_availables/{id_stock}", data=ET.tostring(root, encoding="utf-8"))
        success = res is not None
        if success:
            logger.debug("Stock de producto ID {id} actualizado a {qty}.", id=product_id, qty=qty_clean)
        else:
            logger.error("No se pudo actualizar el stock del producto ID {id}.", id=product_id)
        return success


    # ------------------------------------------------------------------
    # Stock bajo
    # ------------------------------------------------------------------

    def get_low_stock_products(self, threshold: int = 5) -> list:
        """
        Devuelve los productos con stock igual o por debajo del umbral indicado.
        Incluye nombre, referencia, precio y stock actual.
        Útil para detectar productos que necesitan reposición.
        """
        # Obtener todos los stocks
        res_stocks = self._request(
            "GET",
            f"stock_availables?filter[id_product_attribute]=0"
            f"&filter[quantity]=[0,{threshold}]&display=[id,id_product,quantity]"
        )
        if res_stocks is None:
            return []

        # Recoger IDs de productos con stock bajo
        stocks_bajos = {}
        for s in res_stocks.findall(".//stock_available"):
            pid = s.findtext("id_product")
            qty = int(s.findtext("quantity") or 0)
            if pid and qty <= threshold:
                stocks_bajos[pid] = qty

        if not stocks_bajos:
            return []

        # Obtener nombres y referencias de esos productos
        ids_str = "|".join(stocks_bajos.keys())
        res_prods = self._request(
            "GET",
            f"products?filter[id]=[{ids_str}]"
            f"&display=[id,name,reference,price,active]"
        )

        resultado = []
        if res_prods is not None:
            for p in res_prods.findall(".//product"):
                pid       = p.findtext("id")
                name_node = p.find(".//name/language")
                resultado.append({
                    "id":        pid,
                    "nombre":    name_node.text if name_node is not None else "",
                    "referencia": p.findtext("reference") or "",
                    "precio":    p.findtext("price") or "0",
                    "activo":    p.findtext("active") == "1",
                    "stock":     stocks_bajos.get(pid, 0),
                    "alerta":    "sin_stock" if stocks_bajos.get(pid, 0) == 0 else "stock_bajo",
                })
        # Ordenar: primero sin stock, luego por stock ascendente
        resultado.sort(key=lambda x: x["stock"])
        return resultado

    # ------------------------------------------------------------------
    # Devoluciones
    # ------------------------------------------------------------------

    def get_returns(self, limit: int = 50) -> list:
        """
        Devuelve todas las devoluciones de la tienda ordenadas por fecha.
        PS gestiona las devoluciones mediante order_returns.
        """
        res = self._request(
            "GET",
            f"order_returns?display=full&sort=[date_add_DESC]&limit={limit}"
        )
        if res is None:
            return []

        estados_map = {
            "1": "En espera de confirmación",
            "2": "En espera de paquete",
            "3": "Paquete recibido",
            "4": "Devuelto",
            "5": "Cerrado",
        }

        devoluciones = []
        for r in res.findall(".//order_return"):
            estado_id = r.findtext("state") or ""
            devoluciones.append({
                "id":              r.findtext("id"),
                "id_pedido":       r.findtext("id_order"),
                "id_cliente":      r.findtext("id_customer"),
                "id_estado":       estado_id,
                "estado":          estados_map.get(estado_id, f"Estado {estado_id}"),
                "fecha":           r.findtext("date_add"),
                "pregunta":        r.findtext("question"),
            })
        return devoluciones

    def get_return(self, return_id: str) -> Optional[dict]:
        """Devuelve el detalle de una devolución concreta con sus líneas de producto."""
        res = self._request("GET", f"order_returns/{return_id}")
        if res is None:
            return None
        r = res.find(".//order_return")
        if r is None:
            return None

        estados_map = {
            "1": "En espera de confirmación",
            "2": "En espera de paquete",
            "3": "Paquete recibido",
            "4": "Devuelto",
            "5": "Cerrado",
        }

        # Líneas de devolución
        lineas = []
        for det in r.findall(".//order_return_detail"):
            lineas.append({
                "id_producto":   det.findtext("id_order_product"),
                "cantidad":      det.findtext("product_quantity"),
                "id_motivo":     det.findtext("id_customization"),
            })

        estado_id = r.findtext("state") or ""
        return {
            "id":         r.findtext("id"),
            "id_pedido":  r.findtext("id_order"),
            "id_cliente": r.findtext("id_customer"),
            "id_estado":  estado_id,
            "estado":     estados_map.get(estado_id, f"Estado {estado_id}"),
            "fecha":      r.findtext("date_add"),
            "pregunta":   r.findtext("question"),
            "lineas":     lineas,
        }

    def update_return_state(self, return_id: str, state_id: str) -> bool:
        """
        Actualiza el estado de una devolución.
        Estados: 1=Espera confirmación, 2=Espera paquete, 3=Recibido, 4=Devuelto, 5=Cerrado
        """
        res_get = self._request("GET", f"order_returns/{return_id}")
        if res_get is None:
            return False
        r = res_get.find(".//order_return")
        if r is None:
            return False

        state_el = r.find("state")
        if state_el is not None:
            state_el.text = str(state_id)
        else:
            ET.SubElement(r, "state").text = str(state_id)

        root = ET.Element("prestashop")
        root.append(r)
        res = self._request("PUT", f"order_returns/{return_id}", data=ET.tostring(root, encoding="utf-8"))
        success = res is not None
        if success:
            logger.info("Devolución ID {id} actualizada a estado {s}.", id=return_id, s=state_id)
        else:
            logger.error("No se pudo actualizar devolución ID {id}.", id=return_id)
        return success

    # ------------------------------------------------------------------
    # Reconciliación de inventario
    # ------------------------------------------------------------------

    def get_products_snapshot(self) -> dict:
        """
        Devuelve un snapshot del catálogo de PS indexado por ID.
        Campos: nombre, precio, stock, activo, referencia.
        Usado por el reconciliador para comparar con la BD local.
        """
        res = self._request(
            "GET",
            "products?display=[id,name,reference,price,active]"
        )
        if res is None:
            return {}

        snapshot = {}
        for p in res.findall(".//product"):
            pid = p.findtext("id")
            if not pid:
                continue
            name_node = p.find(".//name/language")

            # Obtener stock
            stock_res = self._request(
                "GET",
                f"stock_availables?filter[id_product]={pid}"
                f"&filter[id_product_attribute]=0&display=[quantity]"
            )
            stock = 0
            if stock_res is not None:
                qty = stock_res.findtext(".//quantity")
                try:
                    stock = int(qty or 0)
                except ValueError:
                    stock = 0

            snapshot[pid] = {
                "nombre":     name_node.text if name_node is not None else "",
                "referencia": p.findtext("reference") or "",
                "precio":     float(p.findtext("price") or 0),
                "activo":     p.findtext("active") == "1",
                "stock":      stock,
            }
        return snapshot

    # ------------------------------------------------------------------
    # Búsqueda por nombre o ID
    # ------------------------------------------------------------------

    def search_products(self, query: str) -> list:
        """
        Busca productos en PS por ID (si query es numérico) o por nombre/referencia.
        PS9 no soporta búsqueda parcial por nombre via filtro, por lo que
        se traen todos los productos y se filtra localmente en Python.
        """
        if query.isdigit():
            res = self._request(
                "GET",
                f"products/{query}?display=[id,name,reference,price,active]"
            )
            if res is None:
                return []
            p = res.find(".//product")
            if p is None:
                return []
            name_node = p.find(".//name/language")
            return [{
                "id":         p.findtext("id"),
                "nombre":     name_node.text if name_node is not None else "",
                "referencia": p.findtext("reference"),
                "precio":     p.findtext("price"),
                "activo":     p.findtext("active"),
            }]
        else:
            # Traer todos y filtrar localmente — PS9 no soporta filter[name] parcial fiable
            q_lower = query.lower()
            res = self._request(
                "GET",
                "products?display=[id,name,reference,price,active]"
            )
            if res is None:
                return []
            resultados = []
            for p in res.findall(".//product"):
                name_node = p.find(".//name/language")
                nombre    = name_node.text if name_node is not None else ""
                referencia = p.findtext("reference") or ""
                # Filtrar por nombre o referencia (case-insensitive)
                if q_lower in nombre.lower() or q_lower in referencia.lower():
                    resultados.append({
                        "id":         p.findtext("id"),
                        "nombre":     nombre,
                        "referencia": referencia,
                        "precio":     p.findtext("price"),
                        "activo":     p.findtext("active"),
                    })
            return resultados

    def search_customers_by_id_or_name(self, query: str) -> list:
        """
        Busca clientes por ID (si query es numérico) o por nombre/apellido.
        """
        if query.isdigit():
            res = self._request("GET", f"customers/{query}?display=full")
            if res is None:
                return []
            c = res.find(".//customer")
            if c is None:
                return []
            return [{
                "id":       c.findtext("id"),
                "nombre":   f"{c.findtext('firstname','')} {c.findtext('lastname','')}".strip(),
                "email":    c.findtext("email"),
                "activo":   c.findtext("active"),
                "registro": c.findtext("date_add"),
            }]
        else:
            # Filtrar localmente — PS9 no soporta filter parcial por nombre fiable
            q_lower = query.lower()
            res = self._request("GET", "customers?display=full")
            if res is None:
                return []
            resultados = []
            for c in res.findall(".//customer"):
                firstname = c.findtext("firstname") or ""
                lastname  = c.findtext("lastname") or ""
                nombre_completo = f"{firstname} {lastname}".strip().lower()
                if q_lower in nombre_completo or q_lower in firstname.lower() or q_lower in lastname.lower():
                    resultados.append({
                        "id":       c.findtext("id"),
                        "nombre":   f"{firstname} {lastname}".strip(),
                        "email":    c.findtext("email"),
                        "activo":   c.findtext("active"),
                        "registro": c.findtext("date_add"),
                    })
            return resultados

    def search_categories_by_id_or_name(self, query: str) -> list:
        """
        Busca categorías por ID (si query es numérico) o por nombre.
        """
        if query.isdigit():
            res = self._request(
                "GET",
                f"categories/{query}?display=[id,name,id_parent,active]"
            )
            if res is None:
                return []
            c = res.find(".//category")
            if c is None:
                return []
            name_node = c.find(".//name/language")
            return [{
                "id":        c.findtext("id"),
                "nombre":    name_node.text if name_node is not None else "",
                "id_parent": c.findtext("id_parent"),
                "activo":    c.findtext("active"),
            }]
        else:
            q_lower = query.lower()
            res = self._request("GET", "categories?display=[id,name,id_parent,active]")
            if res is None:
                return []
            resultados = []
            for c in res.findall(".//category"):
                name_node = c.find(".//language")
                nombre = name_node.text if name_node is not None else ""
                if q_lower in nombre.lower():
                    resultados.append({
                        "id":        c.findtext("id"),
                        "nombre":    nombre,
                        "id_parent": c.findtext("id_parent"),
                        "activo":    c.findtext("active"),
                    })
            return resultados

    def search_suppliers_by_id_or_name(self, query: str) -> list:
        """
        Busca proveedores por ID (si query es numérico) o por nombre.
        """
        if query.isdigit():
            res = self._request("GET", f"suppliers/{query}?display=full")
            if res is None:
                return []
            s = res.find(".//supplier")
            if s is None:
                return []
            return [{
                "id":     s.findtext("id"),
                "nombre": s.findtext("name"),
                "activo": s.findtext("active"),
            }]
        else:
            q_lower = query.lower()
            res = self._request("GET", "suppliers?display=[id,name,active]")
            if res is None:
                return []
            resultados = []
            for s in res.findall(".//supplier"):
                nombre = s.findtext("name") or ""
                if q_lower in nombre.lower():
                    resultados.append({
                        "id":     s.findtext("id"),
                        "nombre": nombre,
                        "activo": s.findtext("active"),
                    })
            return resultados

    def search_orders_by_id_or_ref(self, query: str) -> list:
        """
        Busca pedidos por ID (si query es numérico) o por referencia.
        """
        if query.isdigit():
            res = self._request(
                "GET",
                f"orders/{query}?display=[id,reference,id_customer,total_paid,current_state,date_add]"
            )
            if res is None:
                return []
            o = res.find(".//order")
            if o is None:
                return []
            return [{
                "id":           o.findtext("id"),
                "referencia":   o.findtext("reference"),
                "id_cliente":   o.findtext("id_customer"),
                "total_pagado": o.findtext("total_paid"),
                "id_estado":    o.findtext("current_state"),
                "fecha":        o.findtext("date_add"),
            }]
        else:
            q_lower = query.lower()
            res = self._request(
                "GET",
                "orders?display=[id,reference,id_customer,total_paid,current_state,date_add]"
                "&sort=[date_add_DESC]"
            )
            if res is None:
                return []
            resultados = []
            for o in res.findall(".//order"):
                referencia = o.findtext("reference") or ""
                if q_lower in referencia.lower():
                    resultados.append({
                        "id":           o.findtext("id"),
                        "referencia":   referencia,
                        "id_cliente":   o.findtext("id_customer"),
                        "total_pagado": o.findtext("total_paid"),
                        "id_estado":    o.findtext("current_state"),
                        "fecha":        o.findtext("date_add"),
                    })
            return resultados

    # ------------------------------------------------------------------
    # Estadísticas de la tienda
    # ------------------------------------------------------------------

    def get_orders_stats(self) -> dict:
        """
        Devuelve estadísticas agregadas de pedidos:
        total, importe acumulado y desglose por estado.
        """
        res = self._request("GET", "orders?display=[id,total_paid,current_state,date_add]")
        if res is None:
            return {}

        pedidos = res.findall(".//order")
        total_pedidos   = len(pedidos)
        total_importe   = 0.0
        por_estado: dict = {}

        for o in pedidos:
            try:
                total_importe += float(o.findtext("total_paid") or 0)
            except ValueError:
                pass
            estado = o.findtext("current_state") or "?"
            por_estado[estado] = por_estado.get(estado, 0) + 1

        # Enriquecer con nombres de estado
        estados = self.get_order_states()
        mapa_estados = {e["id"]: e["nombre"] for e in estados}
        por_estado_nombrado = {
            mapa_estados.get(k, f"Estado {k}"): v
            for k, v in por_estado.items()
        }

        return {
            "total_pedidos":  total_pedidos,
            "importe_total":  round(total_importe, 2),
            "por_estado":     por_estado_nombrado,
        }

    def get_products_stats(self) -> dict:
        """
        Devuelve estadísticas del catálogo:
        total de productos, activos, sin stock y precio medio.
        """
        res_products = self._request("GET", "products?display=[id,active,price]")
        if res_products is None:
            return {}

        productos = res_products.findall(".//product")
        total     = len(productos)
        activos   = sum(1 for p in productos if p.findtext("active") == "1")
        precios   = []
        for p in productos:
            try:
                precios.append(float(p.findtext("price") or 0))
            except ValueError:
                pass

        # Productos sin stock
        res_stock = self._request(
            "GET",
            "stock_availables?filter[quantity]=0&filter[id_product_attribute]=0&display=[id]"
        )
        sin_stock = 0
        if res_stock is not None:
            sin_stock = len(res_stock.findall(".//stock_available"))

        return {
            "total_productos": total,
            "activos":         activos,
            "inactivos":       total - activos,
            "sin_stock":       sin_stock,
            "precio_medio":    round(sum(precios) / len(precios), 2) if precios else 0.0,
            "precio_minimo":   round(min(precios), 2) if precios else 0.0,
            "precio_maximo":   round(max(precios), 2) if precios else 0.0,
        }

    def get_customers_stats(self) -> dict:
        """
        Devuelve estadísticas de clientes:
        total, activos y registros del último mes.
        """
        from datetime import datetime, timedelta, timezone
        res = self._request("GET", "customers?display=[id,active,date_add]")
        if res is None:
            return {}

        clientes   = res.findall(".//customer")
        total      = len(clientes)
        activos    = sum(1 for c in clientes if c.findtext("active") == "1")

        # Registros en los últimos 30 días
        hace_30    = datetime.now(timezone.utc) - timedelta(days=30)
        recientes  = 0
        for c in clientes:
            fecha_str = c.findtext("date_add") or ""
            try:
                fecha = datetime.fromisoformat(fecha_str.replace(" ", "T"))
                if fecha.replace(tzinfo=timezone.utc) >= hace_30:
                    recientes += 1
            except ValueError:
                pass

        return {
            "total_clientes":    total,
            "activos":           activos,
            "inactivos":         total - activos,
            "nuevos_30_dias":    recientes,
        }

    def get_categories_stats(self) -> dict:
        """Devuelve el número total de categorías y cuántas están activas."""
        res = self._request("GET", "categories?display=[id,active]")
        if res is None:
            return {}
        cats    = res.findall(".//category")
        total   = len(cats)
        activas = sum(1 for c in cats if c.findtext("active") == "1")
        return {
            "total_categorias": total,
            "activas":          activas,
            "inactivas":        total - activas,
        }

    # ------------------------------------------------------------------
    # Imágenes
    # ------------------------------------------------------------------

    def upload_image(self, product_id: str, image_path: str) -> bool:
        """
        Sube una imagen al producto en PrestaShop.
        Procesa la imagen antes de subirla: convierte a JPEG, redimensiona
        si es necesario y optimiza el tamaño.
        """
        if not os.path.exists(image_path):
            logger.warning("Imagen no encontrada en ruta '{path}'.", path=image_path)
            return False
        url = f"images/products/{product_id}"
        try:
            # Procesar imagen antes de subir
            processed = process_image(image_path)
            if processed is None:
                logger.error("No se pudo procesar la imagen '{path}'.", path=image_path)
                return False
            filename = os.path.splitext(os.path.basename(image_path))[0] + ".jpg"
            files   = {"image": (filename, processed, "image/jpeg")}
            res     = self._request("POST", url, files=files)
            success = res is not None
            if success:
                logger.debug("Imagen subida para producto ID {id}: {path}", id=product_id, path=image_path)
            return success
        except OSError as exc:
            logger.error("Error leyendo imagen '{path}': {exc}", path=image_path, exc=exc)
            return False

    def get_existing_image_dimensions(self, product_id: str) -> tuple:
        """
        Obtiene las dimensiones de la primera imagen existente de un producto.
        Devuelve (width, height) o (1200, 1200) si no hay imágenes o falla.
        """
        try:
            import io as _io
            from PIL import Image as _Image
            imagenes = self.get_product_images(product_id)
            if not imagenes:
                return (1200, 1200)
            # Descargar la primera imagen usando la sesión autenticada
            first_url = imagenes[0]["url"]
            resp = self.session.get(first_url, timeout=15)
            if resp.status_code == 200:
                img = _Image.open(_io.BytesIO(resp.content))
                logger.debug(
                    "Dimensiones detectadas del producto {id}: {w}x{h}px",
                    id=product_id, w=img.width, h=img.height,
                )
                return (img.width, img.height)
        except Exception as exc:
            logger.warning("No se pudo detectar dimensiones existentes: {e}", e=exc)
        return (1200, 1200)

    def upload_image_bytes(
        self,
        product_id: str,
        image_bytes: bytes,
        filename: str,
        max_width: int = 0,
        max_height: int = 0,
    ) -> bool:
        """
        Sube una imagen al producto desde bytes.
        Si max_width/max_height son 0, detecta las dimensiones de las imágenes
        existentes del producto y las usa para mantener consistencia.
        """
        import io as _io
        from core.image_processor import process_image

        # Determinar dimensiones target
        w, h = max_width, max_height
        if w == 0 or h == 0:
            detected_w, detected_h = self.get_existing_image_dimensions(product_id)
            if w == 0: w = detected_w
            if h == 0: h = detected_h

        # Procesar imagen con las dimensiones correctas
        processed = process_image(image_bytes, max_width=w, max_height=h)
        if processed is None:
            logger.error("No se pudo procesar la imagen '{f}'.", f=filename)
            return False

        clean_name = os.path.splitext(filename)[0] + ".jpg"
        files      = {"image": (clean_name, processed, "image/jpeg")}
        res        = self._request("POST", f"images/products/{product_id}", files=files)
        success    = res is not None

        if success:
            logger.info(
                "Imagen '{f}' subida al producto ID {id} ({w}x{h}px).",
                f=filename, id=product_id, w=w, h=h,
            )
        else:
            logger.error(
                "No se pudo subir la imagen '{f}' al producto ID {id}.",
                f=filename, id=product_id,
            )
        return success

    def get_product_images(self, product_id: str) -> list:
        """
        Devuelve la lista de imágenes asociadas a un producto en PS.
        PS9 devuelve el XML en formatos distintos según la versión — se intentan
        varias estrategias de parseo para ser robusto.
        """
        res = self._request("GET", f"images/products/{product_id}")
        if res is None:
            return []
        imagenes = []
        vistos = set()

        # Estrategia 1: <image id="N"> con atributo id
        for img in res.iter("image"):
            img_id = img.get("id") or img.findtext("id")
            if img_id and img_id not in vistos:
                vistos.add(img_id)
                imagenes.append({
                    "id":  img_id,
                    "url": f"{self.base_url}/images/products/{product_id}/{img_id}",
                })

        # Estrategia 2: <declination id="N"> (algunos endpoints PS9)
        if not imagenes:
            for dec in res.iter("declination"):
                dec_id = dec.get("id") or dec.findtext("id")
                if dec_id and dec_id not in vistos:
                    vistos.add(dec_id)
                    imagenes.append({
                        "id":  dec_id,
                        "url": f"{self.base_url}/images/products/{product_id}/{dec_id}",
                    })
        return imagenes

    def delete_product_image(self, product_id: str, image_id: str) -> bool:
        """Elimina una imagen concreta de un producto en PS."""
        res = self._request("DELETE", f"images/products/{product_id}/{image_id}")
        success = res is not None
        if success:
            logger.info("Imagen ID {img} eliminada del producto ID {prod}.", img=image_id, prod=product_id)
        else:
            logger.error("No se pudo eliminar la imagen ID {img}.", img=image_id)
        return success

    # ------------------------------------------------------------------
    # Pedidos
    # ------------------------------------------------------------------

    def get_orders(self, limit: int = 50, offset: int = 0) -> list:
        """
        Devuelve los pedidos de la tienda ordenados por fecha descendente.
        PS9 no siempre respeta limit/offset en sort combinado, por lo que
        se traen todos y se pagina en Python.
        """
        res = self._request(
            "GET",
            "orders?display=[id,reference,id_customer,total_paid,current_state,date_add]"
            "&sort=[id_DESC]"
        )
        if res is None:
            return []
        pedidos = []
        for o in res.findall(".//order"):
            pedidos.append({
                "id":            o.findtext("id"),
                "referencia":    o.findtext("reference"),
                "id_cliente":    o.findtext("id_customer"),
                "total_pagado":  o.findtext("total_paid"),
                "id_estado":     o.findtext("current_state"),
                "fecha":         o.findtext("date_add"),
            })
        # Paginar en Python
        return pedidos[offset: offset + limit]

    def get_order(self, order_id: str) -> Optional[dict]:
        """
        Devuelve el detalle completo de un pedido: datos del cliente,
        líneas de producto, totales, estado y dirección de envío.
        """
        res = self._request("GET", f"orders/{order_id}")
        if res is None:
            return None
        o = res.find(".//order")
        if o is None:
            return None

        # Líneas de pedido
        lineas = []
        for row in o.findall(".//order_row"):
            lineas.append({
                "id_producto":   row.findtext("product_id"),
                "nombre":        row.findtext("product_name"),
                "referencia":    row.findtext("product_reference"),
                "cantidad":      row.findtext("product_quantity"),
                "precio_unidad": row.findtext("unit_price_tax_incl"),
                "total":         row.findtext("total_price_tax_incl"),
            })

        return {
            "id":               o.findtext("id"),
            "referencia":       o.findtext("reference"),
            "id_cliente":       o.findtext("id_customer"),
            "id_estado":        o.findtext("current_state"),
            "total_pagado":     o.findtext("total_paid"),
            "total_envio":      o.findtext("total_shipping"),
            "total_descuento":  o.findtext("total_discounts"),
            "metodo_pago":      o.findtext("payment"),
            "fecha":            o.findtext("date_add"),
            "fecha_envio":      o.findtext("delivery_date"),
            "lineas":           lineas,
        }

    def update_order_state(self, order_id: str, state_id: str) -> bool:
        """
        Cambia el estado de un pedido.
        Crea un registro en order_history para mantener el historial.
        """
        root = ET.Element("prestashop")
        oh = ET.SubElement(root, "order_history")
        ET.SubElement(oh, "id_order").text = str(order_id)
        ET.SubElement(oh, "id_order_state").text = str(state_id)

        res = self._request("POST", "order_histories", data=ET.tostring(root, encoding="utf-8"))
        success = res is not None
        if success:
            logger.info("Pedido ID {id} → estado {state}.", id=order_id, state=state_id)
        else:
            logger.error("No se pudo cambiar estado del pedido ID {id}.", id=order_id)
        return success

    def get_order_states(self) -> list:
        """Devuelve todos los estados de pedido disponibles en la tienda."""
        res = self._request("GET", "order_states?display=full")
        if res is None:
            return []
        estados = []
        for s in res.findall(".//order_state"):
            # PS9 devuelve el nombre en campo multilingual <name><language id="1">...</language></name>
            name_node = s.find(".//name/language")
            if name_node is None:
                name_node = s.find(".//language")
            nombre = name_node.text if name_node is not None else s.findtext("name", f"Estado {s.findtext('id','?')}")
            estados.append({
                "id":     s.findtext("id"),
                "nombre": nombre,
            })
        return estados

    # ------------------------------------------------------------------
    # Clientes
    # ------------------------------------------------------------------

    def get_customers(self, limit: int = 50, offset: int = 0) -> list:
        """Devuelve la lista de clientes con id, nombre, email y fecha de registro."""
        # display=full para evitar que PS9 devuelva lista vacía con display parcial
        res = self._request(
            "GET",
            f"customers?display=full&sort=[id_DESC]&limit={limit}&offset={offset}"
        )
        if res is None:
            return []
        clientes = []
        for c in res.findall(".//customer"):
            clientes.append({
                "id":       c.findtext("id"),
                "nombre":   f"{c.findtext('firstname', '')} {c.findtext('lastname', '')}".strip(),
                "email":    c.findtext("email", ""),
                "activo":   c.findtext("active"),
                "registro": c.findtext("date_add"),
            })
        return clientes

    def get_customer(self, customer_id: str) -> Optional[dict]:
        """Devuelve los datos completos de un cliente por su ID."""
        res = self._request("GET", f"customers/{customer_id}")
        if res is None:
            return None
        c = res.find(".//customer")
        if c is None:
            return None
        return {
            "id":        c.findtext("id"),
            "nombre":    f"{c.findtext('firstname', '')} {c.findtext('lastname', '')}".strip(),
            "email":     c.findtext("email"),
            "telefono":  c.findtext("phone"),
            "activo":    c.findtext("active"),
            "registro":  c.findtext("date_add"),
            "id_grupo":  c.findtext("id_default_group"),
            "newsletter": c.findtext("newsletter"),
        }

    def search_customers(self, email: str) -> list:
        """
        Busca clientes por email.
        PS9 soporta filter[email] con valor exacto, pero no parcial fiable.
        Se traen todos y se filtra localmente para soportar búsqueda parcial.
        """
        email_lower = email.lower()
        res = self._request("GET", "customers?display=full")
        if res is None:
            return []
        clientes = []
        for c in res.findall(".//customer"):
            c_email = (c.findtext("email") or "").lower()
            if email_lower in c_email:
                clientes.append({
                    "id":       c.findtext("id"),
                    "nombre":   f"{c.findtext('firstname','') } {c.findtext('lastname','')}".strip(),
                    "email":    c.findtext("email"),
                    "activo":   c.findtext("active"),
                    "registro": c.findtext("date_add"),
                })
        return clientes

    def get_customer_orders(self, customer_id: str) -> list:
        """
        Devuelve todos los pedidos de un cliente.
        PS9 a veces ignora filter[id_customer] — se traen todos y se filtra localmente.
        """
        res = self._request(
            "GET",
            "orders?display=[id,reference,id_customer,total_paid,current_state,date_add]&sort=[id_DESC]"
        )
        if res is None:
            return []

        # Mapa de estados para enriquecer la respuesta
        estados = self.get_order_states()
        mapa_estados = {e["id"]: e["nombre"] for e in estados}

        pedidos = []
        for o in res.findall(".//order"):
            # Filtrar por cliente localmente
            if o.findtext("id_customer") != str(customer_id):
                continue
            id_estado = o.findtext("current_state") or ""
            pedidos.append({
                "id":           o.findtext("id"),
                "referencia":   o.findtext("reference"),
                "total_pagado": o.findtext("total_paid"),
                "id_estado":    id_estado,
                "estado":       mapa_estados.get(id_estado, f"Estado {id_estado}"),
                "fecha":        o.findtext("date_add"),
            })
        return pedidos

    # ------------------------------------------------------------------
    # Impuestos
    # ------------------------------------------------------------------

    def get_tax_rules(self) -> list:
        """
        Devuelve todos los grupos de reglas de impuesto (tax_rule_groups).
        Estos son los que se asignan a los productos (ej: IVA 21%, IVA 10%).
        """
        res = self._request("GET", "tax_rule_groups?display=[id,name,active]")
        if res is None:
            return []
        grupos = []
        for t in res.findall(".//tax_rule_group"):
            grupos.append({
                "id":     t.findtext("id"),
                "nombre": t.findtext("name"),
                "activo": t.findtext("active"),
            })
        return grupos

    def get_taxes(self) -> list:
        """Devuelve todos los tipos de impuesto con su porcentaje."""
        res = self._request("GET", "taxes?display=[id,name,rate,active]")
        if res is None:
            return []
        impuestos = []
        for t in res.findall(".//tax"):
            name_node = t.find(".//language")
            impuestos.append({
                "id":         t.findtext("id"),
                "nombre":     name_node.text if name_node is not None else t.findtext("name", ""),
                "porcentaje": t.findtext("rate"),
                "activo":     t.findtext("active"),
            })
        return impuestos

    def create_tax_rule_group(self, name: str) -> Optional[str]:
        """Crea un grupo de reglas de impuesto nuevo. Devuelve su ID o None."""
        root = ET.Element("prestashop")
        trg = ET.SubElement(root, "tax_rule_group")
        ET.SubElement(trg, "name").text = name
        ET.SubElement(trg, "active").text = "1"
        res = self._request("POST", "tax_rule_groups", data=ET.tostring(root, encoding="utf-8"))
        if res is not None:
            id_node = res.find(".//id")
            if id_node is not None:
                logger.info("Grupo impuesto '{name}' creado con ID {id}", name=name, id=id_node.text)
                return id_node.text
        logger.error("No se pudo crear el grupo de impuesto '{name}'.", name=name)
        return None

    def assign_tax_to_product(self, product_id: str, tax_rule_group_id: str) -> bool:
        """
        Asigna un grupo de reglas de impuesto a un producto.
        Obtiene los campos mínimos requeridos por PS9 y construye
        un XML limpio con id_tax_rules_group incluido.
        """
        res_get = self._request(
            "GET",
            f"products/{product_id}?display=[id,name,reference,price,id_category_default,id_supplier,active]"
        )
        if res_get is None:
            logger.error("No se pudo obtener producto ID {id} para asignar impuesto.", id=product_id)
            return False

        p = res_get.find(".//product")
        if p is None:
            logger.error("Producto ID {id} no encontrado.", id=product_id)
            return False

        name_node = p.find(".//name/language")
        nombre    = name_node.text if name_node is not None else "Producto"
        price     = p.findtext("price") or "0"
        reference = p.findtext("reference") or ""
        id_cat    = p.findtext("id_category_default") or "2"
        id_sup    = p.findtext("id_supplier") or "0"
        active    = p.findtext("active") or "1"

        # XML mínimo válido con el impuesto actualizado
        root = ET.Element("prestashop")
        prod = ET.SubElement(root, "product")
        ET.SubElement(prod, "id").text                 = str(product_id)
        ET.SubElement(prod, "price").text              = price
        ET.SubElement(prod, "reference").text          = reference
        ET.SubElement(prod, "id_category_default").text = id_cat
        ET.SubElement(prod, "id_supplier").text        = id_sup
        ET.SubElement(prod, "active").text             = active
        ET.SubElement(prod, "id_tax_rules_group").text = str(tax_rule_group_id)
        name_el = ET.SubElement(prod, "name")
        ET.SubElement(name_el, "language", {"id": "1"}).text = nombre

        res = self._request("PUT", f"products/{product_id}", data=ET.tostring(root, encoding="utf-8"))
        success = res is not None
        if success:
            logger.info("Impuesto {tax} asignado al producto ID {id}.", tax=tax_rule_group_id, id=product_id)
        else:
            logger.error("No se pudo asignar impuesto al producto ID {id}.", id=product_id)
        return success

    # ------------------------------------------------------------------
    # Combinaciones (variantes de producto: tallas, colores, etc.)
    # ------------------------------------------------------------------

    def get_product_combinations(self, product_id: str) -> list:
        """Devuelve todas las combinaciones (variantes) de un producto."""
        res = self._request(
            "GET",
            f"combinations?filter[id_product]={product_id}&display=full"
        )
        if res is None:
            return []
        combinaciones = []
        for c in res.findall(".//combination"):
            combinaciones.append({
                "id":          c.findtext("id"),
                "referencia":  c.findtext("reference"),
                "precio_extra": c.findtext("price"),
                "stock":       c.findtext("quantity"),
                "activo":      c.findtext("active", "1"),
            })
        return combinaciones

    def get_product_attributes(self) -> list:
        """
        Devuelve todos los grupos de atributos disponibles (ej: Talla, Color).
        Cada grupo contiene los valores posibles (ej: S, M, L o Rojo, Azul).
        """
        res = self._request("GET", "product_options?display=full")
        if res is None:
            return []
        grupos = []
        for g in res.findall(".//product_option"):
            name_node = g.find(".//language")
            valores_res = self._request(
                "GET",
                f"product_option_values?filter[id_attribute_group]={g.findtext('id')}&display=[id,name]"
            )
            valores = []
            if valores_res is not None:
                for v in valores_res.findall(".//product_option_value"):
                    vname = v.find(".//language")
                    valores.append({
                        "id":     v.findtext("id"),
                        "nombre": vname.text if vname is not None else "",
                    })
            grupos.append({
                "id":      g.findtext("id"),
                "nombre":  name_node.text if name_node is not None else "",
                "valores": valores,
            })
        return grupos

    def create_combination(self, product_id: str, data: dict) -> Optional[str]:
        """
        Crea una combinación (variante) para un producto.
        data debe incluir: reference, price (precio extra), id_attribute_values (lista de IDs).
        """
        root = ET.Element("prestashop")
        c = ET.SubElement(root, "combination")
        ET.SubElement(c, "id_product").text = str(product_id)
        ET.SubElement(c, "reference").text  = str(data.get("reference", ""))
        ET.SubElement(c, "price").text      = str(data.get("price_extra", "0"))
        ET.SubElement(c, "active").text     = "1"

        if data.get("id_attribute_values"):
            assoc = ET.SubElement(c, "associations")
            attrs = ET.SubElement(assoc, "product_option_values")
            for attr_id in data["id_attribute_values"]:
                pov = ET.SubElement(attrs, "product_option_value")
                ET.SubElement(pov, "id").text = str(attr_id)

        res = self._request("POST", "combinations", data=ET.tostring(root, encoding="utf-8"))
        if res is not None:
            id_node = res.find(".//id")
            if id_node is not None:
                logger.info("Combinación creada para producto ID {id}.", id=product_id)
                return id_node.text
        logger.error("No se pudo crear combinación para producto ID {id}.", id=product_id)
        return None

    def update_combination_stock(self, combination_id: str, quantity: int) -> bool:
        """Actualiza el stock de una combinación concreta."""
        res_list = self._request(
            "GET",
            f"stock_availables?filter[id_product_attribute]={combination_id}&display=[id]"
        )
        if res_list is None:
            return False
        stock_node = res_list.find(".//stock_available")
        if stock_node is None:
            return False
        id_stock = stock_node.findtext("id")
        if not id_stock:
            return False

        root = ET.Element("prestashop")
        sa = ET.SubElement(root, "stock_available")
        ET.SubElement(sa, "id").text = id_stock
        ET.SubElement(sa, "id_product_attribute").text = str(combination_id)
        ET.SubElement(sa, "quantity").text = str(max(0, int(quantity)))
        ET.SubElement(sa, "depends_on_stock").text = "0"
        ET.SubElement(sa, "out_of_stock").text = "0"

        res = self._request("PUT", f"stock_availables/{id_stock}", data=ET.tostring(root, encoding="utf-8"))
        success = res is not None
        if success:
            logger.debug("Stock de combinación ID {id} actualizado a {qty}.", id=combination_id, qty=quantity)
        return success

    # ------------------------------------------------------------------
    # Características de producto (features)
    # ------------------------------------------------------------------

    def get_features(self) -> list:
        """Devuelve todas las características disponibles (ej: Material, Peso, Origen)."""
        res = self._request("GET", "product_features?display=full")
        if res is None:
            return []
        features = []
        for f in res.findall(".//product_feature"):
            name_node = f.find(".//language")
            features.append({
                "id":     f.findtext("id"),
                "nombre": name_node.text if name_node is not None else "",
            })
        return features

    def get_feature_values(self, feature_id: str) -> list:
        """Devuelve los valores posibles de una característica concreta."""
        res = self._request(
            "GET",
            f"product_feature_values?filter[id_feature]={feature_id}&display=full"
        )
        if res is None:
            return []
        valores = []
        for v in res.findall(".//product_feature_value"):
            name_node = v.find(".//language")
            valores.append({
                "id":     v.findtext("id"),
                "valor":  name_node.text if name_node is not None else "",
            })
        return valores

    def assign_feature_to_product(
        self, product_id: str, feature_id: str, feature_value_id: str
    ) -> bool:
        """
        Asigna una característica con un valor concreto a un producto.
        Primero obtiene el producto completo para no sobreescribir datos existentes.
        """
        res = self._request("GET", f"products/{product_id}")
        if res is None:
            logger.error("No se pudo obtener el producto ID {id} para asignar característica.", id=product_id)
            return False

        root = ET.Element("prestashop")
        p = ET.SubElement(root, "product")
        ET.SubElement(p, "id").text = str(product_id)

        assoc = ET.SubElement(p, "associations")
        feats = ET.SubElement(assoc, "product_features")
        feat  = ET.SubElement(feats, "product_feature")
        ET.SubElement(feat, "id").text             = str(feature_id)
        ET.SubElement(feat, "id_feature_value").text = str(feature_value_id)

        res2 = self._request("PUT", f"products/{product_id}", data=ET.tostring(root, encoding="utf-8"))
        success = res2 is not None
        if success:
            logger.info(
                "Característica {fid} = {vid} asignada al producto ID {pid}.",
                fid=feature_id, vid=feature_value_id, pid=product_id,
            )
        else:
            logger.error("No se pudo asignar característica al producto ID {id}.", id=product_id)
        return success

    # ------------------------------------------------------------------
    # Descuentos y reglas de precio
    # ------------------------------------------------------------------

    def get_cart_rules(self, limit: int = 50) -> list:
        """Devuelve los cupones/descuentos (cart rules) activos en la tienda."""
        res = self._request(
            "GET",
            f"cart_rules?display=[id,name,code,reduction_percent,reduction_amount,"
            f"active,date_from,date_to]&limit={limit}"
        )
        if res is None:
            return []
        cupones = []
        for r in res.findall(".//cart_rule"):
            name_node = r.find(".//language")
            cupones.append({
                "id":               r.findtext("id"),
                "nombre":           name_node.text if name_node is not None else r.findtext("name", ""),
                "codigo":           r.findtext("code"),
                "descuento_%":      r.findtext("reduction_percent"),
                "descuento_importe": r.findtext("reduction_amount"),
                "activo":           r.findtext("active"),
                "desde":            r.findtext("date_from"),
                "hasta":            r.findtext("date_to"),
            })
        return cupones

    def create_cart_rule(self, data: dict) -> Optional[str]:
        """
        Crea un cupón de descuento.
        data: name, code, reduction_percent o reduction_amount, date_from, date_to.
        """
        root = ET.Element("prestashop")
        cr = ET.SubElement(root, "cart_rule")

        name_el = ET.SubElement(cr, "name")
        ET.SubElement(name_el, "language", {"id": "1"}).text = data.get("name", "Descuento")

        ET.SubElement(cr, "code").text               = data.get("code", "")
        ET.SubElement(cr, "active").text             = "1"
        ET.SubElement(cr, "reduction_percent").text  = str(data.get("reduction_percent", "0"))
        ET.SubElement(cr, "reduction_amount").text   = str(data.get("reduction_amount", "0"))
        ET.SubElement(cr, "reduction_tax").text      = "1"
        ET.SubElement(cr, "date_from").text          = data.get("date_from", "2024-01-01 00:00:00")
        ET.SubElement(cr, "date_to").text            = data.get("date_to",   "2099-12-31 23:59:59")
        ET.SubElement(cr, "quantity").text           = str(data.get("quantity", "1000"))
        ET.SubElement(cr, "quantity_per_user").text  = str(data.get("quantity_per_user", "1"))

        res = self._request("POST", "cart_rules", data=ET.tostring(root, encoding="utf-8"))
        if res is not None:
            id_node = res.find(".//id")
            if id_node is not None:
                logger.info("Cupón '{code}' creado con ID {id}.", code=data.get("code"), id=id_node.text)
                return id_node.text
        logger.error("No se pudo crear el cupón '{code}'.", code=data.get("code"))
        return None

    def delete_cart_rule(self, rule_id: str) -> bool:
        """Elimina un cupón de descuento por su ID."""
        res = self._request("DELETE", f"cart_rules/{rule_id}")
        success = res is not None
        if success:
            logger.info("Cupón ID {id} eliminado.", id=rule_id)
        return success

    def get_specific_prices(self, product_id: str) -> list:
        """Devuelve las reglas de precio específico (descuentos por cantidad, grupo, etc.) de un producto."""
        res = self._request(
            "GET",
            f"specific_prices?filter[id_product]={product_id}&display=full"
        )
        if res is None:
            return []
        precios = []
        for p in res.findall(".//specific_price"):
            precios.append({
                "id":           p.findtext("id"),
                "reduccion":    p.findtext("reduction"),
                "tipo":         p.findtext("reduction_type"),
                "desde_cant":   p.findtext("from_quantity"),
                "id_grupo":     p.findtext("id_group"),
                "desde":        p.findtext("from"),
                "hasta":        p.findtext("to"),
            })
        return precios

    # ------------------------------------------------------------------
    # Transportistas
    # ------------------------------------------------------------------

    def get_carriers(self) -> list:
        """Devuelve todos los transportistas configurados en la tienda."""
        res = self._request("GET", "carriers?display=full")
        if res is None:
            return []
        transportistas = []
        for c in res.findall(".//carrier"):
            transportistas.append({
                "id":          c.findtext("id"),
                "nombre":      c.findtext("name"),
                "delay":       c.findtext("delay"),
                "activo":      c.findtext("active"),
                "gratis":      c.findtext("is_free"),
                "precio_base": c.findtext("shipping_handling"),
            })
        return transportistas

    def get_carrier(self, carrier_id: str) -> Optional[dict]:
        """Devuelve el detalle completo de un transportista por su ID."""
        res = self._request("GET", f"carriers/{carrier_id}?display=full")
        if res is None:
            return None
        c = res.find(".//carrier")
        if c is None:
            return None
        return {
            "id":            c.findtext("id"),
            "nombre":        c.findtext("name"),
            "delay":         c.findtext("delay"),
            "activo":        c.findtext("active"),
            "gratis":        c.findtext("is_free"),
            "precio_base":   c.findtext("shipping_handling"),
            "max_peso":      c.findtext("max_weight"),
            "max_ancho":     c.findtext("width"),
            "max_alto":      c.findtext("height"),
            "max_profundo":  c.findtext("depth"),
            "url_tracking":  c.findtext("url"),
            "id_tax":        c.findtext("id_tax_rules_group"),
            "id_referencia": c.findtext("id_reference"),
        }

    def create_carrier(self, data: dict) -> Optional[str]:
        """
        Crea un nuevo transportista en PrestaShop.
        Campos requeridos: name, delay.
        Campos opcionales: active, is_free, url (tracking), max_weight.
        """
        root = ET.Element("prestashop")
        c    = ET.SubElement(root, "carrier")

        campos = {
            "name":            data.get("name", "Nuevo transportista"),
            "active":          str(int(data.get("active", True))),
            "is_free":         str(int(data.get("is_free", False))),
            "shipping_method": str(data.get("shipping_method", "0")),
            "need_range":      "0",
        }
        for k, v in campos.items():
            ET.SubElement(c, k).text = v

        # Delay (multilingual)
        delay_el = ET.SubElement(c, "delay")
        ET.SubElement(delay_el, "language", {"id": "1"}).text = data.get("delay", "3-5 días hábiles")

        if data.get("url"):
            ET.SubElement(c, "url").text = data["url"]
        if data.get("max_weight") is not None:
            ET.SubElement(c, "max_weight").text = str(data["max_weight"])

        res = self._request("POST", "carriers", data=ET.tostring(root, encoding="utf-8"))
        if res is None:
            logger.error("No se pudo crear el transportista '{n}'.", n=data.get("name"))
            return None
        id_node = res.find(".//id")
        if id_node is not None:
            logger.info("Transportista '{n}' creado con ID {id}.", n=data.get("name"), id=id_node.text)
            return id_node.text
        return None

    def delete_carrier(self, carrier_id: str) -> bool:
        """Elimina un transportista de PrestaShop."""
        res = self._request("DELETE", f"carriers/{carrier_id}")
        success = res is not None
        if success:
            logger.info("Transportista ID {id} eliminado.", id=carrier_id)
        else:
            logger.error("No se pudo eliminar el transportista ID {id}.", id=carrier_id)
        return success

    def update_carrier(self, carrier_id: str, data: dict) -> bool:
        """
        Actualiza datos de un transportista.
        Estrategia: GET completo → modificar campos → PUT con XML completo.
        """
        res_get = self._request("GET", f"carriers/{carrier_id}")
        if res_get is None:
            logger.error("No se pudo obtener carrier ID {id}.", id=carrier_id)
            return False

        carrier_el = res_get.find(".//carrier")
        if carrier_el is None:
            logger.error("Carrier ID {id} no encontrado en respuesta PS.", id=carrier_id)
            return False

        # Aplicar cambios sobre el XML original
        def _set(tag: str, val: str):
            el = carrier_el.find(tag)
            if el is not None:
                el.text = val
            else:
                ET.SubElement(carrier_el, tag).text = val

        def _bool(v) -> str:
            return "1" if v else "0"

        if "name" in data and data["name"]:
            nl = carrier_el.find(".//name/language")
            if nl is not None:
                nl.text = data["name"]
            else:
                name_el = carrier_el.find("name")
                if name_el is None:
                    name_el = ET.SubElement(carrier_el, "name")
                ET.SubElement(name_el, "language", {"id": "1"}).text = data["name"]

        if "delay" in data and data["delay"]:
            dl = carrier_el.find(".//delay/language")
            if dl is not None:
                dl.text = data["delay"]
            else:
                delay_el = carrier_el.find("delay")
                if delay_el is None:
                    delay_el = ET.SubElement(carrier_el, "delay")
                ET.SubElement(delay_el, "language", {"id": "1"}).text = data["delay"]

        if "active"      in data and data["active"]      is not None: _set("active",          _bool(data["active"]))
        if "is_free"     in data and data["is_free"]     is not None: _set("is_free",         _bool(data["is_free"]))
        if "url"         in data and data["url"]         is not None: _set("url",             data["url"])
        if "max_weight"  in data and data["max_weight"]  is not None: _set("max_weight",      str(data["max_weight"]))

        root = ET.Element("prestashop")
        root.append(carrier_el)
        res = self._request("PUT", f"carriers/{carrier_id}", data=ET.tostring(root, encoding="utf-8"))
        success = res is not None
        if success:
            logger.info("Transportista ID {id} actualizado.", id=carrier_id)
        else:
            logger.error("No se pudo actualizar el transportista ID {id}.", id=carrier_id)
        return success


    # ------------------------------------------------------------------
    # Facturas, albaranes e historial de pedido
    # ------------------------------------------------------------------

    def get_order_invoices(self, order_id: str) -> list:
        """
        Devuelve las facturas asociadas a un pedido.
        Trae todas y filtra localmente por id_order para compatibilidad con PS9.
        """
        # PS9 a veces ignora filter[id_order] — traemos todas y filtramos en Python
        res = self._request("GET", "order_invoices?display=full&limit=200&sort=[id_DESC]")
        if res is None:
            return []
        facturas = []
        for inv in res.findall(".//order_invoice"):
            if inv.findtext("id_order") != str(order_id):
                continue
            fecha = inv.findtext("date_add") or inv.findtext("date_invoice") or ""
            facturas.append({
                "id":               inv.findtext("id"),
                "numero":           inv.findtext("number"),
                "fecha":            fecha,
                "total_sin_iva":    inv.findtext("total_products"),
                "total_envio":      inv.findtext("total_shipping_tax_incl"),
                "total_descuentos": inv.findtext("total_discount_tax_incl"),
                "total_con_iva":    inv.findtext("total_paid_tax_incl"),
                "url_pdf":          f"{self.base_url}/order_invoices/{inv.findtext('id')}",
            })
        return facturas

    def get_all_invoices(
        self,
        date_from: Optional[str] = None,
        date_to:   Optional[str] = None,
        limit:     int = 50,
    ) -> list:
        """
        Devuelve todas las facturas de la tienda, con filtro de fecha opcional.
        PS9 usa el campo date_invoice para filtrar facturas.
        Sin filtro devuelve las últimas `limit` facturas.
        """
        filtro = f"order_invoices?display=full&limit={limit}&sort=[id_DESC]"
        res = self._request("GET", filtro)
        if res is None:
            return []

        facturas = []
        for inv in res.findall(".//order_invoice"):
            fecha_inv = inv.findtext("date_add") or inv.findtext("date_invoice") or ""
            # Filtrar por fecha en Python si se proporcionó rango
            if date_from and fecha_inv:
                if fecha_inv < date_from or (date_to and fecha_inv > date_to):
                    continue
            facturas.append({
                "id":            inv.findtext("id"),
                "id_pedido":     inv.findtext("id_order"),
                "numero":        inv.findtext("number"),
                "fecha":         fecha_inv,
                "total_con_iva": inv.findtext("total_paid_tax_incl"),
                "total_sin_iva": inv.findtext("total_products"),
                "total_envio":   inv.findtext("total_shipping_tax_incl"),
                "url_pdf":       f"{self.base_url}/order_invoices/{inv.findtext('id')}",
            })
        return facturas

    def get_order_slips(self, order_id: str) -> list:
        """
        Devuelve los albaranes / notas de crédito asociados a un pedido.
        PS genera albaranes al procesar devoluciones parciales o totales.
        """
        res = self._request(
            "GET",
            f"order_slip?filter[id_order]={order_id}&display=full"
        )
        if res is None:
            return []
        albaranes = []
        for slip in res.findall(".//order_slip"):
            albaranes.append({
                "id":              slip.findtext("id"),
                "fecha":           slip.findtext("date_add"),
                "total_productos": slip.findtext("total_products_tax_incl"),
                "total_envio":     slip.findtext("total_shipping_tax_incl"),
                "importe_total":   slip.findtext("amount"),
            })
        return albaranes

    def get_order_history(self, order_id: str) -> list:
        """
        Devuelve el historial completo de cambios de estado de un pedido.
        PS9 puede usar order_history (singular) en lugar de order_histories.
        Filtra localmente por id_order para máxima compatibilidad.
        """
        # Intentar primero con el recurso correcto de PS9
        res = self._request(
            "GET",
            f"order_histories?display=full&filter[id_order]={order_id}&sort=[id_DESC]"
        )
        # Fallback: traer todos y filtrar en Python
        if res is None or not res.findall(".//order_history"):
            res = self._request("GET", "order_histories?display=full&sort=[id_DESC]&limit=200")

        if res is None:
            return []

        estados = self.get_order_states()
        mapa_estados = {e["id"]: e["nombre"] for e in estados}

        historial = []
        for h in res.findall(".//order_history"):
            if h.findtext("id_order") != str(order_id):
                continue
            id_estado = h.findtext("id_order_state") or ""
            historial.append({
                "id":        h.findtext("id"),
                "id_estado": id_estado,
                "estado":    mapa_estados.get(id_estado, f"Estado {id_estado}"),
                "fecha":     h.findtext("date_add"),
            })
        return historial

    def get_financial_summary(
        self,
        date_from: Optional[str] = None,
        date_to:   Optional[str] = None,
    ) -> dict:
        """
        Genera un resumen financiero a partir de las facturas del periodo indicado.
        Si no se especifica fecha, usa el mes en curso.
        """
        from datetime import datetime, date
        if not date_from:
            hoy = date.today()
            date_from = f"{hoy.year}-{hoy.month:02d}-01 00:00:00"
            date_to   = f"{hoy.year}-{hoy.month:02d}-{hoy.day:02d} 23:59:59"

        facturas = self.get_all_invoices(date_from=date_from, date_to=date_to, limit=500)

        total_facturado   = 0.0
        total_sin_iva     = 0.0
        num_facturas      = len(facturas)

        for f in facturas:
            try:
                total_facturado += float(f.get("total_con_iva") or 0)
                total_sin_iva   += float(f.get("total_sin_iva") or 0)
            except (ValueError, TypeError):
                pass

        iva_total = round(total_facturado - total_sin_iva, 2)

        return {
            "periodo":          {"desde": date_from, "hasta": date_to},
            "num_facturas":     num_facturas,
            "total_sin_iva":    round(total_sin_iva, 2),
            "total_iva":        iva_total,
            "total_facturado":  round(total_facturado, 2),
            "facturas":         facturas,
        }

    # ------------------------------------------------------------------
    # Métodos de pago (a través de order_payments)
    # ------------------------------------------------------------------

    def get_order_payments(self, order_reference: str) -> list:
        """Devuelve los pagos registrados para un pedido concreto por su referencia."""
        res = self._request(
            "GET",
            f"order_payments?filter[order_reference]={order_reference}&display=full"
        )
        if res is None:
            return []
        pagos = []
        for p in res.findall(".//order_payment"):
            pagos.append({
                "id":              p.findtext("id"),
                "referencia":      p.findtext("order_reference"),
                "metodo":          p.findtext("payment_method"),
                "importe":         p.findtext("amount"),
                "divisa":          p.findtext("id_currency"),
                "fecha":           p.findtext("date_add"),
                "transaccion_id":  p.findtext("transaction_id"),
            })
        return pagos

    # ------------------------------------------------------------------
    # Divisas e idiomas
    # ------------------------------------------------------------------

    def get_currencies(self) -> list:
        """Devuelve todas las divisas configuradas en la tienda."""
        res = self._request("GET", "currencies?display=full")
        if res is None:
            return []
        divisas = []
        for c in res.findall(".//currency"):
            divisas.append({
                "id":           c.findtext("id"),
                "nombre":       c.findtext("name"),
                "iso_code":     c.findtext("iso_code"),
                "simbolo":      c.findtext("symbol"),
                "tasa_cambio":  c.findtext("conversion_rate"),
                "activa":       c.findtext("active"),
                "por_defecto":  c.findtext("is_default"),
            })
        return divisas

    def get_languages(self) -> list:
        """Devuelve todos los idiomas instalados en la tienda."""
        res = self._request("GET", "languages?display=full")
        if res is None:
            return []
        idiomas = []
        for l in res.findall(".//language"):
            idiomas.append({
                "id":          l.findtext("id"),
                "nombre":      l.findtext("name"),
                "iso_code":    l.findtext("iso_code"),
                "locale":      l.findtext("locale"),
                "activo":      l.findtext("active"),
                "por_defecto": l.findtext("is_default"),
            })
        return idiomas

    def update_currency_rate(self, currency_id: str, rate: float) -> bool:
        """Actualiza la tasa de cambio de una divisa."""
        root = ET.Element("prestashop")
        c = ET.SubElement(root, "currency")
        ET.SubElement(c, "id").text = str(currency_id)
        ET.SubElement(c, "conversion_rate").text = str(rate)
        res = self._request("PUT", f"currencies/{currency_id}", data=ET.tostring(root, encoding="utf-8"))
        success = res is not None
        if success:
            logger.info("Tasa de cambio de divisa ID {id} actualizada a {rate}.", id=currency_id, rate=rate)
        else:
            logger.error("No se pudo actualizar la tasa de cambio de divisa ID {id}.", id=currency_id)
        return success

    # ------------------------------------------------------------------
    # Direcciones
    # ------------------------------------------------------------------

    def get_customer_addresses(self, customer_id: str) -> list:
        """Devuelve todas las direcciones de un cliente."""
        res = self._request(
            "GET",
            f"addresses?filter[id_customer]={customer_id}&display=full"
        )
        if res is None:
            return []
        direcciones = []
        for a in res.findall(".//address"):
            direcciones.append({
                "id":          a.findtext("id"),
                "alias":       a.findtext("alias"),
                "nombre":      f"{a.findtext('firstname','')} {a.findtext('lastname','')}".strip(),
                "empresa":     a.findtext("company"),
                "direccion":   a.findtext("address1"),
                "direccion2":  a.findtext("address2"),
                "ciudad":      a.findtext("city"),
                "codigo_postal": a.findtext("postcode"),
                "id_pais":     a.findtext("id_country"),
                "telefono":    a.findtext("phone"),
                "eliminada":   a.findtext("deleted"),
            })
        return direcciones

    def create_address(self, data: dict) -> Optional[str]:
        """
        Crea una dirección nueva para un cliente.
        data: id_customer, alias, firstname, lastname, address1, city, postcode, id_country.
        """
        root = ET.Element("prestashop")
        a = ET.SubElement(root, "address")
        campos = {
            "id_customer": data.get("id_customer", ""),
            "alias":       data.get("alias", "Casa"),
            "firstname":   data.get("firstname", ""),
            "lastname":    data.get("lastname", ""),
            "address1":    data.get("address1", ""),
            "address2":    data.get("address2", ""),
            "city":        data.get("city", ""),
            "postcode":    data.get("postcode", ""),
            "id_country":  data.get("id_country", "6"),
            "phone":       data.get("phone", ""),
            "company":     data.get("company", ""),
        }
        for key, val in campos.items():
            ET.SubElement(a, key).text = str(val)
        res = self._request("POST", "addresses", data=ET.tostring(root, encoding="utf-8"))
        if res is not None:
            id_node = res.find(".//id")
            if id_node is not None:
                logger.info("Dirección creada para cliente ID {id}.", id=data.get("id_customer"))
                return id_node.text
        logger.error("No se pudo crear la dirección para cliente ID {id}.", id=data.get("id_customer"))
        return None

    def delete_address(self, address_id: str) -> bool:
        """Elimina una dirección por su ID."""
        res = self._request("DELETE", f"addresses/{address_id}")
        success = res is not None
        if success:
            logger.info("Dirección ID {id} eliminada.", id=address_id)
        else:
            logger.error("No se pudo eliminar la dirección ID {id}.", id=address_id)
        return success

    # ------------------------------------------------------------------
    # Grupos de clientes
    # ------------------------------------------------------------------

    def get_customer_groups(self) -> list:
        """Devuelve todos los grupos de clientes (Cliente, Invitado, Mayorista, VIP...)."""
        res = self._request("GET", "groups?display=full")
        if res is None:
            return []
        grupos = []
        for g in res.findall(".//group"):
            name_node = g.find(".//language")
            grupos.append({
                "id":              g.findtext("id"),
                "nombre":          name_node.text if name_node is not None else "",
                "descuento":       g.findtext("reduction"),
                "precio_muestra":  g.findtext("show_prices"),
            })
        return grupos

    def assign_customer_group(self, customer_id: str, group_id: str) -> bool:
        """Asigna un cliente a un grupo concreto."""
        root = ET.Element("prestashop")
        c = ET.SubElement(root, "customer")
        ET.SubElement(c, "id").text = str(customer_id)
        ET.SubElement(c, "id_default_group").text = str(group_id)
        assoc = ET.SubElement(c, "associations")
        groups_el = ET.SubElement(assoc, "groups")
        g_el = ET.SubElement(groups_el, "group")
        ET.SubElement(g_el, "id").text = str(group_id)
        res = self._request("PUT", f"customers/{customer_id}", data=ET.tostring(root, encoding="utf-8"))
        success = res is not None
        if success:
            logger.info("Cliente ID {cid} asignado al grupo ID {gid}.", cid=customer_id, gid=group_id)
        else:
            logger.error("No se pudo asignar el grupo al cliente ID {id}.", id=customer_id)
        return success

    # ------------------------------------------------------------------
    # Páginas CMS
    # ------------------------------------------------------------------

    def get_cms_pages(self) -> list:
        """Devuelve todas las páginas CMS de la tienda (Aviso legal, FAQ, etc.)."""
        res = self._request("GET", "cms?display=full")
        if res is None:
            return []
        paginas = []
        for p in res.findall(".//cms"):
            name_node    = p.find(".//meta_title/language")
            content_node = p.find(".//content/language")
            paginas.append({
                "id":       p.findtext("id"),
                "titulo":   name_node.text if name_node is not None else "",
                "activa":   p.findtext("active"),
                "preview":  (content_node.text or "")[:120].strip() + "..." if content_node is not None and content_node.text else "",
            })
        return paginas

    def get_cms_page(self, page_id: str) -> Optional[dict]:
        """Devuelve el contenido completo de una página CMS."""
        res = self._request("GET", f"cms/{page_id}")
        if res is None:
            return None
        p = res.find(".//cms")
        if p is None:
            return None
        name_node    = p.find(".//meta_title/language")
        content_node = p.find(".//content/language")
        return {
            "id":      p.findtext("id"),
            "titulo":  name_node.text if name_node is not None else "",
            "activa":  p.findtext("active"),
            "contenido": content_node.text if content_node is not None else "",
        }

    def update_cms_page(self, page_id: str, data: dict) -> bool:
        """Actualiza el contenido o estado de una página CMS."""
        root = ET.Element("prestashop")
        p = ET.SubElement(root, "cms")
        ET.SubElement(p, "id").text = str(page_id)
        if "active" in data:
            ET.SubElement(p, "active").text = str(data["active"])
        if "content" in data:
            content_el = ET.SubElement(p, "content")
            ET.SubElement(content_el, "language", {"id": "1"}).text = data["content"]
        if "title" in data:
            title_el = ET.SubElement(p, "meta_title")
            ET.SubElement(title_el, "language", {"id": "1"}).text = data["title"]
        res = self._request("PUT", f"cms/{page_id}", data=ET.tostring(root, encoding="utf-8"))
        success = res is not None
        if success:
            logger.info("Página CMS ID {id} actualizada.", id=page_id)
        else:
            logger.error("No se pudo actualizar la página CMS ID {id}.", id=page_id)
        return success

    # ------------------------------------------------------------------
    # Etiquetas / Tags
    # ------------------------------------------------------------------

    def get_tags(self, limit: int = 100) -> list:
        """Devuelve todas las etiquetas de búsqueda creadas en la tienda."""
        res = self._request("GET", f"tags?display=full&limit={limit}")
        if res is None:
            return []
        etiquetas = []
        for t in res.findall(".//tag"):
            etiquetas.append({
                "id":       t.findtext("id"),
                "nombre":   t.findtext("name"),
                "id_lang":  t.findtext("id_lang"),
            })
        return etiquetas

    def get_product_tags(self, product_id: str) -> list:
        """Devuelve las etiquetas asignadas a un producto concreto."""
        res = self._request(
            "GET",
            f"products/{product_id}?display=[id,tags]"
        )
        if res is None:
            return []
        tags = []
        for t in res.findall(".//tag"):
            tags.append({"id": t.findtext("id")})
        return tags

    def create_tag(self, name: str, id_lang: str = "1") -> Optional[str]:
        """Crea una etiqueta nueva. Devuelve su ID o None si falla."""
        root = ET.Element("prestashop")
        t = ET.SubElement(root, "tag")
        ET.SubElement(t, "name").text    = name
        ET.SubElement(t, "id_lang").text = id_lang
        res = self._request("POST", "tags", data=ET.tostring(root, encoding="utf-8"))
        if res is not None:
            id_node = res.find(".//id")
            if id_node is not None:
                logger.info("Etiqueta '{name}' creada con ID {id}.", name=name, id=id_node.text)
                return id_node.text
        logger.error("No se pudo crear la etiqueta '{name}'.", name=name)
        return None

    # ------------------------------------------------------------------
    # Países y zonas
    # ------------------------------------------------------------------

    def get_countries(self, active_only: bool = True) -> list:
        """Devuelve todos los países, opcionalmente solo los activos."""
        filtro = "filter[active]=1&" if active_only else ""
        res = self._request("GET", f"countries?{filtro}display=full")
        if res is None:
            return []
        paises = []
        for c in res.findall(".//country"):
            name_node = c.find(".//language")
            paises.append({
                "id":        c.findtext("id"),
                "nombre":    name_node.text if name_node is not None else "",
                "iso_code":  c.findtext("iso_code"),
                "id_zona":   c.findtext("id_zone"),
                "activo":    c.findtext("active"),
                "necesita_codigo_postal": c.findtext("need_zip_code"),
            })
        return paises

    def get_zones(self) -> list:
        """Devuelve todas las zonas geográficas configuradas para envíos."""
        res = self._request("GET", "zones?display=full")
        if res is None:
            return []
        zonas = []
        for z in res.findall(".//zone"):
            zonas.append({
                "id":     z.findtext("id"),
                "nombre": z.findtext("name"),
                "activa": z.findtext("enabled"),
            })
        return zonas

    # ------------------------------------------------------------------
    # Reseñas (requiere módulo Product Comments instalado en PS)
    # ------------------------------------------------------------------

    def get_product_reviews(self, product_id: str) -> list:
        """
        Devuelve las reseñas de un producto.
        Requiere el módulo 'productcomments' instalado y activado en PrestaShop.
        """
        res = self._request(
            "GET",
            f"product_comments?filter[id_product]={product_id}&display=full"
        )
        if res is None:
            return []
        resenas = []
        for r in res.findall(".//product_comment"):
            resenas.append({
                "id":         r.findtext("id"),
                "titulo":     r.findtext("title"),
                "contenido":  r.findtext("content"),
                "nota":       r.findtext("grade"),
                "id_cliente": r.findtext("id_customer"),
                "validada":   r.findtext("validate"),
                "fecha":      r.findtext("date_add"),
            })
        return resenas

    # ------------------------------------------------------------------
    # Configuración general de la tienda
    # ------------------------------------------------------------------

    def get_configuration(self, key: str) -> Optional[str]:
        """
        Lee un valor de configuración de la tienda por su clave.
        Ejemplos de claves: PS_SHOP_NAME, PS_SHOP_EMAIL, PS_CURRENCY_DEFAULT.
        """
        res = self._request("GET", f"configurations?filter[name]={key}&display=full")
        if res is None:
            return None
        config = res.find(".//configuration")
        if config is None:
            return None
        return config.findtext("value")

    def get_configurations(self, keys: list) -> dict:
        """Lee varios valores de configuración a la vez. Devuelve dict {clave: valor}."""
        resultado = {}
        for key in keys:
            valor = self.get_configuration(key)
            resultado[key] = valor
        return resultado

    def update_configuration(self, key: str, value: str) -> bool:
        """
        Actualiza un valor de configuración de la tienda.
        ⚠️ Modifica ajustes globales de PrestaShop — usar con precaución.
        """
        res = self._request("GET", f"configurations?filter[name]={key}&display=[id]")
        if res is None:
            logger.error("No se encontró la clave de configuración '{key}'.", key=key)
            return False
        config = res.find(".//configuration")
        if config is None:
            logger.error("Clave de configuración '{key}' no existe.", key=key)
            return False
        config_id = config.findtext("id")

        root = ET.Element("prestashop")
        c = ET.SubElement(root, "configuration")
        ET.SubElement(c, "id").text    = config_id
        ET.SubElement(c, "name").text  = key
        ET.SubElement(c, "value").text = value
        result = self._request("PUT", f"configurations/{config_id}", data=ET.tostring(root, encoding="utf-8"))
        success = result is not None
        if success:
            logger.info("Configuración '{key}' actualizada a '{value}'.", key=key, value=value)
        else:
            logger.error("No se pudo actualizar la configuración '{key}'.", key=key)
        return success

