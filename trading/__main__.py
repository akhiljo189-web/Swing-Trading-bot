"""Local paper simulation commands; no live-order interface."""
import argparse
import json
import os
import getpass
from pathlib import Path


def public_snapshot(snapshot):
    """Export only explicit simulation fields, never connectivity/account settings."""
    scalar = ('mode', 'data_source', 'cash', 'unsettled_cash', 'equity', 'contributions', 'pnl',
              'return_pct', 'drawdown_pct', 'paused', 'last_session')
    fields = {
        'positions': ('symbol', 'quantity', 'entry_price', 'stop_price', 'last_price', 'held_sessions'),
        'trades': ('date', 'symbol', 'side', 'quantity', 'price', 'fee', 'reason'),
        'equity_history': ('date', 'equity', 'contributions'),
    }
    result = {key: snapshot[key] for key in scalar if key in snapshot}
    for key, allowed in fields.items():
        result[key] = [{field: row[field] for field in allowed if field in row}
                       for row in snapshot.get(key, [])]
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='Local paper trading workbench. Broker orders are disabled.')
    parser.add_argument('--db', default='runtime/paper.db', help='Local SQLite path')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('serve', 'demo', 'import', 'deposit', 'status', 'export', 'ibkr-check'):
        command = sub.add_parser(name)
        command.add_argument('--db', default=argparse.SUPPRESS, help='Local SQLite path')
        if name == 'serve':
            command.add_argument('--port', type=int, default=8765)
        elif name in ('import', 'export'):
            command.add_argument('path')
        elif name == 'deposit':
            command.add_argument('amount', type=float)
            command.add_argument('--reference', required=True)
        elif name == 'ibkr-check':
            command.add_argument('--host', default='127.0.0.1')
            command.add_argument('--port', type=int, default=7497)
            command.add_argument('--timeout', type=float, default=10)
    args = parser.parse_args(argv)
    try:
        if args.command == 'ibkr-check':
            from .ibkr import check_connection
            account = os.environ.get('IBKR_PAPER_ACCOUNT') or getpass.getpass('Paper account (DU…, hidden): ')
            result = check_connection(host=args.host, port=args.port, account=account, timeout=args.timeout)
        else:
            from .core import Engine
            Path(args.db).parent.mkdir(parents=True, exist_ok=True)
            engine = Engine(args.db)
            engine.initialize()
            if args.command == 'serve':
                from .web import create_server
                server = create_server(engine, port=args.port)
                print(f'Local paper dashboard: http://127.0.0.1:{server.server_port}', flush=True)
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass
                finally:
                    server.server_close()
                return 0
            if args.command == 'demo':
                result = engine.demo()
            elif args.command == 'import':
                result = engine.process_csv(args.path)
            elif args.command == 'deposit':
                result = engine.deposit(args.amount, args.reference)
            elif args.command == 'export':
                target = Path(args.path)
                if target.resolve() == Path(args.db).resolve():
                    raise ValueError('Export path cannot overwrite the database')
                with target.open('x', encoding='utf-8') as handle:
                    json.dump(public_snapshot(engine.snapshot()), handle, indent=2, allow_nan=False)
                result = {'exported': str(target)}
            else:
                result = engine.snapshot()
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(2, f'Error: {exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())
