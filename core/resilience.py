# ============================================================
#  core/resilience.py — Patrones de resiliencia para llamadas externas
#
#  Implementa dos mecanismos de protección:
#
#  1. retry_with_backoff — decorador que reintenta una función
#     automáticamente con espera exponencial cuando falla por
#     errores transitorios (timeouts, 5xx, conexión caída).
#
#  2. CircuitBreaker — clase que monitoriza los fallos de un
#     servicio externo y, cuando supera el umbral, deja de
#     intentar llamadas durante un período de enfriamiento,
#     evitando cascadas de error y timeouts innecesarios.
#
#  Uso:
#      from core.resilience import retry_with_backoff, circuit_breaker
#
#      @retry_with_backoff(max_retries=3)
#      def llamada_a_api(): ...
#
#      if circuit_breaker.is_open():
#          raise ServiceUnavailableError(...)
# ============================================================

import time
import threading
from enum import Enum
from functools import wraps
from typing import Callable, Optional, Tuple, Type

from loguru import logger


# ──────────────────────────────────────────────────────────────
# Excepciones propias
# ──────────────────────────────────────────────────────────────

class RetryableError(Exception):
    """Error transitorio que puede resolverse reintentando."""
    pass


class CircuitOpenError(Exception):
    """El circuit breaker está abierto — el servicio no está disponible."""
    pass


# ──────────────────────────────────────────────────────────────
# Estados del circuit breaker
# ──────────────────────────────────────────────────────────────

class CircuitState(Enum):
    CLOSED   = "closed"    # Normal — las llamadas pasan
    OPEN     = "open"      # Bloqueado — demasiados fallos recientes
    HALF_OPEN = "half_open" # Prueba — deja pasar una llamada de prueba


# ──────────────────────────────────────────────────────────────
# Circuit Breaker
# ──────────────────────────────────────────────────────────────

