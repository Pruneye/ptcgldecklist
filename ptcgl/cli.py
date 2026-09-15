"""Command line over the local PTCGL install."""

import argparse
import json
import sys

# card names carry accents; Windows consoles default to a codepage that cannot
# encode them, which would otherwise crash on the first Pokemon printed.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from . import cards as cards_mod, decks as decks_mod, paths, watch


def _print_deck(deck, index, fmt):
    if fmt == "export":
        print(deck.export(index))
        return
    if fmt == "json":
        print(json.dumps({
            "name": deck.name,
            "source": deck.source,
            "size": deck.size,
            "meta": deck.meta,
            "cards": [
                {"id": c.id, "name": c.name, "set": c.set_code,
                 "number": c.number, "category": c.category_name, "count": n}
                for c, n in deck.resolve(index)
            ],
        }, indent=2, ensure_ascii=False))
        return
    for card, n in deck.resolve(index):
        print(f"  {n:>2}x {card.name:<32} {card.set_code} {card.number}")


def cmd_check(args):
    for label, ok in paths.check().items():
        print(f"{'ok ' if ok else 'MISSING'}  {label}")
    pid = watch.find_process()
    print(f"{'ok ' if pid else '-- '}  client running" + (f" (pid {pid})" if pid else ""))
    if pid:
        for c in watch.connections(pid):
            print(f"        {c['state']:<12} {c['remote']}")


def cmd_cards(args):
    index = cards_mod.load(refresh=args.refresh)
    print(f"{len(index)} cards indexed")
    if args.lookup:
        for cid in args.lookup:
            card = index.get(cid)
            print(f"  {cid}: {card.name} ({card.set_code} {card.number}, "
                  f"{card.category_name}, reg {card.regulation})" if card
                  else f"  {cid}: not found")


def cmd_mydecks(args):
    index = cards_mod.load()
    found = decks_mod.my_decks()
    if not found:
        print("no local deck state found - open the client and pick a deck", file=sys.stderr)
        return 1
    for deck in found:
        if args.name and args.name.lower() not in deck.name.lower():
            continue
        meta = deck.meta
        record = ""
        if meta.get("wins") is not None:
            w, l = int(meta["wins"]), int(meta["losses"])
            total = w + l
            record = f" - {w}W {l}L" + (f" ({w / total:.0%})" if total else "")
        if args.format != "json":
            print(f"\n{deck.name} [{deck.size} cards]{record}")
            if meta.get("last_played"):
                print(f"  last played {meta['last_played']}")
        _print_deck(deck, index, args.format)


def cmd_aidecks(args):
    index = cards_mod.load()
    found = decks_mod.ai_decks()
    matches = [d for d in found if not args.name or args.name.lower() in d.name.lower()]
    if args.list:
        for deck in matches:
            print(f"{deck.name:<48} {deck.size:>3} cards  [{deck.source}]")
        print(f"\n{len(matches)} of {len(found)} decks")
        return
    for deck in matches:
        if args.format != "json":
            print(f"\n{deck.name} [{deck.size} cards, {deck.source}]")
        _print_deck(deck, index, args.format)


