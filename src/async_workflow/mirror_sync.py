"""Periodically publish the display projection; failures never affect processing."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

import httpx

from .core import Settings, atomic, now
from .dashboard import Inventory
from .mirror import MAX_BYTES, project, validate


def publish(inventory, connection):
    url = connection['url']
    if not url.startswith('https://'):
        raise ValueError('HTTPS required')
    data = validate(project(inventory))
    body = json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(body) > MAX_BYTES:
        raise ValueError('Snapshot too large')
    with httpx.Client(timeout=60, follow_redirects=False, trust_env=False) as client:
        response = client.post(url.rstrip('/') + '/api/sync', content=body,
            headers={'Authorization': 'Bearer ' + connection['publisher_token'], 'Content-Type': 'application/json'})
        response.raise_for_status()
        receipt = response.json()
    if receipt.get('total') != len(data['recordings']) or receipt.get('updated_at') != data['updated_at']:
        raise ValueError('Readback mismatch')
    return {'checked_at': now(), 'status': 'synced', 'total': receipt['total'],
            'source_updated_at': receipt['updated_at'], 'bytes': len(body)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    cfg = Settings.load()
    inventory = Inventory(cfg)
    status_file = cfg.root / 'mirror-status.json'
    published_at = catalog_at = 0.0
    while True:
        try:
            connection = json.loads((cfg.root / 'mirror-connection.json').read_text(encoding='utf-8-sig'))
            from .capture_control import exchange
            from .capture_catalog_sync import sync_catalog
            import asyncio
            with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
                try:
                    exchange(cfg, client, connection)
                    if time.monotonic() - catalog_at >= 60:
                        catalog = asyncio.run(sync_catalog(cfg))
                        response = client.post(connection['url'].rstrip('/') + '/api/capture/worker/catalog',
                            json=catalog, headers={'Authorization': 'Bearer ' + connection['publisher_token']})
                        response.raise_for_status()
                        catalog_at = time.monotonic()
                    atomic(cfg.root / 'capture-sync-status.json', {'checked_at': now(), 'status': 'synced'})
                except Exception as exc:
                    # Catalog/plugin outages do not stop the existing recording viewer.
                    atomic(cfg.root / 'capture-sync-status.json', {'checked_at': now(), 'status': 'failed',
                                                                 'error_type': type(exc).__name__})
            if args.once or time.monotonic() - published_at >= 60:
                receipt = publish(inventory, connection)
                atomic(status_file, receipt)
                print(json.dumps(receipt), flush=True)
                published_at = time.monotonic()
        except Exception as exc:
            # Exception messages can contain credentials, source paths or response bodies.
            atomic(status_file, {'checked_at': now(), 'status': 'failed', 'error_type': type(exc).__name__})
            print('Mirror sync failed: ' + type(exc).__name__, flush=True)
            if args.once:
                raise SystemExit(1) from None
        if args.once:
            return
        time.sleep(5)


if __name__ == '__main__':
    main()
