import unittest
from pathlib import Path
from labelstream.parser import inspect_document

class ParserTests(unittest.TestCase):
    def test_candidate_offsets(self):
        body = b'^XA^FO10,20^FDabc^FS^XZ'
        result = inspect_document(body)
        self.assertEqual([row['code'] for row in result['candidate_markers']], ['XA', 'FO', 'FD', 'FS', 'XZ'])
        self.assertEqual(result['candidate_markers'][1]['offset'], 3)

    def test_no_decode_of_binary(self):
        self.assertEqual(inspect_document(b'\xff\x00')['size'], 2)

    def test_empty(self):
        self.assertEqual(inspect_document(b'')['candidate_markers'], [])

    def test_complete_sample(self):
        result = inspect_document(Path('labelstream/fixtures/sample.zpl').read_bytes())
        self.assertEqual(result['starts'], 4)
        self.assertEqual(result['ends'], 4)
