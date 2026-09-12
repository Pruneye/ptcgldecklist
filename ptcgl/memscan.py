"""Read-only inspection of the running client's memory.

The match protocol is FlatBuffers inside STOMP inside TLS, so decoding the wire
is expensive. But whatever the client *receives* it must also *hold*, and Mono
keeps managed strings as UTF-16. So questions about what the client knows can be
answered by looking at its heap rather than by decrypting anything.

One thing had to be learned the hard way: the client loads the **entire** card
database into memory at startup (~24.7k of 25.4k ids are resident while sitting
in the menu). So "is this card id present?" is worthless as a signal - every
decklist scores 100%. What distinguishes a real decklist is *locality*: the card
database is laid out grouped by expansion, whereas a deck is ~15-30 ids from
several different expansions packed into a small span of memory.

Windows only. Opens the process with PROCESS_QUERY_INFORMATION|PROCESS_VM_READ
and never writes to it.
"""

import ctypes
import ctypes.wintypes as wt
import re

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010

MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
PAGE_GUARD = 0x100
READABLE = {0x02, 0x04, 0x20, 0x40}  # READONLY, READWRITE, EXECUTE_READ, EXECUTE_READWRITE

MAX_REGION = 64 * 1024 * 1024


class MEMORY_BASIC_INFORMATION64(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_ulonglong),
        ("AllocationBase", ctypes.c_ulonglong),
        ("AllocationProtect", wt.DWORD),
        ("__alignment1", wt.DWORD),
        ("RegionSize", ctypes.c_ulonglong),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
        ("__alignment2", wt.DWORD),
    ]


k32.OpenProcess.restype = wt.HANDLE
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.VirtualQueryEx.restype = ctypes.c_size_t
k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p,
                               ctypes.POINTER(MEMORY_BASIC_INFORMATION64), ctypes.c_size_t]
k32.ReadProcessMemory.restype = wt.BOOL
k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.CloseHandle.argtypes = [wt.HANDLE]

# Card ids appear in two forms:
#   deck-definition form:  sv6_130, me2-5_196, ec_24_ph   (client's own decks)
#   match/board form:      SV6_en_130, ME2-5_en_196       (live game state)
# The match form is the one that carries the opponent's deck.
CARD_UTF16 = re.compile(rb"(?:[a-z0-9-]\x00){2,8}_\x00(?:[0-9]\x00){1,3}(?:_\x00(?:[a-z]\x00){2})?")
MATCH_UTF16 = re.compile(
    rb"(?:[A-Za-z0-9-]\x00){2,8}_\x00e\x00n\x00_\x00(?:[0-9]\x00){1,3}(?:_\x00(?:[a-z]\x00){2})?"
)
MATCH_UTF8 = re.compile(rb"[A-Za-z0-9-]{2,8}_en_[0-9]{1,3}(?:_[a-z]{2})?")


def normalize_match_id(s):
    """`SV6_en_130` -> `sv6_130`, matching the card index's ids."""
    return s.replace("_en_", "_", 1).lower()


def open_process(pid):
    handle = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        raise OSError(ctypes.get_last_error(),
                      f"cannot open process {pid} - try running from an elevated shell")
    return handle


def iter_regions(handle):
    """Committed, readable, private regions - where the managed heap lives."""
    mbi = MEMORY_BASIC_INFORMATION64()
    addr = 0
    while addr < 0x7FFFFFFFFFFF:
        if not k32.VirtualQueryEx(handle, ctypes.c_void_p(addr), ctypes.byref(mbi),
                                  ctypes.sizeof(mbi)):
            break
        size = mbi.RegionSize
        if size == 0:
            break
        if (mbi.State == MEM_COMMIT and mbi.Type == MEM_PRIVATE
                and (mbi.Protect & 0xFF) in READABLE
                and not mbi.Protect & PAGE_GUARD
                and size <= MAX_REGION):
            yield mbi.BaseAddress, size
        addr = mbi.BaseAddress + size