def cmd_scan(args):
    """Look for deck-shaped card-id clusters in the live client's heap.

    The whole card database is resident in memory, so presence of an id means
    nothing. Clusters - a handful of ids from several expansions packed tightly
    together - are what a real decklist looks like.
    """
    from . import memscan

    pid = watch.find_process()
    if not pid:
        print("client is not running", file=sys.stderr)
        return 1

    index = cards_mod.load()
    known = decks_mod.ai_decks() + decks_mod.my_decks()

    print(f"scanning pid {pid} ...")
    hits, stats = memscan.card_hits(pid, set(index))
    print(f"read {stats['bytes'] / 1e6:.0f} MB across {stats['regions']} regions; "
          f"{len(hits)} card-id occurrences")

    groups = memscan.cluster(hits, gap=args.gap, min_distinct=args.min_distinct)
    print(f"{len(groups)} clusters with >= {args.min_distinct} distinct ids\n")

    summaries = [memscan.cluster_summary(g) for g in groups]
    # a decklist is compact and mixes expansions; card-database tables are huge
    # and expansion-homogeneous
    decklike = [s for s in summaries
                if len(s["sets"]) >= args.min_sets and len(s["distinct"]) <= args.max_distinct]
    decklike.sort(key=lambda s: -len(s["distinct"]))

    if args.save:
        with open(args.save, "w", encoding="utf-8") as fh:
            json.dump([{"distinct": s["distinct"], "sets": s["sets"],
                        "total": s["total"], "span": s["span"]} for s in decklike], fh)
        print(f"saved {len(decklike)} clusters to {args.save}")

    if args.diff:
        # The client keeps ~283 deck configs resident, so almost every cluster
        # is noise. What matters is what a match *adds*.
        #
        # Compare multiplicity, not mere presence: an AI opponent's deck is
        # already resident as config before the match, and starting the match
        # instantiates a *second* copy with an identical card set. Deduplicating
        # by card set alone would hide exactly the event we are looking for.
        from collections import Counter

        with open(args.diff, encoding="utf-8") as fh:
            before = Counter(tuple(c["distinct"]) for c in json.load(fh))
        now = Counter(tuple(s["distinct"]) for s in decklike)

        added = {fp: n - before.get(fp, 0) for fp, n in now.items()
                 if n > before.get(fp, 0)}
        fresh, used = [], Counter()
        for s in decklike:
            fp = tuple(s["distinct"])
            if fp in added and used[fp] < added[fp]:
                used[fp] += 1
                s["copies"] = f"{now[fp]} now vs {before.get(fp, 0)} before"
                fresh.append(s)

        brand_new = sum(1 for fp in added if fp not in before)
        print(f"{len(fresh)} clusters added since {args.diff} "
              f"({brand_new} with card sets never seen before, "
              f"{len(fresh) - brand_new} extra copies of known sets)\n")
        decklike = fresh

    scored = []
    for s in decklike:
        present = set(s["distinct"])
        pct, deck = max(((memscan.score(present, d), d) for d in known),
                        key=lambda p: p[0], default=(0.0, None))
        scored.append((s, pct, deck))

    if args.unknown:
        # A human opponent's deck matches nothing in local config, so for that
        # case the interesting clusters are the ones we *cannot* explain.
        mine = {c for d in decks_mod.my_decks() for c in d.counts}
        scored = [t for t in scored if t[1] < args.unknown_below
                  and not set(t[0]["distinct"]) <= mine]
        scored.sort(key=lambda t: -len(t[0]["distinct"]))
        print(f"clusters matching no known deck below {args.unknown_below:.0%} "
              f"and not wholly yours: {len(scored)}")

    print(f"deck-shaped clusters ({len(scored)}):")
    if not scored:
        print("  none - no match loaded, or the layout is not what we expect")

    for s, pct, deck in scored[:args.top]:
        # In count-only mode, report the shape of each cluster but never the
        # card identities - enough to tell a decklist from board state without
        # extracting a third party's deck.
        label = ("known" if pct >= 0.6 else "unknown") if args.count_only \
            else (f"{deck.name} [{deck.source}]" if deck else "-")
        print(f"\n  @{s['start']:#014x}  {len(s['distinct'])} distinct over "
              f"{len(s['sets'])} sets, span {s['span']} B")
        print(f"    best known match: {pct:.0%}  {label}")
        if s.get("copies"):
            print(f"    copies: {s['copies']}")
        if args.verbose and not args.count_only:
            names = [(index[c].name if c in index else c) for c in s["distinct"]]
            print(f"    {', '.join(sorted(names)[:args.max_distinct])}")


