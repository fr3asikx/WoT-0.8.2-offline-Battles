import struct
MAGIC = 0x62A14E45

def _strings(d):
    off = 5
    out = []
    while off < len(d):
        e = d.find(b'\0', off)
        if e < 0: break
        s = d[off:e]; off = e + 1
        if not s: break
        out.append(s.decode('utf-8', 'replace'))
    return out, off

def _val(blob, t):
    if t == 0: return None
    if t == 1: return blob.decode('utf-8', 'replace')
    if t == 2:
        if not blob: return 0
        return int.from_bytes(blob, 'little', signed=True)
    if t == 3:
        n = len(blob) // 4
        f = struct.unpack('<%df' % n, blob[:n*4])
        return f[0] if n == 1 else list(f)
    if t == 4: return bool(blob and blob[0])
    return blob

def _section(d, off, strings):
    n, = struct.unpack_from('<H', d, off); off += 2
    sd, = struct.unpack_from('<I', d, off); off += 4
    self_len = sd & 0x0FFFFFFF
    kids = []
    for _ in range(n):
        k, = struct.unpack_from('<H', d, off); off += 2
        dd, = struct.unpack_from('<I', d, off); off += 4
        kids.append((k, dd & 0x0FFFFFFF, (dd >> 28) & 0xF))
    base = off
    own = d[base:base+self_len]
    res = {}
    prev = self_len
    cur = base + self_len
    for k, end, t in kids:
        ln = end - prev
        blob = d[cur:cur+ln]
        name = strings[k] if k < len(strings) else '?%d' % k
        res.setdefault(name, []).append(_section(blob, 0, strings) if t == 0 else _val(blob, t))
        prev = end; cur += ln
    if not res:
        return _val(own, 1) if own else None
    return res

def parse(d):
    if len(d) < 5 or struct.unpack_from('<I', d, 0)[0] != MAGIC:
        return None
    strings, off = _strings(d)
    return _section(d, off, strings)

def get(sec, path):
    cur = sec
    for p in path.split('/'):
        if not isinstance(cur, dict) or p not in cur: return None
        cur = cur[p][0]
    return cur
