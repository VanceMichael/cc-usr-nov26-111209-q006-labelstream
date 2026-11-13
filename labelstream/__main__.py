DESCRIPTION = '物流标签 ZPL 作业的指令边界检查与原始作业归档。'
DEMO_PATH = '/job'
DEMO_STREAMS = ('sample', 'escape', 'graphic', 'recursive', 'unbound')

import argparse
import json
import tempfile
import threading
from pathlib import Path

from .archive import Archive
from .extract import ExtractStore, run_extract
from .parser import inspect_document
from .source_server import FixtureServer
from .transport import fetch


def main():
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    commands = parser.add_subparsers(dest='command', required=True)
    inspect = commands.add_parser('inspect')
    inspect.add_argument('path')
    capture = commands.add_parser('capture')
    capture.add_argument('url')
    capture.add_argument('--db', default='captures.sqlite3')
    capture.add_argument('--objects', default='objects')
    show = commands.add_parser('show')
    show.add_argument('--db', default='captures.sqlite3')
    show.add_argument('--objects', default='objects')
    extract = commands.add_parser('extract')
    extract.add_argument('--url', required=True)
    extract.add_argument('--db', default='captures.sqlite3')
    extract.add_argument('--objects', default='objects')
    extract.add_argument('--chunk', type=int, default=4096)
    jobs = commands.add_parser('jobs')
    jobs.add_argument('--db', default='captures.sqlite3')
    jobs.add_argument('--objects', default='objects')
    labels = commands.add_parser('labels')
    labels.add_argument('--job')
    labels.add_argument('--db', default='captures.sqlite3')
    labels.add_argument('--objects', default='objects')
    isolated = commands.add_parser('isolated')
    isolated.add_argument('--job')
    isolated.add_argument('--db', default='captures.sqlite3')
    isolated.add_argument('--objects', default='objects')
    commands.add_parser('demo')
    arguments = parser.parse_args()
    if arguments.command == 'inspect':
        result = inspect_document(Path(arguments.path).read_bytes())
    elif arguments.command in {'capture', 'show'}:
        archive = Archive(arguments.db, arguments.objects)
        try:
            if arguments.command == 'capture':
                status, content_type, body = fetch(arguments.url)
                result = {'capture_id': archive.save(arguments.url, status, content_type, body), 'http_status': status}
            else:
                result = archive.rows()
        finally:
            archive.close()
    elif arguments.command == 'extract':
        store = ExtractStore(arguments.db, arguments.objects)
        try:
            result = run_extract(store, arguments.url, arguments.chunk)
        finally:
            store.close()
    elif arguments.command in {'jobs', 'labels', 'isolated'}:
        store = ExtractStore(arguments.db, arguments.objects)
        try:
            if arguments.command == 'jobs':
                result = store.jobs()
            elif arguments.command == 'labels':
                result = store.events(kind='label', stream_id=arguments.job)
            else:
                result = store.events(kind='isolated', stream_id=arguments.job)
        finally:
            store.close()
    else:
        with FixtureServer() as server, tempfile.TemporaryDirectory() as directory:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = 'http://127.0.0.1:' + str(server.server_address[1])
                status, content_type, body = fetch(base + DEMO_PATH)
                archive = Archive(Path(directory) / 'demo.sqlite3', Path(directory) / 'objects')
                try:
                    capture_id = archive.save(base + DEMO_PATH, status, content_type, body)
                    observation = inspect_document(archive.read(capture_id))
                finally:
                    archive.close()
                store = ExtractStore(Path(directory) / 'extract.sqlite3', Path(directory) / 'extract-objects')
                try:
                    summaries = [run_extract(store, base + '/stream?name=' + name, chunk=18)
                                 for name in DEMO_STREAMS]
                    printed = store.events(kind='label', stream_id='print-gateway-sample')
                    redefinition = {
                        'stream': 'print-gateway-sample',
                        'prints': [{
                            'range': event['range'],
                            'copies': event['copies'],
                            'format': event['formats'],
                            'fixed_text': [field['text'] for field in event['fields'] if field['n'] is None],
                            'variables': [{'n': field['n'], 'text': field['text']}
                                          for field in event['fields'] if field['n'] is not None],
                        } for event in printed],
                    }
                    isolated_events = store.events(kind='isolated')
                finally:
                    store.close()
                result = {
                    'capture': {'capture_id': capture_id, 'http_status': status, 'observation': observation},
                    'extract': {'jobs': summaries, 'redefinition': redefinition, 'isolated': isolated_events},
                }
            finally:
                server.shutdown()
                thread.join(timeout=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
