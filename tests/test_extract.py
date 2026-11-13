import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from labelstream.extract import ExtractStore, Interpreter, run_extract
from labelstream.source_server import FixtureServer

FIXTURES = Path('labelstream/fixtures')


def interpret(data, chunk_sizes):
    interpreter = Interpreter()
    events = []
    position = 0
    index = 0
    while position < len(data):
        size = chunk_sizes[index % len(chunk_sizes)]
        events.extend(interpreter.feed(data[position:position + size]))
        position += size
        index += 1
    events.extend(interpreter.finish())
    return events


class InterpreterTests(unittest.TestCase):
    def test_sample_redefinition(self):
        data = (FIXTURES / 'sample.zpl').read_bytes()
        events = interpret(data, [len(data)])
        self.assertEqual([e['kind'] for e in events], ['format', 'label', 'format', 'label'])
        self.assertEqual(events[0]['payload'], {'name': 'R:BOX.ZPL', 'version': 1})
        self.assertEqual(events[2]['payload'], {'name': 'R:BOX.ZPL', 'version': 2})
        first, second = events[1]['payload'], events[3]['payload']
        self.assertEqual(first['copies'], 2)
        self.assertEqual(second['copies'], 1)
        self.assertEqual([f['text'] for f in first['fields']], ['批次', 'LOT-7'])
        self.assertEqual([f['text'] for f in second['fields']], ['温控批次', 'LOT-7'])
        self.assertEqual(first['fields'][1]['n'], 1)
        self.assertEqual((first['fields'][1]['x'], first['fields'][1]['y']), (10, 50))
        self.assertEqual(first['formats'], [{'name': 'R:BOX.ZPL', 'version': 1}])
        self.assertEqual(second['formats'], [{'name': 'R:BOX.ZPL', 'version': 2}])

    def test_chunking_invariant(self):
        for name in ('sample.zpl', 'escape.zpl', 'graphic.zpl', 'recursive.zpl', 'unbound.zpl'):
            data = (FIXTURES / name).read_bytes()
            whole = interpret(data, [len(data)])
            self.assertEqual(whole, interpret(data, [1]), name)
            self.assertEqual(whole, interpret(data, [7, 3, 1]), name)

    def test_every_split_point(self):
        for name in ('escape.zpl', 'graphic.zpl'):
            data = (FIXTURES / name).read_bytes()
            whole = interpret(data, [len(data)])
            for cut in range(len(data) + 1):
                self.assertEqual(whole, interpret(data, [cut, len(data)]), f'{name}@{cut}')

    def test_escape_field(self):
        events = interpret((FIXTURES / 'escape.zpl').read_bytes(), [4096])
        [event] = [e for e in events if e['kind'] == 'label']
        self.assertEqual(event['payload']['fields'][0]['text'], '中^FS')
        self.assertEqual(event['payload']['copies'], 1)

    def test_graphic_bytes_are_opaque(self):
        events = interpret((FIXTURES / 'graphic.zpl').read_bytes(), [4096])
        labels = [e for e in events if e['kind'] == 'label']
        self.assertEqual(len(labels), 1)
        payload = labels[0]['payload']
        self.assertEqual([f['text'] for f in payload['fields']], ['VALID'])
        self.assertEqual(payload['graphics'][0]['total'], 6)

    def test_recursive_format_isolated(self):
        events = interpret((FIXTURES / 'recursive.zpl').read_bytes(), [4096])
        self.assertEqual([e['kind'] for e in events], ['format', 'isolated'])
        self.assertEqual(events[1]['payload']['reason'], 'recursive-format')

    def test_undefined_format_isolated(self):
        events = interpret((FIXTURES / 'unbound.zpl').read_bytes(), [4096])
        [event] = events
        self.assertEqual(event['kind'], 'isolated')
        self.assertEqual(event['payload']['reason'], 'undefined-format')

    def test_unbound_variable_isolated_and_no_leak(self):
        data = (b'^XA^DFR:F.ZPL^FS^FO1,2^FN1^FS^XZ'
                b'^XA^XFR:F.ZPL^FS^PQ1^XZ'
                b'^XA^FO3,4^FDOK^FS^XZ')
        events = interpret(data, [len(data)])
        self.assertEqual([e['kind'] for e in events], ['format', 'isolated', 'label'])
        self.assertEqual(events[1]['payload']['reason'], 'unbound-variable')
        self.assertEqual([f['text'] for f in events[2]['payload']['fields']], ['OK'])

    def test_binding_not_reused_between_labels(self):
        data = (b'^XA^DFR:F.ZPL^FS^FN1^FS^XZ'
                b'^XA^XFR:F.ZPL^FS^FN1^FDA^FS^XZ'
                b'^XA^XFR:F.ZPL^FS^XZ')
        events = interpret(data, [len(data)])
        self.assertEqual(events[1]['payload']['fields'][0]['text'], 'A')
        self.assertEqual(events[2]['kind'], 'isolated')
        self.assertEqual(events[2]['payload']['reason'], 'unbound-variable')

    def test_copies_validation(self):
        events = interpret(b'^XA^FDx^FS^PQ0^XZ', [4096])
        self.assertEqual(events[0]['payload']['reason'], 'invalid-pq')
        events = interpret(b'^XA^FDx^FS^PQ1001^XZ', [4096])
        self.assertEqual(events[0]['payload']['reason'], 'invalid-pq')
        events = interpret(b'^XA^FDx^FS^PQ1000^XZ', [4096])
        self.assertEqual(events[0]['payload']['copies'], 1000)

    def test_charset_switching(self):
        events = interpret(b'^XA^FD\x82^FS^XZ', [4096])
        self.assertEqual(events[0]['payload']['fields'][0]['text'], 'é')
        events = interpret('^XA^CI28^FD中^FS^XZ'.encode(), [4096])
        self.assertEqual(events[0]['payload']['fields'][0]['text'], '中')
        events = interpret(b'^XA^CI28^FD\xff^FS^XZ', [4096])
        self.assertEqual(events[0]['payload']['reason'], 'decode-error')

    def test_hex_introducer_change_and_reset(self):
        events = interpret(b'^XA^FH+^FD+41+42^FS^FD_41^FS^XZ', [4096])
        self.assertEqual([f['text'] for f in events[0]['payload']['fields']], ['AB', 'A'])

    def test_bad_hex_escape_isolated(self):
        events = interpret(b'^XA^FD_ZZabc^FS^XZ^XA^FDok^FS^XZ', [4096])
        self.assertEqual(events[0]['payload']['reason'], 'bad-hex-escape')
        self.assertEqual(events[1]['payload']['fields'][0]['text'], 'ok')

    def test_field_runs_to_unescaped_fs_only(self):
        events = interpret(b'^XA^FDa^XZ^FS^XZ', [4096])
        self.assertEqual(events[0]['payload']['fields'][0]['text'], 'a^XZ')

    def test_unterminated_label_at_eof(self):
        events = interpret(b'^XA^FDabc^FS', [4096])
        self.assertEqual(events[-1]['payload']['reason'], 'unterminated-label')

    def test_nested_xa_isolated(self):
        events = interpret(b'^XA^FO1,2^XA^FDb^FS^XZ', [4096])
        self.assertEqual(events[0]['payload']['reason'], 'nested-xa')
        self.assertEqual(events[1]['payload']['fields'][0]['text'], 'b')

    def test_duplicate_numbers_keep_order(self):
        data = (b'^XA^DFR:F.ZPL^FS^FN2^FS^FN1^FS^FN2^FS^XZ'
                b'^XA^XFR:F.ZPL^FS^FN1^FDone^FS^FN2^FDtwo^FS^XZ')
        events = interpret(data, [len(data)])
        fields = events[1]['payload']['fields']
        self.assertEqual([(f['n'], f['text']) for f in fields], [(2, 'two'), (1, 'one'), (2, 'two')])

    def test_barcode_payload(self):
        events = interpret(b'^XA^FO5,6^BC^FD12345^FS^XZ', [4096])
        field = events[0]['payload']['fields'][0]
        self.assertEqual(field['barcode'], 'BC')
        self.assertEqual(field['text'], '12345')

    def test_unsupported_command_noted(self):
        events = interpret(b'^XA^LT12^FDx^FS^XZ', [4096])
        notes = events[0]['payload']['notes']
        self.assertEqual(notes[0]['reason'], 'unsupported-command')
        self.assertEqual(notes[0]['detail'], '^LT')

    def test_depth_limit(self):
        parts = []
        for index in range(20):
            parts.append(f'^XA^DFR:F{index}.ZPL^FS^XFR:F{index + 1}.ZPL^FS^XZ'.encode())
        parts.append(b'^XA^DFR:F20.ZPL^FS^FDend^FS^XZ')
        parts.append(b'^XA^XFR:F0.ZPL^FS^XZ')
        events = interpret(b''.join(parts), [10 ** 6])
        self.assertEqual(events[-1]['payload']['reason'], 'depth-exceeded')

    def test_offsets_traceable(self):
        data = b'^XA^FO1,2^FDAB^FS^XZ'
        events = interpret(data, [4096])
        self.assertEqual((events[0]['start'], events[0]['end']), (0, len(data)))
        self.assertEqual(events[0]['payload']['fields'][0]['offset'], data.index(b'^FD'))


