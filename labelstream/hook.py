import base64
import json
import urllib.parse


def handle(server, path, headers):
    parsed = urllib.parse.urlsplit(path)
    if parsed.path != '/stream':
        return None
    query = urllib.parse.parse_qs(parsed.query)
    try:
        start = int(query.get('offset', ['0'])[0])
        limit = int(query.get('limit', ['17'])[0])
        body = (server.fixtures / 'sample.zpl').read_bytes()
        if start < 0 or start > len(body) or not 1 <= limit <= 4096:
            raise ValueError()
    except ValueError:
        return 400, 'application/json', b'{"error":"range"}'
    stop = min(start + limit, len(body))
    result = {'streamId': 'print-gateway-1', 'start': start, 'end': stop,
              'base64': base64.b64encode(body[start:stop]).decode(), 'eof': stop == len(body)}
    return 200, 'application/json', json.dumps(result).encode()