def cmd_probe(args):
    """Check whether given field-name strings exist in the client's heap."""
    from . import memscan

    pid = watch.find_process()
    if not pid:
        print("client is not running", file=sys.stderr)
        return 1

    markers = args.marker or [
        "deckName", "p1DeckList", "p2DeckList", "deckListP1", "deckListP2",
        "opponentDeck", "revealDeck", "ServerAuthoritativeMetadata",
        "SignedMatchContext", "JoinGame",
    ]
    print(f"probing pid {pid} for {len(markers)} markers ...")
    found, stats = memscan.find_markers(pid, markers)
    print(f"read {stats['bytes'] / 1e6:.0f} MB across {stats['regions']} regions\n")
    for m in markers:
        u16 = found.get((m, "utf-16"), 0)
        u8 = found.get((m, "utf-8"), 0)
        flag = "HIT " if (u16 or u8) else "  - "
        print(f"  {flag} {m:<30} utf16={u16:<6} utf8={u8}")


def cmd_dump(args):
    """Print what sits next to a marker string in the client's heap."""
    from . import memscan

    pid = watch.find_process()
    if not pid:
        print("client is not running", file=sys.stderr)
        return 1

    index = cards_mod.load()
    print(f"dumping context around {args.marker!r} in pid {pid} ...")
    hits, stats = memscan.marker_context(pid, args.marker, args.before, args.after)
    print(f"read {stats['bytes'] / 1e6:.0f} MB; {len(hits)} occurrences\n")

    for h in hits[:args.top]:
        text = memscan.readable(h["blob"])
        found = sorted({m for m in cards_mod.CARD_ID.findall(text) if m in index})
        print(f"--- @{h['addr']:#014x} [{h['enc']}] {len(h['blob'])} B, "
              f"{len(found)} card ids nearby")
        print(f"    {text[:args.width]}")
        if found:
            names = [f"{index[c].name} ({c})" for c in found]
            print(f"    cards: {', '.join(names[:40])}")
        print()


def _print_decklist(counts, index, title):
    """Render a card-id -> count map as a grouped decklist."""
    groups = {cards_mod.POKEMON: [], cards_mod.TRAINER: [], cards_mod.ENERGY: []}
    other = []
    for cid, n in counts.items():
        card = index.get(cid)
        cat = card.category if card else 0
        name = card.name if card else cid
        setc = card.set_code if card else cards_mod._set_code(cid)
        num = card.number if card else ""
        groups.get(cat, other).append((n, name, setc, num))

    total = sum(counts.values())
    print(f"\n=== {title} ({total} cards) ===")
    for cat in (cards_mod.POKEMON, cards_mod.TRAINER, cards_mod.ENERGY):
        entries = sorted(groups[cat], key=lambda e: (-e[0], e[1]))
        if not entries:
            continue
        print(f"\n{cards_mod.CATEGORY_NAMES[cat]}: {sum(e[0] for e in entries)}")
        for n, name, setc, num in entries:
            print(f"  {n} {name} {setc} {num}")
    if other:
        print(f"\nOther: {sum(e[0] for e in other)}")
        for n, name, setc, num in sorted(other, key=lambda e: (-e[0], e[1])):
            print(f"  {n} {name} {setc} {num}")


def _best_cluster(summaries, target_ids):
    """Cluster whose distinct ids overlap `target_ids` most."""
    best, best_score = None, -1.0
    for s in summaries:
        overlap = len(set(s["distinct"]) & target_ids)
        score = overlap / max(len(target_ids), 1)
        if score > best_score:
            best, best_score = s, score
    return best, best_score