def _read_all(handle, on_region, biggest_first=False):
    """Feed every readable private region to `on_region(base, data)`.

    If `on_region` returns a truthy value, scanning stops early - this is what
    turns a full 2 GB sweep into a fraction of it when we only need a first hit.
    With biggest_first, large heap regions (where the objects live) are read
    first, so early-exit finds the target sooner.
    """
    stats = {"regions": 0, "bytes": 0}
    read = ctypes.c_size_t(0)
    regions = list(iter_regions(handle))
    if biggest_first:
        regions.sort(key=lambda r: -r[1])
    for base, size in regions:
        buf = ctypes.create_string_buffer(size)
        if not k32.ReadProcessMemory(handle, ctypes.c_void_p(base), buf, size,
                                     ctypes.byref(read)):
            continue
        data = buf.raw[:read.value]
        stats["regions"] += 1
        stats["bytes"] += len(data)
        if on_region(base, data):
            break
    return stats


def card_hits(pid, valid_ids, match_form=False):
    """Every card-id occurrence as (address, card_id), in address order.

    With match_form=True, also detect the live-match `SET_en_num` form and
    normalize it, so the opponent's board cards are visible.
    """
    handle = open_process(pid)
    hits = []

    def on_region(base, data):
        for m in CARD_UTF16.finditer(data):
            cid = m.group().decode("utf-16-le")
            if cid in valid_ids:
                hits.append((base + m.start(), cid, "d"))
        if match_form:
            for m in MATCH_UTF16.finditer(data):
                cid = normalize_match_id(m.group().decode("utf-16-le"))
                if cid in valid_ids:
                    hits.append((base + m.start(), cid, "m"))
            for m in MATCH_UTF8.finditer(data):
                cid = normalize_match_id(m.group().decode("latin-1"))
                if cid in valid_ids:
                    hits.append((base + m.start(), cid, "m"))

    try:
        stats = _read_all(handle, on_region)
    finally:
        k32.CloseHandle(handle)
    hits.sort()
    return hits, stats


def cluster(hits, gap=4096, min_distinct=12):
    """Group card-id hits that sit close together in memory.

    A run of ids separated by less than `gap` bytes is one candidate structure.
    Deck-shaped clusters are small and span several expansions; the card
    database's own tables are huge and expansion-homogeneous.
    """
    clusters = []
    current = []
    last = None
    for item in hits:
        addr, cid = item[0], item[1]
        if last is not None and addr - last > gap:
            if len(set(c for _, c in current)) >= min_distinct:
                clusters.append(current)
            current = []
        current.append((addr, cid))
        last = addr
    if current and len(set(c for _, c in current)) >= min_distinct:
        clusters.append(current)
    return clusters


def cluster_summary(group):
    from collections import Counter

    ids = [c for _, c in group]
    counts = Counter(ids)
    distinct = sorted(counts)
    sets = sorted({c.split("_", 1)[0] for c in distinct})
    return {
        "start": group[0][0],
        "span": group[-1][0] - group[0][0],
        "total": len(ids),
        "distinct": distinct,
        "counts": dict(counts),   # occurrences per card id within the cluster
        "sets": sets,
    }


def find_markers(pid, markers):
    """Count occurrences of literal marker strings, UTF-16 and UTF-8.

    Useful for asking whether a field name such as `deckName` or `p2DeckList`
    is present in the heap at all.
    """
    needles = []
    for m in markers:
        needles.append((m, "utf-16", m.encode("utf-16-le")))
        needles.append((m, "utf-8", m.encode("utf-8")))

    found = {}

    def on_region(base, data):
        for label, enc, needle in needles:
            start = 0
            while True:
                i = data.find(needle, start)
                if i < 0:
                    break
                key = (label, enc)
                found[key] = found.get(key, 0) + 1
                start = i + 1

    handle = open_process(pid)
    try:
        stats = _read_all(handle, on_region)
    finally:
        k32.CloseHandle(handle)
    return found, stats


