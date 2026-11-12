import base64
import json
import threading
import unittest
import urllib.parse
import urllib.request

from labelstream.source_server import FixtureServer
from labelstream.transport import fetch


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.server = FixtureServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()

    def get(self, path):
        return fetch(self.base + path)

    def phase(self, value):
        self.assertEqual(self.get('/control/phase?value=' + value)[0], 200)

    def test_small_chunks_reconstruct_original_stream(self):
        first = json.loads(self.get('/stream?offset=0&limit=5')[2])
        second = json.loads(self.get('/stream?offset=5&limit=5')[2])
        self.assertEqual(first['end'], second['start'])
        self.assertEqual(base64.b64decode(first['base64']) + base64.b64decode(second['base64']), self.get('/job')[2][:10])
        self.assertFalse(first['eof'])

    def test_replay_is_byte_identical(self):
        self.assertEqual(self.get('/stream?offset=7&limit=11')[2], self.get('/stream?offset=7&limit=11')[2])
        self.assertEqual(self.get('/stream?offset=-1')[0], 400)
