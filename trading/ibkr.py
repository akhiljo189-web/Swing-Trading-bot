"""Optional, one-shot IBKR paper handshake. No order or market-data requests."""

import ipaddress
import math
import re
import secrets
import threading


_MESSAGES = {
    'connected': 'Paper account handshake verified; connected execution is disabled.',
    'invalid_configuration': 'Use a loopback address, paper port, explicit DU account and timeout of 0–60 seconds (exclusive of zero).',
    'sdk_unavailable': 'Install the official IBKR TWS Python API into this Python environment.',
    'account_rejected': 'The session must expose only the explicitly allowed paper account.',
    'timeout': 'Paper handshake timed out; check TWS or Gateway and API settings.',
    'connection_lost': 'The API connection closed before verification completed.',
    'connection_failed': 'The API connection could not be established.',
    'broker_error': 'The broker reported an API error; inspect TWS or Gateway locally.',
}


def _result(status):
    return {'status': status, 'message': _MESSAGES[status], 'read_only': True,
            'order_submission_enabled': False, 'paper_verified': status == 'connected',
            'broker_read_only_setting_verified': False}


def _create_client(callbacks):
    # Import only when the optional readiness check is requested.
    from ibapi.client import EClient
    from ibapi.wrapper import EWrapper

    class Wrapper(EWrapper):
        def managedAccounts(self, accountsList):
            callbacks.managedAccounts(accountsList)

        def nextValidId(self, orderId):
            callbacks.nextValidId(orderId)

        def connectionClosed(self):
            callbacks.connectionClosed()

        def error(self, *args, **kwargs):
            callbacks.error(*args, **kwargs)

    return EClient(Wrapper())


class _Handshake:
    def __init__(self, account):
        self.account = account
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.cancelled = threading.Event()
        self.accounts_ok = False
        self.id_ok = False
        self.failure = None

    def fail(self, status):
        with self.lock:
            if not self.cancelled.is_set():
                self.failure = status
                self.done.set()

    def managedAccounts(self, accountsList):
        accounts = accountsList.split(',') if isinstance(accountsList, str) else []
        with self.lock:
            if accounts != [self.account]:
                self.failure = 'account_rejected'
                self.done.set()
                return
            self.accounts_ok = True
            if self.id_ok:
                self.done.set()

    def nextValidId(self, orderId):
        with self.lock:
            if not isinstance(orderId, int) or isinstance(orderId, bool) or orderId < 0:
                self.failure = 'broker_error'
                self.done.set()
                return
            self.id_ok = True
            if self.accounts_ok:
                self.done.set()

    def connectionClosed(self):
        self.fail('connection_lost')

    def error(self, *args, **kwargs):
        # Older SDK: reqId, code, text[, advancedJSON]. New SDK inserts
        # errorTime after reqId. Do not retain or return free-form broker text.
        code = kwargs.get('errorCode')
        if code is None and len(args) >= 3:
            code = args[2] if isinstance(args[2], int) else args[1]
        if code not in {2104, 2106, 2107, 2108, 2158}:
            self.fail('broker_error')


def check_connection(host='127.0.0.1', port=7497, account=None, timeout=10):
    """Verify one explicitly allowed DU account, then disconnect.

    The read_only flag describes this checker, not the broker's GUI setting.
    Account identifiers and raw SDK errors never appear in the returned dict.
    """
    try:
        endpoint = '127.0.0.1' if host == 'localhost' else str(ipaddress.ip_address(host))
        valid = (ipaddress.ip_address(endpoint).is_loopback
                 and type(port) is int and port in {7497, 4002}
                 and isinstance(account, str) and re.fullmatch(r'DU[0-9]+', account)
                 and type(timeout) in {int, float} and math.isfinite(timeout)
                 and 0 < timeout <= 60)
    except (ValueError, TypeError):
        valid = False
    if not valid:
        return _result('invalid_configuration')

    state = _Handshake(account)
    try:
        client = _create_client(state)
    except ImportError:
        return _result('sdk_unavailable')
    except Exception:
        return _result('connection_failed')

    def disconnect():
        try:
            client.disconnect()
        except Exception:
            pass

    def worker():
        try:
            # Nonzero client ID avoids the special manual-order binding role.
            client.connect(endpoint, port, clientId=secrets.randbelow(900000) + 100000)
            if not state.cancelled.is_set():
                client.run()
            if not state.done.is_set():
                state.fail('connection_lost')
        except Exception:
            state.fail('connection_failed')
        finally:
            disconnect()

    thread = threading.Thread(target=worker, name='ibkr-readiness', daemon=True)
    try:
        thread.start()
        completed = state.done.wait(timeout)
        with state.lock:
            status = (state.failure or 'connected') if completed else 'timeout'
            state.cancelled.set()
    finally:
        state.cancelled.set()
        disconnect()
        if thread.ident is not None:
            thread.join(timeout=0.2)
    return _result(status)
