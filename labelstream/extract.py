"""把确认接收的 ZPL 字节解释成结构化标签记录。

词法与语义边界见 docs/source-contract.md。本模块不驱动打印机、不渲染条码图像，
也不猜测字段的业务含义；字段顺序与重复编号按原文保留，可被任意分块续收。
"""

import base64
import hashlib
import json
import os
import sqlite3
import tempfile
import urllib.parse
from pathlib import Path

from .transport import fetch

INTRODUCERS = (0x5E, 0x7E)  # ^ ~
DEFAULT_HEX_INTRODUCER = 0x5F  # _
CHARSETS = {b'28': 'utf-8', b'0': 'cp437'}
MAX_DEPTH = 16
MAX_FIELDS = 10000
MAX_COPIES = 1000
NO_PARAM_CODES = {'XA', 'XZ', 'FS'}
HEX_DIGITS = b'0123456789abcdefABCDEF'


class Isolated(Exception):
    """单张标签被隔离：原因写入记录，字段不进入任何标签，后续标签独立解释。"""

    def __init__(self, reason, detail):
        super().__init__(f'{reason}: {detail}')
        self.reason = reason
        self.detail = detail


def _parse_int(params):
    if not params:
        return None
    digits = params[1:] if params.startswith(b'-') else params
    if not digits.isdigit():
        return None
    return int(params)


def _parse_position(params):
    parts = params.split(b',')
    if len(parts) != 2:
        return None
    x, y = _parse_int(parts[0]), _parse_int(parts[1])
    if x is None or y is None:
        return None
    return (x, y)


class Lexer:
    """增量词法器。

    任意分块下产出相同的令牌序列；进程可以停在指令、转义或图形块中间，
    续收后从缓冲继续。图形数据按声明长度不透明跳过，字段到未转义 ^FS 为止。
    """

    def __init__(self):
        self.buffer = bytearray()
        self.base = 0  # buffer[0] 在作业流中的字节位置
        self.pending_introducer = DEFAULT_HEX_INTRODUCER

    def feed(self, data):
        self.buffer += data

    def scan(self, eof=False):
        tokens = []
        pos = 0
        while True:
            token, nxt = self._scan_one(pos, eof)
            if token is not None:
                tokens.append(token)
                self._apply(token)
            if nxt <= pos:
                break
            pos = nxt
        if pos:
            del self.buffer[:pos]
            self.base += pos
        return tokens

    def _apply(self, token):
        if token[0] == 'cmd' and token[3] == 'FH' and token[4]:
            self.pending_introducer = token[4][0]
        elif token[0] == 'field':
            self.pending_introducer = DEFAULT_HEX_INTRODUCER

    def _scan_one(self, pos, eof):
        buf = self.buffer
        i = pos
        while i < len(buf) and buf[i] not in INTRODUCERS:
            i += 1
        if i >= len(buf):
            return None, i
        if len(buf) - i < 3:
            if eof:
                return ('corrupt', self.base + i, 'truncated-command',
                        bytes(buf[i:]).hex()), len(buf)
            return None, i
        prefix, b1, b2 = buf[i], buf[i + 1], buf[i + 2]
        if not (65 <= b1 <= 90 and (65 <= b2 <= 90 or 48 <= b2 <= 57)):
            return ('corrupt', self.base + i, 'bad-command',
                    bytes(buf[i:i + 3]).hex()), i + 1
        code = chr(b1) + chr(b2)
        offset = self.base + i
        if code in NO_PARAM_CODES:
            return ('cmd', offset, chr(prefix), code, b''), i + 3
        if code == 'FD':
            return self._scan_field(i, offset, eof)
        if code == 'GF':
            return self._scan_graphic(i, offset, eof)
        j = i + 3
        while j < len(buf) and buf[j] not in INTRODUCERS:
            j += 1
        if j >= len(buf) and not eof:
            return None, i
        return ('cmd', offset, chr(prefix), code, bytes(buf[i + 3:j])), j

    def _scan_field(self, i, offset, eof):
        buf = self.buffer
        introducer = self.pending_introducer
        data = bytearray()
        j = i + 3
        while True:
            if j >= len(buf):
                if eof:
                    return ('corrupt', offset, 'truncated-field', ''), len(buf)
                return None, i
            b = buf[j]
            if b == introducer:
                if j + 3 > len(buf):
                    if eof:
                        return ('corrupt', offset, 'truncated-escape', ''), len(buf)
                    return None, i
                pair = bytes(buf[j + 1:j + 3])
                if pair[0] not in HEX_DIGITS or pair[1] not in HEX_DIGITS:
                    return ('corrupt', offset, 'bad-hex-escape',
                            pair.decode('ascii', 'replace')), j + 3
                data.append(int(pair, 16))
                j += 3
                continue
            if b == 0x5E:  # ^
                if j + 3 > len(buf):
                    if eof:
                        return ('corrupt', offset, 'truncated-field', ''), len(buf)
                    return None, i
                if buf[j + 1:j + 3] == b'FS':
                    return ('field', offset, bytes(data)), j + 3
            data.append(b)
            j += 1

    def _scan_graphic(self, i, offset, eof):
        buf = self.buffer
        k = i + 3
        commas = 0
        while k < len(buf) and commas < 4:
            if buf[k] == 0x2C:
                commas += 1
            k += 1
        if commas < 4:
            if eof:
                return ('corrupt', offset, 'truncated-graphic', 'header'), len(buf)
            return None, i
        parts = bytes(buf[i + 3:k]).split(b',')
        if parts[0] != b'B':
            return ('corrupt', offset, 'unsupported-gf-compression',
                    parts[0].decode('ascii', 'replace')), k
        numbers = parts[1:4]
        if any(not n.isdigit() for n in numbers):
            return ('corrupt', offset, 'bad-gf-header',
                    bytes(buf[i + 3:k]).decode('ascii', 'replace')), k
        total, used, _rows = (int(n) for n in numbers)
        if len(buf) - k < total:
            if eof:
                return ('corrupt', offset, 'truncated-graphic', 'data'), len(buf)
            return None, i
        data_end = k + total
        if len(buf) - data_end < 3:
            if eof:
                return ('corrupt', offset, 'truncated-graphic', 'terminator'), len(buf)
            return None, i
        if bytes(buf[data_end:data_end + 3]) != b'^FS':
            return ('corrupt', offset, 'graphic-missing-fs',
                    f'offset {self.base + data_end}'), data_end
        return ('graphic', offset, total, used, self.base + k), data_end + 3