def marker_context(pid, marker, before=512, after=4096, encodings=("utf-16", "utf-8")):
    """Raw bytes surrounding each occurrence of `marker`.

    Finding a live `p2DeckList` string says a structure exists; reading what
    sits next to it says whether the cards are actually in there.
    """
    needles = []
    if "utf-16" in encodings:
        needles.append(("utf-16", marker.encode("utf-16-le")))
    if "utf-8" in encodings:
        needles.append(("utf-8", marker.encode("utf-8")))

    out = []

    def on_region(base, data):
        for enc, needle in needles:
            start = 0
            while True:
                i = data.find(needle, start)
                if i < 0:
                    break
                lo = max(0, i - before)
                hi = min(len(data), i + len(needle) + after)
                out.append({"addr": base + i, "enc": enc, "blob": data[lo:hi],
                            "offset": i - lo})
                start = i + 1

    handle = open_process(pid)
    try:
        stats = _read_all(handle, on_region)
    finally:
        k32.CloseHandle(handle)
    return out, stats


def readable(blob):
    """Both plausible decodings of a blob, as printable text."""
    u16 = re.sub(rb"(?:[\x20-\x7e]\x00){4,}",
                 lambda m: m.group().decode("utf-16-le").encode(), blob)
    text = re.sub(rb"[^\x20-\x7e]+", b" ", u16).decode("ascii", "replace")
    return re.sub(r"\s{2,}", " ", text).strip()


import json as _json

# A deck definition is stored as a JSON object of "cardId": count pairs. Require
# several pairs so tiny unrelated objects are ignored.
_DECK_JSON = re.compile(r'\{(?:\s*"[a-z0-9][a-z0-9_.-]{1,15}"\s*:\s*\d{1,2}\s*,?){8,}\s*\}')
_UTF16_RUN = re.compile(rb"(?:[\x09\x0a\x0d\x20-\x7e]\x00){24,}")


def _valid_deck_map(obj, valid_ids):
    if not isinstance(obj, dict) or not obj:
        return None
    hits = {k: v for k, v in obj.items()
            if isinstance(v, int) and k in valid_ids and 1 <= v <= 4}
    # a real deck: most keys are known card ids, total lands near 60
    if len(hits) < 8 or len(hits) < 0.8 * len(obj):
        return None
    if not (30 <= sum(hits.values()) <= 70):
        return None
    return hits


def find_deck_maps(pid, valid_ids):
    """Every `{cardId: count}` deck definition present in the heap.

    Returns a list of (address, encoding, {cardId: count}). Counts are exact -
    they come from the deck's own serialized form, not from proximity.
    """
    out = []
    seen = set()

    def consider(addr, enc, text):
        for m in _DECK_JSON.finditer(text):
            try:
                obj = _json.loads(m.group())
            except ValueError:
                continue
            deck = _valid_deck_map(obj, valid_ids)
            if not deck:
                continue
            key = tuple(sorted(deck.items()))
            if key in seen:
                continue
            seen.add(key)
            out.append((addr + m.start(), enc, deck))

    def on_region(base, data):
        consider(base, "utf-8", data.decode("latin-1"))
        for m in _UTF16_RUN.finditer(data):
            consider(base + m.start(), "utf-16", m.group().decode("utf-16-le"))

    handle = open_process(pid)
    try:
        stats = _read_all(handle, on_region)
    finally:
        k32.CloseHandle(handle)
    return out, stats


def score(counts, deck):
    """Fraction of `deck`'s distinct cards present in `counts`."""
    ids = set(deck.counts)
    if not ids:
        return 0.0
    return len(ids & set(counts)) / len(ids)


# Deck identity as it appears serialized in memory: DeckInfo (full, with cards -
# your own decks) or DeckBrief (name + cosmetics, no cards - the shareable form).
_DECKNAME = re.compile(r'"deckName"\s*:\s*"([^"]{0,60})"')
_FIELD = lambda k: re.compile(r'"' + k + r'"\s*:\s*"([^"]{0,80})"')
_DB, _CN, _SL = _FIELD("deckBox"), _FIELD("coin"), _FIELD("sleeve")
_CARDS = re.compile(r'"cards"\s*:\s*\{((?:\s*"[a-z0-9][a-z0-9_.-]{1,15}"\s*:\s*\d{1,2}\s*,?){1,})\}')
_PAIR = re.compile(r'"([a-z0-9][a-z0-9_.-]{1,15})"\s*:\s*(\d{1,2})')


