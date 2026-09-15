import json
import threading
import time
import types
import unittest
from unittest.mock import patch

from trading import ibkr


class Transport:
    def __init__(self, callbacks, events=()):
        self.callbacks = callbacks
        self.events = events
        self.closed = threading.Event()

    def connect(self, host, port, clientId):
        pass

    def run(self):
        for method, args in self.events:
            getattr(self.callbacks, method)(*args)
        self.closed.wait(1)

    def disconnect(self):
        self.closed.set()


class IbkrTests(unittest.TestCase):
    def probe(self, events, **kwargs):
        transports = []
        def factory(callbacks):
            transport = Transport(callbacks, events)
            transports.append(transport)
            return transport
        with patch.object(ibkr, '_create_client', side_effect=factory):
            result = ibkr.check_connection(account='DU123456', timeout=0.03, **kwargs)
        self.assertTrue(transports[0].closed.is_set())
        self.assertNotIn('DU123456', json.dumps(result))
        self.assertTrue(result['read_only'])
        self.assertFalse(result['order_submission_enabled'])
        return result

    def test_explicit_account_and_endpoint_required_before_import(self):
        for kwargs in ({}, {'account': 'U123456'}, {'account': 'DU*'},
                       {'account': 'DU123456', 'host': 'example.com'},
                       {'account': 'DU123456', 'port': 7496},
                       {'account': 'DU123456', 'port': 4001},
                       {'account': 'DU123456', 'timeout': float('nan')},
                       {'account': 'DU123456', 'timeout': 0}):
            with self.subTest(kwargs=kwargs), patch.object(ibkr, '_create_client') as factory:
                self.assertEqual(ibkr.check_connection(**kwargs)['status'], 'invalid_configuration')
                factory.assert_not_called()

    def test_both_handshake_callbacks_required_in_either_order(self):
        events = [('managedAccounts', ('DU123456',)), ('nextValidId', (10,))]
        for ordering in (events, list(reversed(events))):
            self.assertEqual(self.probe(ordering)['status'], 'connected')
        for event in events:
            self.assertEqual(self.probe([event])['status'], 'timeout')

    def test_wrong_live_mixed_or_empty_accounts_rejected(self):
        for accounts in ('DU654321', 'U123456', 'DU123456,U987654',
                         'DU123456,DU654321', '', 'DU123456,DU123456'):
            with self.subTest(accounts=accounts):
                result = self.probe([('managedAccounts', (accounts,)), ('nextValidId', (1,))])
                self.assertEqual(result['status'], 'account_rejected')

    def test_connection_loss_and_error_are_redacted(self):
        self.assertEqual(self.probe([('connectionClosed', ())])['status'], 'connection_lost')
        for error in ((-1, 502, 'secret DU123456'),
                      (-1, 1234567890, 502, 'secret DU123456', '')):
            result = self.probe([('error', error)])
            self.assertEqual(result['status'], 'broker_error')
            self.assertNotIn('secret', json.dumps(result))

    def test_farm_information_does_not_fail_handshake(self):
        result = self.probe([('error', (-1, 2104, 'farm OK')),
                             ('managedAccounts', ('DU123456',)), ('nextValidId', (1,))], port=4002)
        self.assertEqual(result['status'], 'connected')

    def test_dependency_unavailable_is_a_normal_result(self):
        with patch.object(ibkr, '_create_client', side_effect=ImportError('private path')):
            result = ibkr.check_connection(account='DU123456')
        self.assertEqual(result['status'], 'sdk_unavailable')
        self.assertNotIn('private', json.dumps(result))

    def test_connect_exception_disconnects(self):
        transport = Transport(None)
        def broken(*args, **kwargs):
            raise OSError('DU123456 secret')
        transport.connect = broken
        with patch.object(ibkr, '_create_client', return_value=transport):
            result = ibkr.check_connection(account='DU123456', timeout=0.1)
        self.assertEqual(result['status'], 'connection_failed')
        self.assertTrue(transport.closed.is_set())
        self.assertNotIn('secret', json.dumps(result))

    def test_official_adapter_forwards_callbacks(self):
        class Wrapper:
            pass
        class Client(Transport):
            def __init__(self, wrapper):
                super().__init__(wrapper, [('managedAccounts', ('DU123456',)),
                                           ('nextValidId', (3,))])
        modules = {'ibapi': types.ModuleType('ibapi'),
                   'ibapi.client': types.ModuleType('ibapi.client'),
                   'ibapi.wrapper': types.ModuleType('ibapi.wrapper')}
        modules['ibapi.client'].EClient = Client
        modules['ibapi.wrapper'].EWrapper = Wrapper
        with patch.dict('sys.modules', modules):
            result = ibkr.check_connection(account='DU123456', timeout=0.1)
        self.assertEqual(result['status'], 'connected')

    def test_timeout_during_connect_closes_even_after_late_return(self):
        release = threading.Event()
        transport = Transport(None)
        transport.connect = lambda *args, **kwargs: release.wait(2)
        started = time.monotonic()
        try:
            with patch.object(ibkr, '_create_client', return_value=transport):
                result = ibkr.check_connection(account='DU123456', timeout=0.02)
            self.assertEqual(result['status'], 'timeout')
            self.assertLess(time.monotonic() - started, 0.8)
            self.assertTrue(transport.closed.is_set())
        finally:
            release.set()


if __name__ == '__main__':
    unittest.main()