def cmd_deck(args):
    """Recover full decklists with exact copy counts from live memory.

    Deck definitions are stored as JSON `{cardId: count}` objects, so counts are
    read straight from the deck's own serialized form.
    """
    from . import memscan

    pid = watch.find_process()
    if not pid:
        print("client is not running", file=sys.stderr)
        return 1

    index = cards_mod.load()
    print(f"scanning pid {pid} ...")
    maps, stats = memscan.find_deck_maps(pid, set(index))
    print(f"read {stats['bytes'] / 1e6:.0f} MB; {len(maps)} distinct deck maps\n")

    if args.save_maps:
        with open(args.save_maps, "w", encoding="utf-8") as fh:
            json.dump([{"addr": addr, "enc": enc, "deck": deck}
                       for addr, enc, deck in maps], fh)
        print(f"wrote {len(maps)} deck maps to {args.save_maps}")
        return

    if args.against:
        # Validation: reproduce a deck whose exact list we already know.
        target = None
        for d in decks_mod.my_decks():
            if args.against.lower() in d.name.lower():
                target = d
                break
        if not target:
            print(f"no local deck matching {args.against!r}", file=sys.stderr)
            return 1

        truth = target.counts
        best, best_ov = None, -1
        for _, _, deck in maps:
            ov = len(set(deck) & set(truth))
            if ov > best_ov:
                best, best_ov = deck, ov
        if best is None:
            print("no deck map found to validate against")
            return 1

        exact = sum(1 for c in truth if best.get(c) == truth[c])
        present = sum(1 for c in truth if c in best)
        extra = [c for c in best if c not in truth]
        print(f"validating against {target.name} ({sum(truth.values())} cards):")
        print(f"  distinct present:  {present}/{len(truth)}")
        print(f"  exact count match: {exact}/{len(truth)}")
        print(f"  recovered total:   {sum(best.values())} vs true {sum(truth.values())}")
        print(f"  spurious cards:    {len(extra)}")
        if args.show:
            for c in sorted(truth):
                mark = "ok " if best.get(c) == truth[c] else "XX "
                print(f"    {mark} {c:<12} true={truth[c]} got={best.get(c, 0)} "
                      f"{index[c].name if c in index else ''}")
        verdict = "TRUSTWORTHY" if exact == len(truth) and not extra else "NOT reliable yet"
        print(f"\n  count recovery: {verdict}")
        return

    # Recovery: the opponent's deck is the map that matches none of the decks
    # already resident as local config (your decks + the 283 AI/precon decks).
    known = decks_mod.ai_decks() + decks_mod.my_decks()

    def closest(ids):
        best, name = 0.0, None
        for d in known:
            s = len(ids & set(d.counts)) / max(len(ids | set(d.counts)), 1)
            if s > best:
                best, name = s, d.name
        return best, name

    ranked = []
    for addr, enc, deck in maps:
        sim, name = closest(set(deck))
        ranked.append((sim, addr, enc, deck, name))
    ranked.sort(key=lambda t: t[0])   # least like any known deck first

    print(f"{len(maps)} deck maps found; showing the {args.top} least like any "
          f"local/AI deck (those are opponent candidates):\n")
    for sim, addr, enc, deck, name in ranked[:args.top]:
        tag = f"closest known: {sim:.0%} {name}" if name else "no known match"
        _print_decklist(deck, index, f"deck @{addr:#x} [{enc}] - {tag}")


def cmd_capture(args):
    """One memory scan that saves every card occurrence to disk.

    Lets all cluster/count analysis run offline afterwards - no rescanning.
    """
    from . import memscan

    pid = watch.find_process()
    if not pid:
        print("client is not running", file=sys.stderr)
        return 1

    index = cards_mod.load()
    print(f"scanning pid {pid} (match form included) ...")
    hits, stats = memscan.card_hits(pid, set(index), match_form=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"pid": pid, "bytes": stats["bytes"],
                   "hits": [[a, c, f] for a, c, f in hits]}, fh)
    print(f"read {stats['bytes'] / 1e6:.0f} MB; wrote {len(hits)} card "
          f"occurrences to {args.out}")


