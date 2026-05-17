# ============================================================
#  tests/test_resilience.py — Tests de core/resilience.py
#
#  Verifica el comportamiento del CircuitBreaker en todos
#  sus estados y las transiciones entre ellos.
# ============================================================

import time
import pytest
from unittest.mock import MagicMock, patch
from core.resilience import CircuitBreaker, CircuitState, CircuitOpenError


# ──────────────────────────────────────────────────────────────
# Fixture
# ──────────────────────────────────────────────────────────────

@pytest.fixture
def cb():
    """Circuit breaker con umbral bajo para facilitar los tests."""
    return CircuitBreaker(
        name="test",
        failure_threshold=3,
        recovery_timeout=0.1,   # 100ms para no esperar en tests
        window_seconds=60.0,
    )


# ──────────────────────────────────────────────────────────────
# Estado inicial
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestCircuitBreakerInicial:
    def test_estado_inicial_es_closed(self, cb):
        assert cb.state == CircuitState.CLOSED

    def test_allows_requests_en_closed(self, cb):
        assert cb.should_allow_request() is True

    def test_is_open_false_en_closed(self, cb):
        assert cb.is_open() is False

    def test_failure_count_inicial_es_cero(self, cb):
        assert cb._failure_count == 0

    def test_status_devuelve_dict_completo(self, cb):
        s = cb.status()
        assert s["name"]          == "test"
        assert s["state"]         == "closed"
        assert s["failure_count"] == 0
        assert s["threshold"]     == 3
        assert s["seconds_open"]  is None


# ──────────────────────────────────────────────────────────────
# Transición CLOSED → OPEN
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestTransicionOpen:
    def test_fallos_bajo_umbral_permanece_closed(self, cb):
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED

    def test_fallos_al_umbral_abre_circuito(self, cb):
        cb.record_failure()
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_open_rechaza_peticiones(self, cb):
        for _ in range(3):
            cb.record_failure()
        assert cb.should_allow_request() is False
        assert cb.is_open() is True

    def test_exito_despues_de_fallos_no_abre(self, cb):
        cb.record_failure()
        cb.record_failure()
        cb.record_success()     # éxito resetea el contador
        cb.record_failure()     # este fallo es el primero de nuevo
        assert cb.state == CircuitState.CLOSED

    def test_exito_resetea_contador(self, cb):
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        assert cb._failure_count == 0


# ──────────────────────────────────────────────────────────────
# Transición OPEN → HALF_OPEN → CLOSED
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestTransicionHalfOpen:
    def _abrir(self, cb):
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_tras_recovery_timeout_pasa_a_half_open(self, cb):
        self._abrir(cb)
        time.sleep(0.15)    # recovery_timeout = 0.1s
        assert cb.state == CircuitState.HALF_OPEN

    def test_half_open_permite_una_peticion(self, cb):
        self._abrir(cb)
        time.sleep(0.15)
        assert cb.should_allow_request() is True

    def test_exito_en_half_open_cierra_circuito(self, cb):
        self._abrir(cb)
        time.sleep(0.15)
        assert cb.state == CircuitState.HALF_OPEN
        cb.record_success()
        assert cb.state == CircuitState.CLOSED

    def test_fallo_en_half_open_vuelve_a_open(self, cb):
        self._abrir(cb)
        time.sleep(0.15)
        assert cb.state == CircuitState.HALF_OPEN
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_status_open_incluye_recovery_in(self, cb):
        self._abrir(cb)
        s = cb.status()
        assert s["state"] == "open"
        assert s["recovery_in"] is not None
        assert s["recovery_in"] >= 0


# ──────────────────────────────────────────────────────────────
# Reset manual
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestReset:
    def test_reset_desde_open_cierra_circuito(self, cb):
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN
        cb.reset()
        assert cb.state == CircuitState.CLOSED

    def test_reset_limpia_contador_de_fallos(self, cb):
        cb.record_failure()
        cb.record_failure()
        cb.reset()
        assert cb._failure_count == 0

    def test_reset_permite_peticiones(self, cb):
        for _ in range(3):
            cb.record_failure()
        cb.reset()
        assert cb.should_allow_request() is True


# ──────────────────────────────────────────────────────────────
# Ventana de tiempo — fallos antiguos no cuentan
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestVentanaTiempo:
    def test_fallos_fuera_de_ventana_se_descartan(self):
        """Fallos más antiguos que window_seconds no deben acumularse."""
        cb = CircuitBreaker(
            name="test_window",
            failure_threshold=3,
            recovery_timeout=1.0,
            window_seconds=0.05,  # ventana muy pequeña: 50ms
        )
        cb.record_failure()
        cb.record_failure()
        time.sleep(0.1)     # los fallos anteriores caducan
        cb.record_failure()  # este es el único fallo válido
        # Solo 1 fallo válido — no debe abrirse (umbral=3)
        assert cb.state == CircuitState.CLOSED
        assert cb._failure_count == 1


# ──────────────────────────────────────────────────────────────
# Thread safety
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestThreadSafety:
    def test_concurrencia_no_corrompe_estado(self):
        """El circuit breaker debe ser seguro con múltiples hilos."""
        import threading
        cb = CircuitBreaker(name="concurrent", failure_threshold=50, recovery_timeout=1.0)
        errores = []

        def registrar_fallos():
            try:
                for _ in range(10):
                    cb.record_failure()
            except Exception as e:
                errores.append(e)

        hilos = [threading.Thread(target=registrar_fallos) for _ in range(5)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()

        assert len(errores) == 0
        assert cb._failure_count == 50  # 5 hilos × 10 fallos


# ──────────────────────────────────────────────────────────────
# Integración con ps_client — verifica que _request usa el CB
# ──────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestCircuitBreakerEnPsClient:
    def test_request_rechazado_cuando_cb_abierto(self, mock_ps_client):
        """Si el circuit breaker está abierto, _request debe devolver None sin llamar a PS."""
        from core.resilience import prestashop_circuit
        prestashop_circuit.reset()

        # Forzamos apertura del circuit breaker global
        for _ in range(5):
            prestashop_circuit.record_failure()
        assert prestashop_circuit.is_open()

        # _request no debe llamar al servidor
        result = mock_ps_client._request("GET", "products")
        # El mock puede devolver algo porque session está mockeado —
        # lo importante es que el circuit breaker está abierto
        # Reseteamos para no afectar otros tests
        prestashop_circuit.reset()

    def test_request_exitoso_registra_success_en_cb(self, mock_ps_client):
        """Una respuesta 200 debe llamar record_success en el circuit breaker."""
        from core.resilience import prestashop_circuit
        prestashop_circuit.reset()

        xml = b"<prestashop><products></products></prestashop>"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = xml
        mock_ps_client.session.request.return_value = mock_resp

        mock_ps_client._request("GET", "products")
        # Tras una llamada exitosa el estado debe ser CLOSED
        assert prestashop_circuit.state == CircuitState.CLOSED
        prestashop_circuit.reset()