class Interpreter:
    """把令牌流解释成事件：label（实际打印）、format（格式版本）、isolated（隔离）。"""

    def __init__(self):
        self.lexer = Lexer()
        self.in_label = False
        self.label_tokens = []
        self.label_start = 0
        self.label_taint = None
        self.formats = {}  # name -> (version, tokens)
        self.charset = 'cp437'
        self.seq = 0

    def feed(self, data):
        self.lexer.feed(data)
        return self._drain(eof=False)

    def finish(self):
        events = self._drain(eof=True)
        if self.in_label:
            reason, detail = self.label_taint or ('unterminated-label', '来源结束时标签未闭合')
            events.append(self._event('isolated', self.label_start, None,
                                      {'reason': reason, 'detail': detail}))
            self.in_label = False
        return events

    def _drain(self, eof):
        events = []
        for token in self.lexer.scan(eof=eof):
            self._handle(token, events)
        return events

    def _event(self, kind, start, end, payload):
        event = {'seq': self.seq, 'kind': kind, 'start': start, 'end': end, 'payload': payload}
        self.seq += 1
        return event

    def _handle(self, token, events):
        kind = token[0]
        if kind == 'corrupt':
            _, offset, reason, detail = token
            if self.in_label:
                if self.label_taint is None:
                    self.label_taint = (reason, detail)
            else:
                events.append(self._event('isolated', offset, None,
                                          {'reason': reason, 'detail': detail}))
            return
        if kind == 'cmd' and token[3] == 'XA':
            if self.in_label:
                reason, detail = self.label_taint or ('nested-xa', '标签未闭合又出现 ^XA')
                events.append(self._event('isolated', self.label_start, token[1],
                                          {'reason': reason, 'detail': detail}))
            self.in_label = True
            self.label_tokens = []
            self.label_start = token[1]
            self.label_taint = None
            return
        if kind == 'cmd' and token[3] == 'XZ':
            if not self.in_label:
                events.append(self._event('isolated', token[1], token[1] + 3,
                                          {'reason': 'xz-outside-label',
                                           'detail': '^XZ 没有配对的 ^XA'}))
                return
            events.extend(self._finalize(token[1] + 3))
            self.in_label = False
            self.label_tokens = []
            return
        if self.in_label:
            self.label_tokens.append(token)
            return
        if kind == 'cmd' and token[3] == 'CI':
            charset = CHARSETS.get(token[4])
            if charset:
                self.charset = charset
            return
        if kind == 'cmd' and token[3] in ('FS', 'FH'):
            return
        if kind == 'cmd':
            detail = token[2] + token[3]
        elif kind == 'field':
            detail = 'FD 字段在标签之外'
        else:
            detail = 'GF 图形在标签之外'
        events.append(self._event('isolated', token[1], None,
                                  {'reason': 'token-outside-label', 'detail': detail}))

    def _finalize(self, end):
        start = self.label_start
        if self.label_taint is not None:
            reason, detail = self.label_taint
            return [self._event('isolated', start, end, {'reason': reason, 'detail': detail})]
        definitions = [t for t in self.label_tokens if t[0] == 'cmd' and t[3] == 'DF']
        if len(definitions) > 1:
            return [self._event('isolated', start, end,
                                {'reason': 'multiple-df', 'detail': '一张标签内出现多个 ^DF'})]
        if definitions:
            params = definitions[0][4]
            try:
                name = params.decode('ascii')
            except UnicodeDecodeError:
                name = ''
            if not name:
                return [self._event('isolated', start, end,
                                    {'reason': 'bad-format-name',
                                     'detail': params.hex() or '空格式名'})]
            body = [t for t in self.label_tokens if t is not definitions[0]]
            version = self.formats[name][0] + 1 if name in self.formats else 1
            self.formats[name] = (version, body)
            return [self._event('format', start, end, {'name': name, 'version': version})]
        try:
            payload = self._expand()
        except Isolated as exc:
            return [self._event('isolated', start, end,
                                {'reason': exc.reason, 'detail': exc.detail})]
        return [self._event('label', start, end, payload)]

    def _expand(self):
        tokens = self.label_tokens
        bindings = {}
        skip = set()
        notes = []
        for i, token in enumerate(tokens):
            if token[0] == 'cmd' and token[3] == 'FN':
                number = _parse_int(token[4])
                if number is None:
                    raise Isolated('invalid-fn', token[4].decode('ascii', 'replace'))
                skip.add(i)
                if i + 1 < len(tokens) and tokens[i + 1][0] == 'field':
                    bindings[number] = (tokens[i + 1][2], tokens[i + 1][1])
                    skip.add(i + 1)
                else:
                    notes.append({'offset': token[1], 'reason': 'fn-without-field',
                                  'detail': f'FN{number} 后没有 ^FD'})
        out = {'copies': 1, 'fields': [], 'graphics': [], 'formats': [], 'notes': notes}
        self._run(tokens, bindings, 0, (), out, skip, {'fo': None, 'barcode': None})
        if bindings and not out['formats']:
            out['notes'].append({'offset': None, 'reason': 'unused-binding',
                                 'detail': '标签未调用格式，^FN 绑定未使用'})
        return out

    def _run(self, tokens, bindings, depth, stack, out, skip, pending):
        i = 0
        while i < len(tokens):
            token = tokens[i]
            i += 1
            if skip and (i - 1) in skip:
                continue
            kind = token[0]
            if kind == 'field':
                self._emit_field(out, pending, None, token[2], token[1])
                continue
            if kind == 'graphic':
                _, offset, total, used, data_offset = token
                out['graphics'].append({'offset': data_offset, 'total': total})
                if total != used:
                    out['notes'].append({'offset': offset, 'reason': 'gf-total-used-mismatch',
                                         'detail': f'total={total} used={used}'})
                continue
            _, offset, prefix, code, params = token
            if code == 'FO':
                position = _parse_position(params)
                if position is None:
                    out['notes'].append({'offset': offset, 'reason': 'invalid-fo',
                                         'detail': params.decode('ascii', 'replace')})
                pending['fo'] = position
            elif code in ('BC', 'B3'):
                pending['barcode'] = code
            elif code == 'FN':
                number = _parse_int(params)
                if number is None:
                    raise Isolated('invalid-fn', params.decode('ascii', 'replace'))
                default = None
                if i < len(tokens) and tokens[i][0] == 'field':
                    default = (tokens[i][2], tokens[i][1])
                    i += 1
                if number in bindings:
                    value, value_offset = bindings[number]
                elif default is not None:
                    value, value_offset = default
                else:
                    raise Isolated('unbound-variable', f'FN{number} 没有绑定值')
                self._emit_field(out, pending, number, value, value_offset)
            elif code == 'PQ':
                copies = _parse_int(params)
                if copies is None or not 1 <= copies <= MAX_COPIES:
                    raise Isolated('invalid-pq', params.decode('ascii', 'replace'))
                out['copies'] = copies
            elif code == 'CI':
                charset = CHARSETS.get(params)
                if charset is None:
                    out['notes'].append({'offset': offset, 'reason': 'unsupported-ci',
                                         'detail': params.decode('ascii', 'replace')})
                else:
                    self.charset = charset
            elif code == 'FH':
                if len(params) != 1:
                    out['notes'].append({'offset': offset, 'reason': 'invalid-fh',
                                         'detail': params.decode('ascii', 'replace')})
            elif code == 'FS':
                pass
            elif code == 'XF':
                try:
                    name = params.decode('ascii')
                except UnicodeDecodeError:
                    raise Isolated('bad-format-name', params.hex())
                entry = self.formats.get(name)
                if entry is None:
                    raise Isolated('undefined-format', name)
                version, body = entry
                out['formats'].append({'name': name, 'version': version})
                if name in stack:
                    raise Isolated('recursive-format', ' -> '.join(stack + (name,)))
                if depth + 1 > MAX_DEPTH:
                    raise Isolated('depth-exceeded', f'展开深度超过 {MAX_DEPTH}')
                self._run(body, bindings, depth + 1, stack + (name,), out, None, pending)
            else:
                out['notes'].append({'offset': offset, 'reason': 'unsupported-command',
                                     'detail': prefix + code})

    def _emit_field(self, out, pending, number, data, offset):
        try:
            text = data.decode(self.charset)
        except UnicodeDecodeError:
            raise Isolated('decode-error', f'offset {offset} 无法按 {self.charset} 解码')
        position = pending['fo'] or (None, None)
        out['fields'].append({
            'n': number, 'x': position[0], 'y': position[1],
            'text': text, 'barcode': pending['barcode'], 'offset': offset,
        })
        pending['fo'] = None
        pending['barcode'] = None
        if len(out['fields']) > MAX_FIELDS:
            raise Isolated('too-many-fields', f'字段数超过 {MAX_FIELDS}')


