"""Reader for the .NET BinaryWriter column tables PTCGL ships in config-cache.

Layout, all little-endian:

    u8      unused flag
    str7    table name
    i32     column count
    (str7 name, str7 clr_type) * column count
    i32     row count
    row * row count

Every cell is prefixed with a null byte: 1 means the value is absent and
nothing follows, 0 means a value of the column's CLR type follows. Strings are
7-bit-length-prefixed UTF-8, the rest are fixed width.
"""

import base64
import json
import struct

FIXED = {
    "System.Boolean": ("<?", 1),
    "System.Byte": ("<B", 1),
    "System.SByte": ("<b", 1),
    "System.Int16": ("<h", 2),
    "System.UInt16": ("<H", 2),
    "System.Int32": ("<i", 4),
    "System.UInt32": ("<I", 4),
    "System.Single": ("<f", 4),
    "System.Int64": ("<q", 8),
    "System.UInt64": ("<Q", 8),
    "System.Double": ("<d", 8),
}


class _Cursor:
    def __init__(self, buf):
        self.buf = buf
        self.pos = 0

    def u8(self):
        v = self.buf[self.pos]
        self.pos += 1
        return v

    def i32(self):
        v = struct.unpack_from("<i", self.buf, self.pos)[0]
        self.pos += 4
        return v

    def string(self):
        n = shift = 0
        while True:
            c = self.buf[self.pos]
            self.pos += 1
            n |= (c & 0x7F) << shift
            if not c & 0x80:
                break
            shift += 7
        v = self.buf[self.pos:self.pos + n].decode("utf-8")
        self.pos += n
        return v


class Table:
    def __init__(self, name, columns, rows):
        self.name = name
        self.columns = columns
        self.rows = rows

    def __len__(self):
        return len(self.rows)


def parse(buf):
    """Parse raw table bytes. Raises ValueError if the buffer is not consumed
    exactly, which is the signal that the on-disk format has changed."""
    c = _Cursor(buf)
    c.u8()
    name = c.string()
    columns = [(c.string(), c.string()) for _ in range(c.i32())]

    rows = []
    for _ in range(c.i32()):
        row = {}
        for col, clr in columns:
            if c.u8() == 1:
                row[col] = None
            elif clr in FIXED:
                fmt, width = FIXED[clr]
                row[col] = struct.unpack_from(fmt, c.buf, c.pos)[0]
                c.pos += width
            else:
                row[col] = c.string()
        rows.append(row)

    if c.pos != len(buf):
        raise ValueError(
            f"table {name!r}: consumed {c.pos} of {len(buf)} bytes - "
            "the client's table format has probably changed"
        )
    return Table(name, columns, rows)


def load(path):
    """Parse a config-cache document that wraps a binary table."""
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    for key, entry in doc.get("keys", {}).items():
        if entry.get("isBinary"):
            return parse(base64.b64decode(entry["contentBinary"]))
    raise ValueError(f"{path}: no binary table found")