def find_deck_identities(pid):
    """Every serialized deck identity in memory: name, cosmetics, and cards if present.

    Catches both DeckInfo (your decks, with a cards map) and DeckBrief (name +
    cosmetics only) - the latter is the shareable opponent form.
    """
    out = []
    seen = set()

    def scan_text(text):
        for m in _DECKNAME.finditer(text):
            lo = max(0, m.start() - 4000)
            hi = min(len(text), m.end() + 4000)
            win = text[lo:hi]
            name = m.group(1)
            box = (_DB.search(win) or [None, ""])[1] if _DB.search(win) else ""
            coin = (_CN.search(win) or [None, ""])[1] if _CN.search(win) else ""
            sleeve = (_SL.search(win) or [None, ""])[1] if _SL.search(win) else ""
            cm = _CARDS.search(win)
            cards = {}
            if cm:
                for pk, pv in _PAIR.findall(cm.group(1)):
                    cards[pk] = int(pv)
            key = (name, box, coin, sleeve, tuple(sorted(cards.items())))
            if key in seen:
                continue
            seen.add(key)
            out.append({"deckName": name, "deckBox": box, "coin": coin,
                        "sleeve": sleeve, "cards": cards})

    def on_region(base, data):
        scan_text(data.decode("latin-1"))
        for m in _UTF16_RUN.finditer(data):
            scan_text(m.group().decode("utf-16-le"))

    handle = open_process(pid)
    try:
        stats = _read_all(handle, on_region)
    finally:
        k32.CloseHandle(handle)
    return out, stats


# A full player identity serializes (via Json.NET [JsonProperty]) with these
# fields; grab whatever is present in a window around a "playerName" anchor.
_PLAYERNAME = re.compile(r'"playerName"\s*:\s*"([^"]{0,40})"')

def _sfield(k):
    return re.compile(r'"' + k + r'"\s*:\s*"([^"]{0,60})"')

def _nfield(k):
    return re.compile(r'"' + k + r'"\s*:\s*(\d{1,10})')

_PLAYER_STR = {k: _sfield(k) for k in
               ("playerID", "playerId", "seasonLeague", "seasonTier")}
_PLAYER_NUM = {k: _nfield(k) for k in
               ("elo", "playerExp", "playerWinStreak", "currentSeason",
                "seasonLeagueNumber", "prestigeNormalizedLevel", "deckSize")}


def find_player_identities(pid, window=6000):
    """Every serialized player identity in memory: name, rank/stats, and the
    nested deck (name + cosmetics, plus cards if a reveal populated them)."""
    out = []
    seen = set()

    def scan_text(text):
        for m in _PLAYERNAME.finditer(text):
            lo = max(0, m.start() - 200)
            hi = min(len(text), m.end() + window)
            win = text[lo:hi]
            rec = {"playerName": m.group(1)}
            for k, rx in _PLAYER_STR.items():
                mm = rx.search(win)
                if mm:
                    rec[k] = mm.group(1)
            for k, rx in _PLAYER_NUM.items():
                mm = rx.search(win)
                if mm:
                    rec[k] = int(mm.group(1))
            # nested deck identity
            dn = _DECKNAME.search(win)
            if dn:
                rec["deckName"] = dn.group(1)
                for k, rx in (("deckBox", _DB), ("coin", _CN), ("sleeve", _SL)):
                    mm = rx.search(win)
                    if mm:
                        rec[k] = mm.group(1)
                cm = _CARDS.search(win)
                if cm:
                    rec["cards"] = {pk: int(pv) for pk, pv in _PAIR.findall(cm.group(1))}
            key = (rec.get("playerName"), rec.get("playerID") or rec.get("playerId"),
                   rec.get("deckName"), rec.get("elo"))
            if key in seen:
                continue
            seen.add(key)
            out.append(rec)

    def on_region(base, data):
        scan_text(data.decode("latin-1"))
        for mm in _UTF16_RUN.finditer(data):
            scan_text(mm.group().decode("utf-16-le"))

    handle = open_process(pid)
    try:
        stats = _read_all(handle, on_region)
    finally:
        k32.CloseHandle(handle)
    return out, stats


def read_qword(pid_handle, addr):
    """Read an 8-byte little-endian value from the process."""
    buf = ctypes.create_string_buffer(8)
    read = ctypes.c_size_t(0)
    if not k32.ReadProcessMemory(pid_handle, ctypes.c_void_p(addr), buf, 8, ctypes.byref(read)):
        return None
    if read.value != 8:
        return None
    return int.from_bytes(buf.raw, "little")


