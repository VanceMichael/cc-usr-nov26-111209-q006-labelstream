import base64
import json
import re
import urllib.parse


def handle(server, path, headers):
    parsed = urllib.parse.urlsplit(path)
    if parsed.path != '/stream':
        return None
    query = urllib.parse.parse_qs(parsed.query)
    name = query.get('name', ['sample'])[0]
    if not re.fullmatch(r'[A-Za-z0-9_-]+', name):
        return 400, 'application/json', b'{"error":"name"}'
    fixture = server.fixtures / (name + '.zpl')
    if not fixture.is_file():
        return 404, 'application/json', b'{"error":"unknown stream"}'
    try:
        start = int(query.get('offset', ['0'])[0])
        limit = int(query.get('limit', ['17'])[0])
        body = fixture.read_bytes()
        if start < 0 or start > len(body) or not 1 <= limit <= 4096:
            raise ValueError()
    except ValueError:
        return 400, 'application/json', b'{"error":"range"}'
    stop = min(start + limit, len(body))
    result = {'streamId': 'print-gateway-' + name, 'start': start, 'end': stop,
              'base64': base64.b64encode(body[start:stop]).decode(), 'eof': stop == len(body)}
    return 200, 'application/json', json.dumps(result).encode()