class ExtractStore:
    """按作业持久化确认字节与解释事件；重放同一区间不会产生重复事件。"""

    def __init__(self, database, objects):
        self.objects = Path(objects)
        self.objects.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(database)
        self.connection.execute('pragma foreign_keys=on')
        self.connection.executescript("""
            create table if not exists jobs (
                id integer primary key, url text not null, stream_id text not null,
                confirmed integer not null default 0, eof integer not null default 0,
                unique(url, stream_id)
            );
            create table if not exists chunks (
                job_id integer not null references jobs(id),
                start integer not null, end integer not null, digest text not null,
                primary key (job_id, start)
            );
            create table if not exists events (
                job_id integer not null references jobs(id),
                seq integer not null, kind text not null,
                start_offset integer, end_offset integer, payload text not null,
                primary key (job_id, seq)
            );
        """)

    def job_for(self, url):
        row = self.connection.execute(
            'select id, stream_id, confirmed, eof from jobs where url=? order by id desc limit 1',
            (url,)).fetchone()
        if row is None:
            return None
        return {'id': row[0], 'stream_id': row[1], 'confirmed': row[2], 'eof': bool(row[3])}

    def ensure_job(self, url, stream_id):
        with self.connection:
            self.connection.execute('insert or ignore into jobs(url, stream_id) values(?, ?)',
                                    (url, stream_id))
        return self.connection.execute(
            'select id from jobs where url=? and stream_id=?', (url, stream_id)).fetchone()[0]

    def _write_object(self, data):
        digest = hashlib.sha256(data).hexdigest()
        destination = self.objects / digest
        if destination.exists():
            if destination.read_bytes() != data:
                raise ValueError('块对象校验失败')
            return digest
        descriptor, temporary = tempfile.mkstemp(prefix='.chunk-', dir=self.objects)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return digest

    def add_events(self, job_id, events):
        with self.connection:
            for event in events:
                self.connection.execute(
                    'insert or ignore into events(job_id, seq, kind, start_offset, end_offset, payload)'
                    ' values(?, ?, ?, ?, ?, ?)',
                    (job_id, event['seq'], event['kind'], event['start'], event['end'],
                     json.dumps(event['payload'], ensure_ascii=False)))

    def append(self, job_id, start, data, events, eof):
        with self.connection:
            confirmed = self.connection.execute(
                'select confirmed from jobs where id=?', (job_id,)).fetchone()[0]
            if confirmed != start:
                raise ValueError('确认水位与来源偏移不一致')
            if data:
                digest = self._write_object(data)
                self.connection.execute(
                    'insert into chunks(job_id, start, end, digest) values(?, ?, ?, ?)',
                    (job_id, start, start + len(data), digest))
            for event in events:
                self.connection.execute(
                    'insert or ignore into events(job_id, seq, kind, start_offset, end_offset, payload)'
                    ' values(?, ?, ?, ?, ?, ?)',
                    (job_id, event['seq'], event['kind'], event['start'], event['end'],
                     json.dumps(event['payload'], ensure_ascii=False)))
            self.connection.execute('update jobs set confirmed=?, eof=? where id=?',
                                    (start + len(data), 1 if eof else 0, job_id))

    def read_chunks(self, job_id):
        rows = self.connection.execute(
            'select start, end, digest from chunks where job_id=? order by start', (job_id,))
        expected = 0
        result = []
        for start, end, digest in rows:
            if start != expected:
                raise ValueError('已确认字节不连续')
            body = (self.objects / digest).read_bytes()
            if hashlib.sha256(body).hexdigest() != digest:
                raise ValueError('块对象摘要不匹配')
            result.append(body)
            expected = end
        return result

    def jobs(self):
        rows = []
        for job_id, stream_id, url, confirmed, eof in self.connection.execute(
                'select id, stream_id, url, confirmed, eof from jobs order by id'):
            counts = dict(self.connection.execute(
                'select kind, count(*) from events where job_id=? group by kind', (job_id,)))
            rows.append({'id': job_id, 'stream_id': stream_id, 'url': url,
                         'confirmed': confirmed, 'eof': bool(eof),
                         'labels': counts.get('label', 0),
                         'isolated': counts.get('isolated', 0),
                         'formats': counts.get('format', 0)})
        return rows

    def events(self, kind=None, stream_id=None):
        sql = ('select j.stream_id, e.seq, e.kind, e.start_offset, e.end_offset, e.payload'
               ' from events e join jobs j on j.id = e.job_id')
        clauses, args = [], []
        if kind:
            clauses.append('e.kind=?')
            args.append(kind)
        if stream_id:
            clauses.append('j.stream_id=?')
            args.append(stream_id)
        if clauses:
            sql += ' where ' + ' and '.join(clauses)
        sql += ' order by j.id, e.seq'
        rows = []
        for stream, seq, event_kind, start, end, payload in self.connection.execute(sql, args):
            row = {'job': stream, 'seq': seq, 'kind': event_kind, 'range': [start, end]}
            row.update(json.loads(payload))
            rows.append(row)
        return rows

    def summary(self, job_id):
        stream_id, confirmed, eof = self.connection.execute(
            'select stream_id, confirmed, eof from jobs where id=?', (job_id,)).fetchone()
        counts = dict(self.connection.execute(
            'select kind, count(*) from events where job_id=? group by kind', (job_id,)))
        return {'job': stream_id, 'received': confirmed, 'eof': bool(eof),
                'labels': counts.get('label', 0), 'isolated': counts.get('isolated', 0),
                'formats': counts.get('format', 0)}

    def close(self):
        self.connection.close()