def read_mono_string(pid_handle, obj_addr, max_len=200):
    """Read a Mono System.String object. Layout (64-bit): 16-byte object header,
    int32 length at +16, UTF-16 chars at +20."""
    header = ctypes.create_string_buffer(20)
    read = ctypes.c_size_t(0)
    if not k32.ReadProcessMemory(pid_handle, ctypes.c_void_p(obj_addr), header, 20, ctypes.byref(read)):
        return None
    if read.value != 20:
        return None
    length = int.from_bytes(header.raw[16:20], "little")
    if length <= 0 or length > max_len:
        return None
    nbytes = length * 2
    cbuf = ctypes.create_string_buffer(nbytes)
    if not k32.ReadProcessMemory(pid_handle, ctypes.c_void_p(obj_addr + 20), cbuf, nbytes, ctypes.byref(read)):
        return None
    try:
        return cbuf.raw[:read.value].decode("utf-16-le")
    except UnicodeDecodeError:
        return None


def find_strings(pid, pattern, encodings=("utf-16", "utf-8"), limit=None,
                 biggest_first=True):
    """Every managed/plain string matching `pattern`, as (address, value, enc).

    For utf-16 the address is of the char data; the Mono String object begins
    20 bytes earlier (16-byte header + int32 length). `limit` stops the scan
    once that many hits are collected (early-exit)."""
    hits = []
    ascii_rx = re.compile(pattern.encode("latin-1"))
    utf16_rx = re.compile(_ascii_to_utf16_pattern(pattern))

    def on_region(base, data):
        if "utf-8" in encodings:
            for m in ascii_rx.finditer(data):
                hits.append((base + m.start(), m.group().decode("latin-1"), "utf-8"))
        if "utf-16" in encodings:
            for m in utf16_rx.finditer(data):
                try:
                    hits.append((base + m.start(), m.group().decode("utf-16-le"), "utf-16"))
                except UnicodeDecodeError:
                    pass
        return limit is not None and len(hits) >= limit

    handle = open_process(pid)
    try:
        stats = _read_all(handle, on_region, biggest_first=biggest_first)
    finally:
        k32.CloseHandle(handle)
    return hits, stats


def _ascii_to_utf16_pattern(pattern):
    """Turn a simple char-class/literal ascii regex into its UTF-16LE byte form.
    Supports literals and [..] classes and quantifiers by inserting \x00 after
    each single-byte matcher - good enough for cosmetic id patterns."""
    out = bytearray()
    i = 0
    p = pattern
    while i < len(p):
        c = p[i]
        if c == "[":
            j = p.index("]", i)
            cls = p[i:j + 1]
            out += cls.encode("latin-1")
            out += rb"\x00"
            i = j + 1
            # attach following quantifier if any
            if i < len(p) and p[i] in "*+?{":
                if p[i] == "{":
                    k = p.index("}", i)
                    out += p[i:k + 1].encode("latin-1")
                    i = k + 1
                else:
                    out += p[i].encode("latin-1")
                    i += 1
        else:
            out += re.escape(c).encode("latin-1")
            out += rb"\x00"
            i += 1
    return bytes(out)


def find_referencing_objects(pid, target_addrs, before=96, after=400):
    """Find every 8-byte pointer in memory equal to one of `target_addrs`
    (String object addresses), then read the enclosing object's pointer fields
    and resolve each to a Mono string.

    This walks from a known string (e.g. a player name) out to its sibling
    reference fields (deckName, deckBox, coin, sleeve, ...) without needing exact
    struct offsets."""
    targets = set(target_addrs)
    tbytes = {a.to_bytes(8, "little"): a for a in targets}
    results = []
    handle = open_process(pid)

    def resolve_window(center):
        strings = []
        buf = ctypes.create_string_buffer(before + after)
        read = ctypes.c_size_t(0)
        start = center - before
        if not k32.ReadProcessMemory(handle, ctypes.c_void_p(start), buf, before + after, ctypes.byref(read)):
            return strings
        raw = buf.raw[:read.value]
        for off in range(0, len(raw) - 8, 8):
            ptr = int.from_bytes(raw[off:off + 8], "little")
            if ptr < 0x10000 or ptr > 0x7FFFFFFFFFFF:
                continue
            s = read_mono_string(handle, ptr)
            if s and s.isprintable() and 1 <= len(s) <= 60:
                strings.append((start + off, s))
        return strings

    def on_region(base, data):
        # find any target pointer value in this region
        for tb, ta in tbytes.items():
            idx = 0
            while True:
                i = data.find(tb, idx)
                if i < 0:
                    break
                if i % 8 == (base % 8):  # 8-byte aligned pointer field
                    field_addr = base + i
                    results.append({"field_addr": field_addr, "target": ta,
                                    "siblings": resolve_window(field_addr)})
                idx = i + 1

    try:
        stats = _read_all(handle, on_region)
    finally:
        k32.CloseHandle(handle)
    return results, stats


