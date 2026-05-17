# ============================================================
#  core/image_processor.py — Procesado de imágenes
#
#  Prepara las imágenes antes de subirlas a PrestaShop:
#    - Convierte cualquier formato a JPEG
#    - Redimensiona respetando el aspect ratio
#    - Optimiza el peso sin pérdida visible de calidad
#    - Convierte RGBA/P a RGB (PS no acepta transparencias)
# ============================================================

import io
import os
from typing import Optional

from loguru import logger
from PIL import Image, ImageOps

DEFAULT_MAX_WIDTH  = 1200
DEFAULT_MAX_HEIGHT = 1200
DEFAULT_QUALITY    = 85
MAX_FILE_SIZE_MB   = 8


def process_image(
    source,
    max_width:  int = DEFAULT_MAX_WIDTH,
    max_height: int = DEFAULT_MAX_HEIGHT,
    quality:    int = DEFAULT_QUALITY,
    output_path: Optional[str] = None,
) -> Optional[bytes]:
    """
    Procesa una imagen para subirla a PrestaShop:
    1. Abre el archivo (bytes o ruta en disco)
    2. Convierte a RGB si es necesario (elimina transparencias)
    3. Aplica auto-orientación EXIF (fotos de móvil giradas)
    4. Redimensiona si supera max_width x max_height (respeta aspect ratio)
    5. Guarda como JPEG con la calidad indicada
    Devuelve los bytes JPEG procesados, o None si la imagen no es válida.
    """
    try:
        if isinstance(source, bytes):
            img = Image.open(io.BytesIO(source))
        else:
            img = Image.open(source)

        img = ImageOps.exif_transpose(img)

        if img.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            background.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")

        original_size = img.size
        if img.width > max_width or img.height > max_height:
            img.thumbnail((max_width, max_height), Image.LANCZOS)
            logger.debug("Imagen redimensionada: {orig} -> {new}", orig=original_size, new=img.size)

        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=quality, optimize=True)
        result_bytes = buffer.getvalue()

        if output_path:
            os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
            with open(output_path, "wb") as f:
                f.write(result_bytes)

        logger.debug("Imagen procesada: {w}x{h}px, {kb:.1f} KB", w=img.width, h=img.height, kb=len(result_bytes)/1024)
        return result_bytes

    except Exception as exc:
        logger.error("Error procesando imagen: {e}", e=exc)
        return None


def validate_image(data: bytes, max_mb: float = MAX_FILE_SIZE_MB):
    """
    Valida que los bytes son una imagen válida y no superan el tamaño máximo.
    Devuelve (True, "") o (False, "mensaje de error").
    """
    size_mb = len(data) / (1024 * 1024)
    if size_mb > max_mb:
        return False, f"La imagen supera el tamaño máximo ({size_mb:.1f} MB > {max_mb} MB)."
    try:
        img = Image.open(io.BytesIO(data))
        img.verify()
        return True, ""
    except Exception as exc:
        return False, f"El archivo no es una imagen válida: {exc}"

# Alias para compatibilidad — process_bytes es lo mismo que process_image
process_bytes = process_image
