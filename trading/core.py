"""Causal completed-bar simulation. Prices and all accounting are GBP.

The CSV importer cannot verify exchange calendars, adjusted prices or earnings
claims: these are explicit source responsibilities, not broker integration.
"""
import csv
import json
import math
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path


SETTINGS = dict(fee=0.35, slippage_bps=5, position_limit=3,
                position_fraction=.25, risk_fraction=.005,
                aggregate_risk_fraction=.015, fee_risk_fraction=.2,
                drawdown_limit=.05, max_held_sessions=20)
FIELDS = ['date', 'symbol', 'open', 'high', 'low', 'close', 'volume',
          'asset_type', 'earnings_safe']


def _number(value, positive=True):
    if isinstance(value, bool):
        raise ValueError('Boolean is not a monetary amount')
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError('Expected a finite number') from None
    if not math.isfinite(value) or (value <= 0 if positive else value < 0):
        raise ValueError('Expected a positive finite number' if positive else 'Expected a nonnegative finite number')
    if value > 1e12:
        raise ValueError('Number exceeds supported range')
    return value


class Engine:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        if self.db_path == ':memory:':
            raise ValueError('Use a filesystem database for persistent operation')
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)')
            conn.execute('CREATE TABLE IF NOT EXISTS bars (date TEXT, symbol TEXT, body TEXT NOT NULL, PRIMARY KEY(date,symbol))')

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        try:
            conn.execute('BEGIN IMMEDIATE')
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _read(self, conn):
        row = conn.execute('SELECT body FROM state WHERE id=1').fetchone()
        if not row:
            raise ValueError('Initialize the paper account first')
        return json.loads(row[0])

    def _save(self, conn, state):
        conn.execute('INSERT OR REPLACE INTO state VALUES (1,?)', (json.dumps(state, allow_nan=False),))

    def initialize(self, initial_cash=1000, fee=0.35):
        initial_cash = _number(initial_cash)
        fee = _number(fee, positive=False)
        with self._connection() as conn:
            if conn.execute('SELECT 1 FROM state').fetchone():
                return
            self._save(conn, dict(mode='local-paper', data_source='none', cash=initial_cash,
                equity=initial_cash, contributions=initial_cash, pnl=0, return_pct=0,
                drawdown_pct=0, paused=False, last_session=None, positions=[], trades=[],
                events=[], equity_history=[], settings=dict(SETTINGS, fee=fee),
                unsettled_cash=0, units=initial_cash, peak_nav=1, deposits={},
                universe=[], signals=[], exit_signals=[]))

    def snapshot(self):
        with self._connection() as conn:
            state = self._read(conn)
        return {key: state[key] for key in ['mode','data_source','cash','equity','contributions',
            'pnl','return_pct','drawdown_pct','paused','last_session','positions','trades',
            'events','equity_history','settings','unsettled_cash']}

    def deposit(self, amount, reference):
        amount = _number(amount)
        if not isinstance(reference, str) or not reference.strip() or len(reference) > 200:
            raise ValueError('A nonempty deposit reference up to 200 characters is required')
        reference = reference.strip()
        with self._connection() as conn:
            state = self._read(conn)
            if reference in state['deposits']:
                if state['deposits'][reference] != amount:
                    raise ValueError('Deposit reference already used for a different amount')
                return
            nav = state['equity'] / state['units']
            if nav <= 0:
                raise ValueError('Cannot unitize a deposit into an exhausted account')
            state['units'] += amount / nav
            state['cash'] += amount
            state['contributions'] += amount
            state['deposits'][reference] = amount
            self._value(state)
            self._event(state, 'Virtual deposit recorded')
            self._save(conn, state)

    def set_paused(self, paused):
        if not isinstance(paused, bool):
            raise ValueError('paused must be true or false')
        with self._connection() as conn:
            state = self._read(conn)
            state['paused'] = paused or state['drawdown_pct'] >= state['settings']['drawdown_limit'] * 100
            self._event(state, 'New entries paused' if state['paused'] else 'New entries resumed')
            self._save(conn, state)

    def _event(self, state, message):
        state['events'].append(dict(date=state['last_session'], message=message))

    def _value(self, state):
        state['equity'] = state['cash'] + state['unsettled_cash'] + sum(p['quantity']*p['last_price'] for p in state['positions'])
        state['pnl'] = state['equity'] - state['contributions']
        nav = state['equity'] / state['units']
        state['peak_nav'] = max(state['peak_nav'], nav)
        state['return_pct'] = (nav-1)*100
        state['drawdown_pct'] = max(0, (1-nav/state['peak_nav'])*100)
        if state['drawdown_pct'] >= state['settings']['drawdown_limit']*100:
            if not state['paused']:
                self._event(state, 'Cash-flow-adjusted drawdown limit reached; new entries paused')
            state['paused'] = True

    def process_csv(self, path):
        with open(path, newline='', encoding='utf-8-sig') as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != FIELDS:
                raise ValueError('CSV header must be: ' + ','.join(FIELDS))
            rows = list(reader)
        return self._process(rows, 'csv-import')

    def _validate(self, rows):
        if not rows:
            raise ValueError('CSV contains no bars')
        sessions = {}
        previous = ''
        for raw in rows:
            if set(raw) != set(FIELDS) or any(value is None for value in raw.values()):
                raise ValueError('Incomplete CSV row')
            day = str(raw['date'])
            try:
                if date.fromisoformat(day).isoformat() != day:
                    raise ValueError()
            except ValueError:
                raise ValueError('Dates must use YYYY-MM-DD') from None
            if day > date.today().isoformat():
                raise ValueError('Future sessions are not completed bars')
            if day < previous:
                raise ValueError('Dates must be sorted in ascending order')
            previous = day
            symbol = str(raw['symbol'])
            if not re.fullmatch(r'[A-Z0-9][A-Z0-9._-]{0,23}', symbol):
                raise ValueError('Invalid symbol')
            bar = {key: _number(raw[key]) for key in ['open','high','low','close']}
            bar['volume'] = _number(raw['volume'], positive=False)
            if bar['low'] > min(bar['open'],bar['close']) or bar['high'] < max(bar['open'],bar['close']) or bar['low'] > bar['high']:
                raise ValueError('Inconsistent OHLC prices')
            asset_type = str(raw['asset_type']).lower()
            if asset_type not in ('stock', 'etf') or str(raw['earnings_safe']).lower() not in ('true','false'):
                raise ValueError('asset_type must be stock/etf; earnings_safe must be true/false')
            bar.update(date=day, symbol=symbol, asset_type=asset_type, earnings_safe=str(raw['earnings_safe']).lower() == 'true')
            bucket = sessions.setdefault(day, {})
            if symbol in bucket:
                raise ValueError('Duplicate symbol in session')
            bucket[symbol] = bar
        return sessions

    def _process(self, rows, source):
        sessions = self._validate(rows)
        processed = 0
        skipped = 0
        with self._connection() as conn:
            state = self._read(conn)
            if state['data_source'] not in ('none', source):
                raise ValueError('Data source is immutable; use a separate database')
            if source == 'synthetic-demo' and state['last_session']:
                raise ValueError('Demo requires an empty data history')
            universe = state['universe'] or sorted(next(iter(sessions.values())))
            classifications = {symbol: json.loads(body)['asset_type'] for symbol,body in conn.execute('SELECT symbol,body FROM bars WHERE date=?', (state['last_session'],))}
            # Validate entire batch before accepting any session.
            for day, bars in sessions.items():
                if sorted(bars) != universe:
                    raise ValueError('Every session must contain the complete synchronized universe, including held symbols')
                for symbol, bar in bars.items():
                    if classifications.setdefault(symbol,bar['asset_type']) != bar['asset_type']:
                        raise ValueError('Instrument asset classification cannot change across sessions')
                if state['last_session'] and day <= state['last_session']:
                    stored = {sym: json.loads(body) for sym,body in conn.execute('SELECT symbol,body FROM bars WHERE date=?', (day,))}
                    if stored != bars:
                        raise ValueError('Previously processed dates cannot be changed or inserted retrospectively')
            state['data_source'] = source
            state['universe'] = universe
            histories = {symbol: [] for symbol in universe}
            for symbol, body in conn.execute('SELECT symbol,body FROM bars ORDER BY date,symbol'):
                histories[symbol].append(json.loads(body))
            for day, bars in sessions.items():
                if state['last_session'] and day <= state['last_session']:
                    skipped += 1
                    continue
                self._session(state, day, bars, histories)
                for symbol, bar in bars.items():
                    conn.execute('INSERT INTO bars VALUES (?,?,?)', (day,symbol,json.dumps(bar)))
                    histories[symbol].append(bar)
                self._save(conn, state)
                processed += 1
        return dict(processed_sessions=processed, skipped_sessions=skipped, data_source=source)

    def _sell(self, state, position, price, reason):
        price *= 1 - state['settings']['slippage_bps']/10000
        fee = state['settings']['fee']
        state['unsettled_cash'] += position['quantity']*price-fee
        state['trades'].append(dict(date=state['last_session'],symbol=position['symbol'],side='SELL',
            quantity=position['quantity'],price=price,fee=fee,reason=reason))
        state['positions'].remove(position)

    def _session(self, state, day, bars, histories):
        if any(bars[p['symbol']]['volume'] <= 0 for p in state['positions']):
            raise ValueError('Held-symbol zero-volume session is unsupported; no fills or ledger changes accepted')
        state['last_session'] = day
        state['cash'] += state['unsettled_cash']
        state['unsettled_cash'] = 0
        config = state['settings']
        for position in list(state['positions']):
            bar = bars[position['symbol']]
            position['held_sessions'] += 1
            if bar['open'] <= position['stop_price']:
                self._sell(state, position, bar['open'], 'gap-stop')
            elif position['symbol'] in state['exit_signals']:
                self._sell(state, position, bar['open'], 'scheduled-exit')
            else:
                # Size at the opening mark, never at this session's later close.
                position['last_price'] = bar['open']
        self._value(state)
        if not state['paused']:
            for signal in state['signals']:
                if state['paused']:
                    break
                symbol = signal['symbol']
                bar = bars[symbol]
                if len(state['positions']) >= config['position_limit']:
                    break
                if any(p['symbol'] == symbol for p in state['positions']):
                    continue
                if bar['low'] > signal['limit'] or bar['volume'] <= 0:
                    continue
                # Earnings metadata must be known as safe on both signal and fill sessions.
                if bar['asset_type'] == 'stock' and not bar['earnings_safe']:
                    continue
                equity = state['equity']
                budget = equity*config['risk_fraction']
                roundtrip = 2*config['fee']
                if roundtrip > budget*config['fee_risk_fraction']:
                    self._event(state, f'{symbol}: skipped because fees exceed risk allowance')
                    continue
                price = signal['limit']
                stop = price-2*signal['atr']
                if stop <= 0:
                    continue
                loss_per_share = price-stop*(1-config['slippage_bps']/10000)
                open_risk = sum(p['quantity']*(p['entry_price']-p['stop_price']*(1-config['slippage_bps']/10000))+roundtrip for p in state['positions'])
                allowed = min(budget,equity*config['aggregate_risk_fraction']-open_risk)
                quantity = math.floor(min((allowed-roundtrip)/loss_per_share,
                    equity*config['position_fraction']/price,(state['cash']-config['fee'])/price))
                if quantity <= 0:
                    self._event(state, f'{symbol}: no whole share fits cash and risk limits')
                    continue
                state['cash'] -= quantity*price+config['fee']
                position = dict(symbol=symbol,quantity=quantity,entry_price=price,stop_price=stop,
                    last_price=price,held_sessions=1)
                state['positions'].append(position)
                state['trades'].append(dict(date=day,symbol=symbol,side='BUY',quantity=quantity,
                    price=price,fee=config['fee'],reason='prior-session-breakout'))
                self._value(state)
        # Keep existing exposure reserved during entry sizing. Intraday lows cannot
        # free slots or risk budgets before entries. Ambiguous new entries are
        # conservatively assumed to fill before their protective stop is touched.
        for position in list(state['positions']):
            bar = bars[position['symbol']]
            if bar['low'] <= position['stop_price']:
                is_new = position['held_sessions'] == 1
                self._sell(state,position,min(position['stop_price'],bar['open']),
                           'entry-session-stop' if is_new else 'stop')
        state['signals'] = []
        state['exit_signals'] = []
        for position in state['positions']:
            symbol = position['symbol']
            position['last_price'] = bars[symbol]['close']
            history = histories[symbol]
            if position['held_sessions'] >= config['max_held_sessions'] or (len(history)>=10 and bars[symbol]['close'] < min(b['low'] for b in history[-10:])):
                state['exit_signals'].append(symbol)
        for symbol in sorted(bars):
            bar = bars[symbol]
            history = histories[symbol]
            if len(history) < 199 or bar['volume'] <= 0:
                continue
            if bar['asset_type'] == 'stock' and not bar['earnings_safe']:
                continue
            closes = [b['close'] for b in history[-199:]] + [bar['close']]
            high20 = max(b['high'] for b in history[-20:])
            sma200 = sum(closes)/200
            if bar['close'] <= high20 or bar['close'] <= sma200 or sum(closes[-50:])/50 <= sma200:
                continue
            recent = history[-14:] + [bar]
            atr = sum(max(recent[i]['high']-recent[i]['low'],abs(recent[i]['high']-recent[i-1]['close']),abs(recent[i]['low']-recent[i-1]['close'])) for i in range(1,15))/14
            if atr > 0:
                state['signals'].append(dict(symbol=symbol,atr=atr,limit=bar['close']+.25*atr,rank=bar['close']/high20-1))
        state['signals'].sort(key=lambda s:(-s['rank'],s['symbol']))
        self._value(state)
        state['equity_history'].append(dict(date=day,equity=state['equity'],contributions=state['contributions']))

    def demo(self):
        rows = []
        start = date(2023,1,2)
        for i in range(280):
            for index, symbol in enumerate(['DEMO-A','DEMO-B','DEMO-C']):
                close = 15+index*4+i*.035+math.sin(i/8+index)*.3
                rows.append(dict(date=(start+timedelta(days=i)).isoformat(),symbol=symbol,
                    open=close-.025,high=close+.08,low=close-.10,close=close,volume=100000,
                    asset_type='etf',earnings_safe='true'))
        return self._process(rows, 'synthetic-demo')