# --- DeckInfo / PlayerDetails object layout (Mono, 64-bit), from RE ---
# DeckInfo object:  +0 vtable, +16 cards(Dictionary), +24 deckName, +32 deckDefID,
#                   +40 deckBox, +48 coin, +56 sleeve
DI_CARDS, DI_NAME, DI_BOX, DI_COIN, DI_SLEEVE = 16, 24, 40, 48, 56


def read_u32(handle, addr):
    buf = ctypes.create_string_buffer(4)
    read = ctypes.c_size_t(0)
    if not k32.ReadProcessMemory(handle, ctypes.c_void_p(addr), buf, 4, ctypes.byref(read)) or read.value != 4:
        return None
    return int.from_bytes(buf.raw, "little")


def read_cards_dict(handle, dict_addr, valid_ids):
    """Parse a Mono Dictionary<string,int> of cardId->count.
    entries array at dict+24; Mono array length at arr+24, elements from arr+32;
    Entry stride 24, key ptr at +8, value int at +16."""
    if not dict_addr or dict_addr < 0x10000:
        return {}
    entries = read_qword(handle, dict_addr + 24)
    if not entries or entries < 0x10000:
        return {}
    length = read_qword(handle, entries + 24)
    if not length or length <= 0 or length > 5000:
        return {}
    out = {}
    base = entries + 32
    for i in range(length):
        e = base + i * 24
        kp = read_qword(handle, e + 8)
        if not kp or kp < 0x10000:
            continue
        s = read_mono_string(handle, kp)
        if s and s in valid_ids:
            v = read_u32(handle, e + 16)
            if v and 1 <= v <= 59:  # basic energy can exceed 4
                out[s] = v
    return out


def scan_pointers_to(pid, target_addrs, on_hit=None, biggest_first=True):
    """Every 8-byte-aligned location holding a pointer equal to one of
    `target_addrs`. Returns list of (field_addr, target).

    If `on_hit(field_addr, target)` is given and returns truthy, the scan stops
    early - used to bail out as soon as the opponent's deck is resolved."""
    tbytes = {a.to_bytes(8, "little"): a for a in target_addrs}
    found = []
    done = [False]

    def on_region(base, data):
        for tb, ta in tbytes.items():
            idx = 0
            while True:
                i = data.find(tb, idx)
                if i < 0:
                    break
                if (base + i) % 8 == 0:
                    found.append((base + i, ta))
                    if on_hit and on_hit(base + i, ta):
                        done[0] = True
                        return True
                idx = i + 1
        return False

    handle = open_process(pid)
    try:
        stats = _read_all(handle, on_region, biggest_first=biggest_first)
    finally:
        k32.CloseHandle(handle)
    return found, stats


def read_deckinfo(handle, obj_addr, valid_ids):
    """Read a DeckInfo object at obj_addr. Returns dict or None if it doesn't
    look like one."""
    name = read_mono_string(handle, read_qword(handle, obj_addr + DI_NAME) or 0)
    if not name or not name.isprintable():
        return None
    box = read_mono_string(handle, read_qword(handle, obj_addr + DI_BOX) or 0) or ""
    coin = read_mono_string(handle, read_qword(handle, obj_addr + DI_COIN) or 0) or ""
    sleeve = read_mono_string(handle, read_qword(handle, obj_addr + DI_SLEEVE) or 0) or ""
    cards = read_cards_dict(handle, read_qword(handle, obj_addr + DI_CARDS) or 0, valid_ids)
    return {"addr": obj_addr, "deckName": name, "deckBox": box,
            "coin": coin, "sleeve": sleeve, "cards": cards}