def _render_deckinfo(di, index, player=None):
    """Pretty-print one DeckInfo (+ optional player stats) as a clean readout."""
    line = "─" * 60
    print(line)
    if player and player.get("playerName"):
        print(f"  PLAYER    {player['playerName']}")
        for label, key in (("player id", "playerId"), ("rank/elo", "elo"),
                           ("deck size", "deckSize")):
            if player.get(key) is not None:
                print(f"  {label:<9} {player[key]}")
    print(f"  DECK     {di['deckName']}")
    for label, key in (("box", "deckBox"), ("coin", "coin"), ("sleeve", "sleeve")):
        if di.get(key):
            print(f"  {label:<9}{di[key]}")
    cards = di.get("cards") or {}
    total = sum(cards.values())
    print(f"  SIZE     {total} cards, {len(cards)} distinct")
    print(line)
    if not cards:
        print("  (card list not populated for this deck)")
        return
    groups = {cards_mod.POKEMON: [], cards_mod.TRAINER: [], cards_mod.ENERGY: []}
    other = []
    for cid, n in cards.items():
        c = index.get(cid)
        entry = (n, c.name if c else cid, c.set_code if c else "", c.number if c else "")
        groups.get(c.category if c else 0, other).append(entry)
    for cat in (cards_mod.POKEMON, cards_mod.TRAINER, cards_mod.ENERGY):
        rows = sorted(groups[cat], key=lambda e: (-e[0], e[1]))
        if not rows:
            continue
        print(f"\n  {cards_mod.CATEGORY_NAMES[cat].upper()}: {sum(e[0] for e in rows)}")
        for n, name, sc, num in rows:
            print(f"    {n}  {name} {sc} {num}")
    print()


def _read_player_stats(pid, deckinfo_addr):
    """Hop from a DeckInfo to the PlayerDetails that owns it and read stats.

    PlayerDetails.deckInfo points at the DeckInfo, with playerName/playerId
    (strings) and deckSize/elo (ints) as sibling fields."""
    from . import memscan

    fields, _ = memscan.scan_pointers_to(pid, {deckinfo_addr})
    handle = memscan.open_process(pid)
    best = {}
    try:
        for field_addr, _ in fields:
            names, ids, ints = [], [], []
            for off in range(-56, 64, 8):
                v = memscan.read_qword(handle, field_addr + off)
                if v is None:
                    continue
                if 0x10000 < v < 0x7FFFFFFFFFFF:
                    s = memscan.read_mono_string(handle, v)
                    if s and s.isprintable() and 1 <= len(s) <= 40:
                        (ids if _looks_like_guid(s) else names).append(s)
                iv = v & 0xFFFFFFFF
                if 1 <= iv <= 60000:
                    ints.append(iv)
            if names:
                best = {"playerName": names[0],
                        "playerId": ids[0] if ids else None,
                        "deckSize": next((i for i in ints if 40 <= i <= 70), None),
                        "elo": next((i for i in ints if 100 <= i <= 5000), None)}
                break
    finally:
        memscan.k32.CloseHandle(handle)
    return best


def _looks_like_guid(s):
    import re as _re
    return bool(_re.fullmatch(r"[A-Za-z0-9+/_-]{20,24}", s)) or "-" in s and len(s) > 30