def run_extract(store, url, chunk=4096, max_chunks=None):
    """持续接收一个 /stream 作业直到 eof；可从已确认水位续收，重放不重复计份。"""
    if not 1 <= chunk <= 4096:
        raise ValueError('分块大小需在 1..4096 之间')
    interpreter = Interpreter()
    job = store.job_for(url)
    if job is not None and job['eof']:
        result = store.summary(job['id'])
        result['note'] = '作业已完成，无新增字节'
        return result
    job_id, stream_id, confirmed = None, None, 0
    if job is not None:
        job_id, stream_id, confirmed = job['id'], job['stream_id'], job['confirmed']
        for data in store.read_chunks(job_id):
            store.add_events(job_id, interpreter.feed(data))
    fetched = 0
    while True:
        separator = '&' if urllib.parse.urlsplit(url).query else '?'
        status, _, body = fetch(f'{url}{separator}offset={confirmed}&limit={chunk}')
        if status != 200:
            raise ValueError(f'来源返回状态 {status}')
        message = json.loads(body)
        if message.get('start') != confirmed:
            raise ValueError('来源偏移与确认水位不一致')
        if stream_id is None:
            stream_id = message['streamId']
            job_id = store.ensure_job(url, stream_id)
        elif message.get('streamId') != stream_id:
            raise ValueError('同一地址的 streamId 发生变化')
        data = base64.b64decode(message['base64'])
        if len(data) != message['end'] - confirmed:
            raise ValueError('块长度与 start/end 不一致')
        eof = bool(message.get('eof'))
        events = interpreter.feed(data)
        if eof:
            events += interpreter.finish()
        store.append(job_id, confirmed, data, events, eof)
        confirmed += len(data)
        fetched += 1
        if eof:
            break
        if not data:
            raise ValueError('来源未返回新字节也未标记结束')
        if max_chunks is not None and fetched >= max_chunks:
            break
    return store.summary(job_id)