def find_literal(pid, text, limit=None, biggest_first=True):
    """Fast raw search for a literal string (no regex) in UTF-16LE and UTF-8.
    Returns (addr, value, enc). `limit` early-exits after that many hits."""
    needles = [(text.encode("utf-16-le"), "utf-16"), (text.encode("utf-8"), "utf-8")]
    hits = []

    def on_region(base, data):
        for nb, enc in needles:
            idx = 0
            while True:
                i = data.find(nb, idx)
                if i < 0:
                    break
                hits.append((base + i, text, enc))
                idx = i + 1
        return limit is not None and len(hits) >= limit

    handle = open_process(pid)
    try:
        stats = _read_all(handle, on_region, biggest_first=biggest_first)
    finally:
        k32.CloseHandle(handle)
    return hits, stats


import re as _re


def _is_player_name(s):
    """A human player name, not a card id / cosmetic / guid / deck field."""
    if not s or not s.isprintable() or not (2 <= len(s) <= 30):
        return False
    if s[:3] in ("db_", "cn_", "cs_"):
        return False
    if _re.fullmatch(r"[a-z0-9-]{2,8}_[0-9]{1,3}(?:_[a-z]{2})?", s):  # card id
        return False
    if _re.fullmatch(r"[A-Za-z0-9+/_-]{20,24}", s) or (s.count("-") >= 4):  # guid
        return False
    return True


def find_match_decks(pid, vtable, valid_ids):
    """Return the decks actually in the live match, each with its owning player.

    A player's deck library is all resident in memory, so content can't tell the
    opponent's deck from your own. What distinguishes the two *match* decks is
    that each is pointed at by a PlayerDetails object (which carries a
    playerName); library decks are only referenced by the deck-manager UI.

    Returns list of {di, playerName, deckSize} for the match decks.
    """
    # 1) enumerate every DeckInfo object (via the class vtable)
    handle = open_process(pid)
    di_by_addr = {}
    try:
        def on_di(obj, _t):
            di = read_deckinfo(handle, obj, valid_ids)
            if di and di["cards"]:
                di_by_addr[obj] = di
            return False
        scan_pointers_to(pid, {vtable}, on_hit=on_di, biggest_first=True)
    finally:
        k32.CloseHandle(handle)
    if not di_by_addr:
        return []

    # 2) find PlayerDetails objects that point at one of those DeckInfos.
    #    A PlayerDetails is strongly signatured: near its deckInfo field sit a
    #    player NAME, a GUID player-id, and a deck SIZE. Requiring all three
    #    rejects the deck-manager UI objects that also reference library decks.
    handle = open_process(pid)
    match = {}

    def on_ref(field_addr, di_addr):
        if di_addr in match:
            return False
        name = guid = size = elo = None
        for off in range(-72, 72, 8):
            v = read_qword(handle, field_addr + off)
            if v and 0x10000 < v < 0x7FFFFFFFFFFF:
                s = read_mono_string(handle, v)
                if s:
                    base = s.split("{")[0]
                    if _looks_like_guid(base):
                        guid = guid or base
                    elif _is_player_name(base):
                        name = name or base
            iv = read_u32(handle, field_addr + off) or 0
            if 40 <= iv <= 70:
                size = size or iv
            elif 100 <= iv <= 9999:   # competitiveElo (0 in unranked)
                elo = elo or iv
        if name and guid and size:   # all three = a real PlayerDetails
            match[di_addr] = {"di": di_by_addr[di_addr], "playerName": name,
                              "deckSize": size, "elo": elo, "addr": di_addr}
        return len(match) >= 2   # a match has two players - stop once both found

    try:
        scan_pointers_to(pid, set(di_by_addr), on_hit=on_ref, biggest_first=True)
    finally:
        k32.CloseHandle(handle)
    return list(match.values())


def _looks_like_guid(s):
    return bool(_re.fullmatch(r"[A-Za-z0-9+/_-]{20,24}", s)) or s.count("-") >= 4