class ExtractStoreTests(unittest.TestCase):
    def setUp(self):
        self.server = FixtureServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_address[1])
        self.directory = tempfile.TemporaryDirectory()
        self.store = ExtractStore(Path(self.directory.name) / 'extract.sqlite3',
                                  Path(self.directory.name) / 'objects')

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()

    def test_resume_and_replay_do_not_double_count(self):
        url = self.base + '/stream'
        first = run_extract(self.store, url, chunk=11, max_chunks=3)
        self.assertFalse(first['eof'])
        second = run_extract(self.store, url, chunk=11)
        self.assertTrue(second['eof'])
        labels = self.store.events(kind='label', stream_id='print-gateway-sample')
        self.assertEqual([row['copies'] for row in labels], [2, 1])
        again = run_extract(self.store, url, chunk=11)
        self.assertEqual(again['note'], '作业已完成，无新增字节')
        labels = self.store.events(kind='label')
        self.assertEqual(len(labels), 2)
        self.assertEqual(sum(row['copies'] for row in labels), 3)

    def test_isolated_recorded_per_job(self):
        run_extract(self.store, self.base + '/stream?name=recursive', chunk=7)
        run_extract(self.store, self.base + '/stream?name=unbound', chunk=7)
        recursive = self.store.events(kind='isolated', stream_id='print-gateway-recursive')
        self.assertEqual(recursive[0]['reason'], 'recursive-format')
        unbound = self.store.events(kind='isolated', stream_id='print-gateway-unbound')
        self.assertEqual(unbound[0]['reason'], 'undefined-format')

    def test_reopen_continues_from_confirmed(self):
        url = self.base + '/stream?name=escape'
        run_extract(self.store, url, chunk=5, max_chunks=2)
        self.store.close()
        self.store = ExtractStore(Path(self.directory.name) / 'extract.sqlite3',
                                  Path(self.directory.name) / 'objects')
        summary = run_extract(self.store, url, chunk=5)
        self.assertTrue(summary['eof'])
        labels = self.store.events(kind='label', stream_id='print-gateway-escape')
        self.assertEqual(labels[0]['fields'][0]['text'], '中^FS')


