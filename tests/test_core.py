import csv
import json
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from trading.core import Engine


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.engine = Engine(str(self.path / 'paper.db'))
        self.engine.initialize()

    def rows(self, count=200):
        return [dict(date=(date(2020, 1, 1) + timedelta(days=i)).isoformat(),
                     symbol='TEST', open=100+i*.01, high=100+i*.01+.02,
                     low=100+i*.01-.02, close=100+i*.01, volume=100000,
                     asset_type='etf', earnings_safe='false') for i in range(count)]

    def append(self, rows, price, low=None):
        i = len(rows)
        rows.append(dict(rows[-1], date=(date(2020, 1, 1)+timedelta(days=i)).isoformat(),
                         open=price, close=price, high=price+.02,
                         low=price-.02 if low is None else low))
        return rows

    def run_rows(self, rows):
        path = self.path / 'bars.csv'
        with path.open('w', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=['date','symbol','open','high','low','close','volume','asset_type','earnings_safe'])
            writer.writeheader()
            writer.writerows(rows)
        return self.engine.process_csv(str(path))

    def entry(self):
        rows = self.append(self.rows(), 102.10)
        self.run_rows(rows)
        self.assertEqual(self.engine.snapshot()['positions'], [])
        self.append(rows, 102.12, 102.10)
        self.run_rows(rows)
        self.assertEqual(len(self.engine.snapshot()['positions']), 1)
        return rows

    def test_causal_entry_cost_sizing_and_replay(self):
        rows = self.entry()
        before = self.engine.snapshot()
        position = before['positions'][0]
        self.assertLessEqual(position['quantity'] * position['entry_price'], 250)
        self.assertLessEqual(position['quantity'] * (position['entry_price'] - position['stop_price']) + .7, 5)
        self.run_rows(rows)
        self.assertEqual(before, self.engine.snapshot())
        self.assertEqual(before, Engine(str(self.path / 'paper.db')).snapshot())

    def test_gap_stop_pause_and_unitized_deposit(self):
        rows = self.entry()
        self.engine.set_paused(True)
        self.append(rows, .1, .08)
        self.run_rows(rows)
        state = self.engine.snapshot()
        self.assertFalse(state['positions'])
        self.assertTrue(state['paused'])
        self.assertAlmostEqual(state['trades'][-1]['price'], .1 * .9995)
        self.assertGreater(state['drawdown_pct'], 5)
        prior_return = state['return_pct']
        self.engine.deposit(500, 'monthly')
        self.assertAlmostEqual(self.engine.snapshot()['return_pct'], prior_return)
        state = self.engine.snapshot()
        self.engine.deposit(500, 'monthly')
        self.assertEqual(state, self.engine.snapshot())
        with self.assertRaises(ValueError):
            self.engine.deposit(600, 'monthly')
        self.engine.set_paused(False)
        self.assertTrue(self.engine.snapshot()['paused'])

    def test_automatic_drawdown_pause(self):
        rows = self.entry()
        self.append(rows, .1, .08)
        self.run_rows(rows)
        self.assertTrue(self.engine.snapshot()['paused'])

    def test_reject_bad_and_missing_data_atomically(self):
        rows = self.entry()
        state = self.engine.snapshot()
        bad = self.append(rows.copy(), 102.2)
        bad[-1]['symbol'] = 'OTHER'
        with self.assertRaises(ValueError):
            self.run_rows(bad)
        self.assertEqual(state, self.engine.snapshot())
        for field, value in [('close', 'nan'), ('volume', '-1'), ('earnings_safe', 'yes')]:
            bad = [dict(rows[0], **{field: value})]
            with self.assertRaises(ValueError):
                self.run_rows(bad)
        with self.assertRaises(ValueError):
            self.run_rows(list(reversed(rows)))
        altered = [dict(rows[0], close=100.001)]
        with self.assertRaises(ValueError):
            self.run_rows(altered)

    def test_invalid_deposit_and_initialization(self):
        for value in [-1, 0, float('nan'), True]:
            with self.assertRaises(ValueError):
                self.engine.deposit(value, 'bad')
        self.engine.initialize(2000)
        self.assertEqual(self.engine.snapshot()['contributions'], 1000)

    def test_demo_source_is_immutable(self):
        self.engine.demo()
        self.assertEqual(self.engine.snapshot()['data_source'], 'synthetic-demo')
        with self.assertRaises(ValueError):
            self.run_rows(self.rows())
        with self.assertRaises(ValueError):
            self.engine.demo()

    def test_fees_block_small_account(self):
        self.engine = Engine(str(self.path / 'small.db'))
        self.engine.initialize(100)
        rows = self.append(self.rows(), 102.10)
        self.append(rows, 102.12, 102.10)
        self.run_rows(rows)
        self.assertFalse(self.engine.snapshot()['trades'])
        self.assertIn('fees exceed', self.engine.snapshot()['events'][-1]['message'])

    def test_whole_share_cap_and_earnings_block_entries(self):
        rows = self.append(self.rows(), 102.10)
        self.append(rows, 102.12, 102.10)
        for bar in rows:
            bar['asset_type'] = 'stock'
        self.run_rows(rows)
        self.assertFalse(self.engine.snapshot()['trades'])
        self.engine = Engine(str(self.path / 'expensive.db'))
        self.engine.initialize()
        for bar in rows:
            bar['asset_type'] = 'etf'
            for field in ['open','high','low','close']:
                bar[field] *= 10
        self.run_rows(rows)
        self.assertFalse(self.engine.snapshot()['trades'])

    def test_sale_cash_unavailable_until_following_session(self):
        # An explicit ledger fixture isolates settlement from signal generation.
        with sqlite3.connect(self.engine.db_path) as conn:
            state = json.loads(conn.execute('SELECT body FROM state').fetchone()[0])
            state.update(cash=0, positions=[dict(symbol='HELD', quantity=10,
                entry_price=100, stop_price=99, last_price=100, held_sessions=2)],
                signals=[dict(symbol='NEW', limit=10, atr=.1, rank=.1)])
            conn.execute('UPDATE state SET body=?', (json.dumps(state),))
        conn.close()
        bars = [dict(date='2020-01-01',symbol=symbol,open=price,close=price,
                     high=price+.01,low=price-.01,volume=1000,asset_type='etf',
                     earnings_safe='true') for symbol,price in [('HELD',98),('NEW',10)]]
        self.run_rows(bars)
        state = self.engine.snapshot()
        self.assertEqual([t['side'] for t in state['trades']], ['SELL'])
        self.assertEqual(state['cash'], 0)
        self.assertGreater(state['unsettled_cash'], 900)
        for bar in bars:
            bar['date'] = '2020-01-02'
        self.run_rows(bars)
        state = self.engine.snapshot()
        self.assertEqual(state['unsettled_cash'], 0)
        self.assertGreater(state['cash'], 900)

    def test_zero_volume_cannot_fill(self):
        rows = self.append(self.rows(),102.10)
        self.append(rows,102.12,102.10)
        rows[-1]['volume'] = 0
        self.run_rows(rows)
        self.assertFalse(self.engine.snapshot()['trades'])

    def test_asset_type_case_and_immutable_classification(self):
        rows = self.rows()
        for bar in rows:
            bar['asset_type'] = 'ETF'
        self.run_rows(rows)
        self.append(rows,102.10)
        rows[-1]['asset_type'] = 'STOCK'
        with self.assertRaises(ValueError):
            self.run_rows(rows)

    def test_held_zero_volume_rejected_without_mutation(self):
        rows = self.entry()
        before = self.engine.snapshot()
        self.append(rows, .1, .08)
        rows[-1]['volume'] = 0
        with self.assertRaises(ValueError):
            self.run_rows(rows)
        self.assertEqual(before,self.engine.snapshot())

    def test_intraday_stops_do_not_free_entry_slots(self):
        with sqlite3.connect(self.engine.db_path) as conn:
            state = json.loads(conn.execute('SELECT body FROM state').fetchone()[0])
            state.update(cash=700, positions=[dict(symbol=symbol, quantity=10,
                entry_price=10, stop_price=9.9, last_price=10, held_sessions=2)
                for symbol in ['A','B','C']],
                signals=[dict(symbol='NEW', limit=10, atr=.1, rank=.1)])
            conn.execute('UPDATE state SET body=?', (json.dumps(state),))
        conn.close()
        bars = [dict(date='2020-01-01',symbol=symbol,open=10,close=10,
                     high=10.1,low=9,volume=1000,asset_type='etf',
                     earnings_safe='true') for symbol in ['A','B','C','NEW']]
        self.run_rows(bars)
        self.assertEqual([t['side'] for t in self.engine.snapshot()['trades']], ['SELL']*3)


if __name__ == '__main__':
    unittest.main()
