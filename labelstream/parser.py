import re


def inspect_document(body):
    markers = []
    for match in re.finditer(rb'[\^~]([A-Z][A-Z0-9])', body):
        markers.append({'offset': match.start(), 'prefix': chr(body[match.start()]),
                        'code': match[1].decode('ascii')})
    return {'size': len(body), 'candidate_markers': markers,
            'starts': sum(item['code'] == 'XA' for item in markers),
            'ends': sum(item['code'] == 'XZ' for item in markers)}