class CliTests(unittest.TestCase):
    def test_extract_labels_isolated_cli(self):
        server = FixtureServer()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = 'http://127.0.0.1:' + str(server.server_address[1])
            with tempfile.TemporaryDirectory() as directory:
                db = str(Path(directory) / 'c.sqlite3')
                objects = str(Path(directory) / 'objects')

                def run(*args):
                    result = subprocess.run([sys.executable, '-m', 'labelstream', *args],
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    return json.loads(result.stdout)

                summary = run('extract', '--url', base + '/stream?name=sample',
                              '--db', db, '--objects', objects, '--chunk', '13')
                self.assertEqual(summary['labels'], 2)
                self.assertEqual(summary['formats'], 2)
                labels = run('labels', '--job', 'print-gateway-sample', '--db', db, '--objects', objects)
                self.assertEqual([row['copies'] for row in labels], [2, 1])
                self.assertEqual(labels[0]['fields'][0]['text'], '批次')
                self.assertEqual(labels[1]['fields'][0]['text'], '温控批次')
                run('extract', '--url', base + '/stream?name=recursive', '--db', db, '--objects', objects)
                isolated = run('isolated', '--job', 'print-gateway-recursive', '--db', db, '--objects', objects)
                self.assertEqual(isolated[0]['reason'], 'recursive-format')
                jobs = run('jobs', '--db', db, '--objects', objects)
                self.assertEqual(len(jobs), 2)
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()


if __name__ == '__main__':
    unittest.main()