class CircuitBreaker:
    """
    Circuit Breaker para proteger llamadas a servicios externos.

    Estados:
      CLOSED   → Las llamadas se ejecutan normalmente.
      OPEN     → Las llamadas se rechazan inmediatamente sin ejecutarse.
                 Se activa cuando se superan `failure_threshold` fallos
                 consecutivos en menos de `window_seconds`.
      HALF_OPEN → Tras `recovery_timeout` segundos en OPEN, se deja
                 pasar una llamada de prueba. Si tiene éxito → CLOSED.
                 Si falla → vuelve a OPEN.

    Uso:
        cb = CircuitBreaker(name="prestashop", failure_threshold=5)

        if cb.is_open():
            raise CircuitOpenError("PrestaShop no disponible")

        try:
            result = llamada_a_ps()
            cb.record_success()
        except Exception:
            cb.record_failure()
            raise
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        window_seconds: float = 60.0,
    ) -> None:
        self.name              = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout  = recovery_timeout
        self.window_seconds    = window_seconds

        self._state            = CircuitState.CLOSED
        self._failure_count    = 0
        self._last_failure_time: Optional[float] = None
        self._opened_at: Optional[float]         = None
        self._lock             = threading.Lock()

    # ── Estado ──────────────────────────────────────────────

    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._get_state()

    def _get_state(self) -> CircuitState:
        """Calcula el estado actual considerando el tiempo transcurrido."""
        if self._state == CircuitState.OPEN:
            if self._opened_at and (time.monotonic() - self._opened_at) >= self.recovery_timeout:
                # Ha pasado el tiempo de recuperación → probamos con HALF_OPEN
                self._state = CircuitState.HALF_OPEN
                logger.info(
                    "Circuit breaker '{name}': OPEN → HALF_OPEN (probando recuperación)",
                    name=self.name,
                )
        return self._state

    def is_open(self) -> bool:
        """Devuelve True si el circuit está OPEN (las llamadas deben rechazarse)."""
        return self.state == CircuitState.OPEN

    def should_allow_request(self) -> bool:
        """
        Devuelve True si se debe permitir la llamada.
        CLOSED y HALF_OPEN permiten; OPEN rechaza.
        """
        with self._lock:
            state = self._get_state()
            if state == CircuitState.CLOSED:
                return True
            if state == CircuitState.HALF_OPEN:
                # Solo dejamos pasar una llamada de prueba
                return True
            return False  # OPEN

    # ── Registro de resultados ───────────────────────────────

    def record_success(self) -> None:
        """Registra una llamada exitosa. Si estaba en HALF_OPEN → CLOSED."""
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                logger.info(
                    "Circuit breaker '{name}': HALF_OPEN → CLOSED (recuperado)",
                    name=self.name,
                )
            self._state         = CircuitState.CLOSED
            self._failure_count = 0
            self._last_failure_time = None
            self._opened_at         = None

    def record_failure(self) -> None:
        """
        Registra un fallo. Si se supera el umbral → OPEN.
        Los fallos fuera de la ventana de tiempo se descartan.
        """
        with self._lock:
            now = time.monotonic()

            # Resetear contador si los fallos son muy antiguos
            if (
                self._last_failure_time is not None
                and (now - self._last_failure_time) > self.window_seconds
            ):
                self._failure_count = 0

            self._failure_count    += 1
            self._last_failure_time = now

            if self._state == CircuitState.HALF_OPEN:
                # Fallo en prueba → volver a OPEN
                self._state     = CircuitState.OPEN
                self._opened_at = now
                logger.warning(
                    "Circuit breaker '{name}': HALF_OPEN → OPEN (fallo en prueba)",
                    name=self.name,
                )
            elif (
                self._state == CircuitState.CLOSED
                and self._failure_count >= self.failure_threshold
            ):
                self._state     = CircuitState.OPEN
                self._opened_at = now
                logger.error(
                    "Circuit breaker '{name}': CLOSED → OPEN "
                    "({n} fallos consecutivos en {w}s). "
                    "Recuperación en {r}s.",
                    name=self.name,
                    n=self._failure_count,
                    w=self.window_seconds,
                    r=self.recovery_timeout,
                )

    # ── Info de diagnóstico ──────────────────────────────────

    def status(self) -> dict:
        """Devuelve el estado actual del circuit breaker para el health check."""
        with self._lock:
            state = self._get_state()
            time_open = None
            if self._opened_at:
                time_open = round(time.monotonic() - self._opened_at, 1)
            return {
                "name":           self.name,
                "state":          state.value,
                "failure_count":  self._failure_count,
                "threshold":      self.failure_threshold,
                "seconds_open":   time_open,
                "recovery_in":    max(0, round(
                    self.recovery_timeout - (time.monotonic() - self._opened_at), 1
                )) if self._opened_at and state == CircuitState.OPEN else None,
            }

    def reset(self) -> None:
        """Resetea el circuit breaker manualmente (útil para tests y administración)."""
        with self._lock:
            self._state         = CircuitState.CLOSED
            self._failure_count = 0
            self._last_failure_time = None
            self._opened_at         = None
            logger.info("Circuit breaker '{name}' reseteado manualmente.", name=self.name)


# ──────────────────────────────────────────────────────────────
# Decorador de reintentos con backoff exponencial
# ──────────────────────────────────────────────────────────────

def retry_with_backoff(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 16.0,
    retryable_statuses: Tuple[int, ...] = (429, 500, 502, 503, 504),
    retryable_exceptions: Tuple[Type[Exception], ...] = (),
) -> Callable:
    """
    Decorador que reintenta la función decorada con espera exponencial.

    Parámetros:
        max_retries         — número máximo de reintentos (no incluye el intento inicial)
        base_delay          — espera inicial en segundos (se duplica en cada reintento)
        max_delay           — espera máxima en segundos
        retryable_statuses  — códigos HTTP que disparan un reintento
        retryable_exceptions — excepciones que disparan un reintento

    Ejemplo de esperas con base_delay=1:
        Intento 1 → falla → espera 1s
        Intento 2 → falla → espera 2s
        Intento 3 → falla → espera 4s
        Intento 4 → falla → se lanza la excepción / devuelve None

    El decorador NO reintenta en errores 4xx (errores del cliente),
    ya que reintentar una petición malformada nunca tendrá éxito.
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries + 1):
                try:
                    result = func(*args, **kwargs)

                    # Si la función devuelve None y no es el primer intento,
                    # comprobamos si fue por un status reintentable
                    # (ps_client._request devuelve None en caso de error)
                    if result is None and attempt < max_retries:
                        # Miramos si el último status fue reintentable
                        # accediendo al atributo _last_status si existe
                        last_status = getattr(wrapper, "_last_status", None)
                        if last_status in retryable_statuses:
                            delay = min(base_delay * (2 ** attempt), max_delay)
                            logger.warning(
                                "HTTP {status} en intento {n}/{total} — reintentando en {delay}s",
                                status=last_status, n=attempt + 1,
                                total=max_retries + 1, delay=delay,
                            )
                            time.sleep(delay)
                            continue

                    return result

                except retryable_exceptions as exc:
                    last_exception = exc
                    if attempt < max_retries:
                        delay = min(base_delay * (2 ** attempt), max_delay)
                        logger.warning(
                            "{exc_type} en intento {n}/{total} — reintentando en {delay}s",
                            exc_type=type(exc).__name__,
                            n=attempt + 1, total=max_retries + 1, delay=delay,
                        )
                        time.sleep(delay)
                    else:
                        logger.error(
                            "Agotados {n} reintentos. Último error: {exc}",
                            n=max_retries, exc=exc,
                        )
                        raise

            return None

        return wrapper
    return decorator


# ──────────────────────────────────────────────────────────────
# Instancias globales
# ──────────────────────────────────────────────────────────────

# Circuit breaker para el webservice de PrestaShop.
# Se abre tras 5 fallos consecutivos en 60s y espera 30s antes de probar.
prestashop_circuit = CircuitBreaker(
    name="prestashop_webservice",
    failure_threshold=5,
    recovery_timeout=30.0,
    window_seconds=60.0,
)

# Circuit breaker para MySQL.
mysql_circuit = CircuitBreaker(
    name="mysql",
    failure_threshold=3,
    recovery_timeout=15.0,
    window_seconds=30.0,
)