def _local_account_name():
    """Best-effort local account name, from the client's Unity registry keys
    (e.g. STARTUP_<name>). Returns None if it can't be determined - the readout
    then just shows both players by name without a YOU/OPPONENT label."""
    import subprocess
    import re as _re
    try:
        out = subprocess.run(
            ["reg", "query", r"HKCU\Software\pokemon\Pokemon TCG Live"],
            capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    names = set(_re.findall(r"STARTUP_([A-Za-z0-9_]+)_h\d", out))
    return next(iter(names)) if len(names) == 1 else None


def _vtable_cache_path():
    import tempfile, os
    return os.path.join(tempfile.gettempdir(), "ptcgl-vtable.json")


def _vtable_cache_get(pid):
    """The DeckInfo class vtable is stable for a process's life, so cache it per
    pid and skip the (slow) bootstrap on repeat runs in the same session."""
    try:
        with open(_vtable_cache_path(), encoding="utf-8") as fh:
            c = json.load(fh)
        return c["vtable"] if c.get("pid") == pid else None
    except (OSError, ValueError, KeyError):
        return None


def _vtable_cache_put(pid, vtable):
    try:
        with open(_vtable_cache_path(), "w", encoding="utf-8") as fh:
            json.dump({"pid": pid, "vtable": vtable}, fh)
    except OSError:
        pass


def _deck_baseline_path():
    import tempfile, os
    return os.path.join(tempfile.gettempdir(), "ptcgl-deck-baseline.json")


def _deck_baseline_put(pid, decks):
    """Save deck fingerprints resident at the menu, keyed to pid.

    `decks` is keyed by (name, ((cardId, count), ...)). Serialize each key as
    [name, [[cardId, count], ...]]."""
    fps = [[name, [list(c) for c in cards]] for (name, cards) in decks]
    try:
        with open(_deck_baseline_path(), "w", encoding="utf-8") as fh:
            json.dump({"pid": pid, "fps": fps}, fh)
    except OSError:
        pass


def _deck_baseline_get(pid):
    """Return the baseline fingerprint set for this pid, or None."""
    try:
        with open(_deck_baseline_path(), encoding="utf-8") as fh:
            c = json.load(fh)
        if c.get("pid") != pid:
            return None
        return {(name, tuple(tuple(c) for c in cards)) for name, cards in c["fps"]}
    except (OSError, ValueError, KeyError):
        return None


def _bootstrap_deckinfo_vtable(pid, names, valid, memscan):
    """Find the DeckInfo class vtable once, via a known local deck name.

    Anchor on your deck name (rare string), find the pointer to it (a
    DeckInfo.deckName field), verify the object is really a DeckInfo, and read
    its vtable. Early-exits as soon as one is confirmed."""
    print("  bootstrapping DeckInfo class (first run this session)")
    # We only need ONE loaded deck name to anchor on, so stop at the first that's
    # actually present in memory instead of scanning for all of them.
    name_objs = set()
    for n in names:
        hits, _ = memscan.find_literal(pid, n, limit=50)
        name_objs |= {a - 20 for a, _, enc in hits if enc == "utf-16"}
        if name_objs:
            break
    if not name_objs:
        return None

    handle = memscan.open_process(pid)
    result = [None]

    def on_hit(field_addr, _target):
        obj = field_addr - memscan.DI_NAME
        di = memscan.read_deckinfo(handle, obj, valid)
        # A real loaded deck (yours or the opponent's) has a populated card list -
        # that's the reliable signal. The deck-box cosmetic varies (e.g. the
        # default box is "DB_TCGL-default_redblue"), so don't gate on it.
        if di and sum(di["cards"].values()) >= 20:
            vt = memscan.read_qword(handle, obj)
            if vt and vt > 0x10000:
                result[0] = vt
                return True
        return False

    try:
        memscan.scan_pointers_to(pid, name_objs, on_hit=on_hit)
    finally:
        memscan.k32.CloseHandle(handle)
    return result[0]


def cmd_opponent(args):
    """Read the opponent's deck and identity live from the match in memory.

    Walks the client's own object graph: every loaded DeckInfo is found via its
    coin cosmetic, then its name, cosmetics and full card list are read straight
    out of the object. Your own deck is shown separately; the opponent's is
    whatever else carries a populated card list. With --stats it also hops to the
    owning PlayerDetails for player name / rank / deck size.
    """
    from . import memscan

    pid = watch.find_process()
    if not pid:
        print("client is not running", file=sys.stderr)
        return 1

    index = cards_mod.load()
    valid = set(index)
    my_names = {d.name for d in decks_mod.my_decks()}

    names = [d.name for d in decks_mod.my_decks()]
    if not names:
        print("no local deck found to bootstrap from - pick a deck in the client",
              file=sys.stderr)
        return 1

    print(f"scanning pid {pid} ...")
    vtable = None if args.refresh else _vtable_cache_get(pid)
    if vtable:
        print(f"  using cached DeckInfo class {vtable:#x}")
    else:
        vtable = _bootstrap_deckinfo_vtable(pid, names, valid, memscan)
        if not vtable:
            print("could not resolve DeckInfo class "
                  "(are you in a match with a deck loaded?)", file=sys.stderr)
            return 1
        _vtable_cache_put(pid, vtable)

    # Your whole deck library is resident in memory, so content can't tell your
    # decks from the opponent's. The two *match* decks are the ones owned by a
    # PlayerDetails (player name + GUID + deck size together); everything else is
    # your library. This needs no baseline and works any time during the match.
    print("  identifying match players")
    players = memscan.find_match_decks(pid, vtable, valid)
    if not players:
        print("\nno match players found - are you on the board in a match?")
        return 0

    local = _local_account_name()  # may be None if several accounts logged in here

    def role_of(name):
        if not local:
            return ""          # can't tell reliably - just show the name
        return "  [YOU]" if name.lower() == local.lower() else "  [OPPONENT]"

    # Past matches linger in memory this session; newest objects (highest
    # address) are the current match. Show newest first.
    players.sort(key=lambda p: -p["addr"])
    stale = len(players) > 2
    print(f"\n{len(players)} player-deck(s) in memory"
          + (" (newest first; earlier ones are past matches)" if stale else "")
          + ("" if local else " - shown by name") + "\n")
    for p in players[:args.top]:
        _render_deckinfo(p["di"], index,
                         {"playerName": p["playerName"] + role_of(p["playerName"]),
                          "deckSize": p["deckSize"], "elo": p.get("elo")})


def cmd_autowatch(args):
    """Keep running; auto-print the opponent's deck when a match is detected.

    Loops the match scan. When two player-owned decks appear (a match started)
    and the opponent's deck differs from the last one shown, it prints it, then
    waits for the next match. Ctrl+C to stop.
    """
    from . import memscan
    import time

    pid = watch.find_process()
    if not pid:
        print("client is not running", file=sys.stderr)
        return 1
    index = cards_mod.load()
    valid = set(index)
    names = [d.name for d in decks_mod.my_decks()]

    vtable = _vtable_cache_get(pid) or _bootstrap_deckinfo_vtable(pid, names, valid, memscan)
    if not vtable:
        print("could not resolve DeckInfo class (open the client to a deck once)",
              file=sys.stderr)
        return 1
    _vtable_cache_put(pid, vtable)
    local = _local_account_name()

    def opponent_of(players):
        """The current match's opponent: newest non-local player-deck. Past
        matches linger in memory, so we take the most recently allocated."""
        if not players:
            return None
        cand = [p for p in players
                if not (local and p["playerName"].lower() == local.lower())]
        return max(cand or players, key=lambda p: p["addr"])

    print(f"watching pid {pid} for matches (Ctrl+C to stop)...")
    shown = None
    try:
        while True:
            if watch.find_process() != pid:      # client closed/restarted
                print("client gone - stopping."); break
            # newest non-local deck = the opponent in the current match; print it
            # the first time we see it, and again whenever it changes.
            opp = opponent_of(memscan.find_match_decks(pid, vtable, valid))
            if opp and opp["addr"] != shown:
                shown = opp["addr"]
                print(f"\n=== opponent {time.strftime('%H:%M:%S')} ===")
                tag = "  [OPPONENT]" if local else ""
                _render_deckinfo(opp["di"], index,
                                 {"playerName": opp["playerName"] + tag,
                                  "deckSize": opp["deckSize"], "elo": opp.get("elo")})
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped")


def cmd_watch(args):
    try:
        watch.run(interval=args.interval, raw=args.raw)
    except KeyboardInterrupt:
        print("\nstopped")


def main(argv=None):
    p = argparse.ArgumentParser(prog="ptcgl", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="verify paths and show live connections").set_defaults(fn=cmd_check)

    c = sub.add_parser("cards", help="build/inspect the card index")
    c.add_argument("--refresh", action="store_true", help="rebuild the cached index")
    c.add_argument("lookup", nargs="*", help="card ids to look up, e.g. sv6_130")
    c.set_defaults(fn=cmd_cards)

    for name, fn, helptext in (
        ("mydecks", cmd_mydecks, "your decks from local client state"),
        ("aidecks", cmd_aidecks, "decklists AI opponents play"),
    ):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("--name", help="filter by deck name")
        s.add_argument("--format", choices=["list", "export", "json"], default="list")
        if name == "aidecks":
            s.add_argument("--list", action="store_true", help="names only")
        s.set_defaults(fn=fn)

    s = sub.add_parser("scan", help="find deck-shaped card clusters in live memory")
    s.add_argument("--top", type=int, default=15, help="clusters to show")
    s.add_argument("--gap", type=int, default=4096, help="bytes that split clusters")
    s.add_argument("--min-distinct", type=int, default=12)
    s.add_argument("--max-distinct", type=int, default=70)
    s.add_argument("--min-sets", type=int, default=3)
    s.add_argument("--verbose", action="store_true", help="list card names per cluster")
    s.add_argument("--count-only", action="store_true",
                   help="report cluster shapes without card identities")
    s.add_argument("--unknown", action="store_true",
                   help="show only clusters that match no known deck")
    s.add_argument("--unknown-below", type=float, default=0.6)
    s.add_argument("--save", help="write cluster fingerprints to a baseline file")
    s.add_argument("--diff", help="show only clusters absent from this baseline")
    s.set_defaults(fn=cmd_scan)

    pr = sub.add_parser("probe", help="check for field-name strings in live memory")
    pr.add_argument("marker", nargs="*", help="strings to look for")
    pr.set_defaults(fn=cmd_probe)

    dk = sub.add_parser("deck", help="recover a full decklist with counts from memory")
    dk.add_argument("--against", help="validate against a known local deck by name")
    dk.add_argument("--gap", type=int, default=4096)
    dk.add_argument("--min-distinct", type=int, default=12)
    dk.add_argument("--known-below", type=float, default=0.6)
    dk.add_argument("--top", type=int, default=1)
    dk.add_argument("--show", action="store_true", help="per-card detail (validation)")
    dk.add_argument("--save-maps", help="dump every deck map to a JSON file and stop")
    dk.set_defaults(fn=cmd_deck)

    op = sub.add_parser("opponent", help="read the opponent's deck + identity live")
    op.add_argument("--top", type=int, default=5, help="max decks to show")
    op.add_argument("--mine", action="store_true", help="also show your own decks")
    op.add_argument("--stats", action="store_true", default=True,
                    help="also read player name/rank (extra scan)")
    op.add_argument("--no-stats", dest="stats", action="store_false",
                    help="skip the player-stats hop (faster)")
    op.add_argument("--refresh", action="store_true",
                    help="ignore the cached DeckInfo class and re-bootstrap")
    op.add_argument("--baseline", action="store_true",
                    help="run at the menu to snapshot your decks; the opponent's "
                         "deck is then whatever's new in a match")
    op.set_defaults(fn=cmd_opponent)

    cap = sub.add_parser("capture", help="one scan; save all card occurrences to a file")
    cap.add_argument("out", help="output JSON file")
    cap.set_defaults(fn=cmd_capture)

    du = sub.add_parser("dump", help="print memory around a marker string")
    du.add_argument("marker")
    du.add_argument("--before", type=int, default=512)
    du.add_argument("--after", type=int, default=8192)
    du.add_argument("--width", type=int, default=1200)
    du.add_argument("--top", type=int, default=8)
    du.set_defaults(fn=cmd_dump)

    aw = sub.add_parser("autowatch",
                        help="keep running; auto-print the opponent's deck each match")
    aw.add_argument("--interval", type=float, default=10.0,
                    help="seconds between checks (default 10)")
    aw.set_defaults(fn=cmd_autowatch)

    w = sub.add_parser("watch", help="follow the running client live")
    w.add_argument("--interval", type=float, default=1.0)
    w.add_argument("--raw", action="store_true", help="do not filter log lines")
    w.set_defaults(fn=cmd_watch)

    args = p.parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
