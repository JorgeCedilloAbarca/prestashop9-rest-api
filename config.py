# ============================================================
#  config.py — Configuración centralizada sin pydantic-settings
#  Compatible con Python 3.14. Lee credenciales desde .env
# ============================================================

import os
from dotenv import load_dotenv

# Carga el archivo .env si existe (en producción se usan variables del sistema)
load_dotenv()


def _require(key: str) -> str:
    """Lee una variable de entorno obligatoria. Lanza error si no existe."""
    value = os.getenv(key)
    if not value:
        raise EnvironmentError(
            f"Variable de entorno obligatoria no encontrada: '{key}'\n"
            f"Asegúrate de que existe el archivo .env con esa clave."
        )
    return value


class Settings:
    """
    Configuración de la aplicación cargada desde variables de entorno.
    El orden de prioridad es: variables del sistema > archivo .env
    """

    # --- PrestaShop Webservice ---
    ps_api_key:  str = _require("PS_API_KEY")
    ps_base_url: str = _require("PS_BASE_URL")

    # --- Base de datos MySQL ---
    db_host:     str = _require("DB_HOST")
    db_port:     int = int(os.getenv("DB_PORT", "3306"))
    db_user:     str = _require("DB_USER")
    db_password: str = _require("DB_PASSWORD")
    db_name:     str = _require("DB_NAME")

    # --- Rutas locales ---
    img_path: str = os.getenv("IMG_PATH", "./static/images/")

    # --- Servidor ---
    app_host:   str  = os.getenv("APP_HOST", "0.0.0.0")
    app_port:   int  = int(os.getenv("APP_PORT", "8000"))
    app_reload: bool = os.getenv("APP_RELOAD", "false").lower() == "true"

    # --- JWT ---
    jwt_secret_key: str = os.getenv("JWT_SECRET_KEY", "")
    jwt_algorithm:  str = os.getenv("JWT_ALGORITHM", "HS256")
    jwt_access_token_expire_minutes:  int = int(os.getenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
    jwt_refresh_token_expire_days:    int = int(os.getenv("JWT_REFRESH_TOKEN_EXPIRE_DAYS", "7"))

    @property
    def db_config(self) -> dict:
        """Devuelve la configuración de MySQL como dict para mysql-connector."""
        return {
            "host":     self.db_host,
            "port":     self.db_port,
            "user":     self.db_user,
            "password": self.db_password,
            "database": self.db_name,
        }


# Instancia única reutilizable en toda la aplicación
settings = Settings()
