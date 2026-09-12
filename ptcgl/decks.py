"""Decks the client holds locally: yours, and the ones AI opponents play."""

import json
import os

from . import cards as cards_mod, paths

# config-cache documents that carry playable deck definitions
AI_DECK_CONFIGS = [
    "ai-decks", "ai-decks-expanded", "ftue-decks", "theme-decks", "bp-decks",
    "tt-glc-decks", "me-battle-decks", "sv-battle-decks", "swsh-battle-decks",
    "sm-battle-decks", "xy-battle-decks",
    "battle-academy2020-decks", "battle-academy2022-decks", "battle-academy2024-decks",
]


class Deck:
    def __init__(self, name, counts, source, meta=None):
        self.name = name
        self.counts = counts          # card id -> copies
        self.source = source
        self.meta = meta or {}

    @property
    def size(self):
        return sum(self.counts.values())

    def resolve(self, index):
        """[(Card, count)] sorted the way a decklist reads."""
        out = []
        for card_id, n in self.counts.items():
            card = index.get(card_id)
            if card is None:
                card = cards_mod.Card(
                    id=card_id, name=f"<unknown {card_id}>", number="",
                    set_code=cards_mod._set_code(card_id), category=0,
                    regulation="", hp=None, type="",
                )
            out.append((card, n))
        out.sort(key=lambda p: (p[0].category or 99, p[0].name, p[0].number))
        return out

    def export(self, index):
        """PTCGL decklist export text - the format the game itself imports."""
        groups = {cards_mod.POKEMON: [], cards_mod.TRAINER: [], cards_mod.ENERGY: []}
        other = []
        for card, n in self.resolve(index):
            groups.get(card.category, other).append((card, n))

        lines = []
        for cat in (cards_mod.POKEMON, cards_mod.TRAINER, cards_mod.ENERGY):
            entries = groups[cat]
            if not entries:
                continue
            total = sum(n for _, n in entries)
            lines.append(f"{cards_mod.CATEGORY_NAMES[cat]}: {total}")
            lines.extend(c.export_line(n) for c, n in entries)
            lines.append("")
        if other:
            lines.append(f"Other: {sum(n for _, n in other)}")
            lines.extend(c.export_line(n) for c, n in other)
            lines.append("")
        lines.append(f"Total Cards: {self.size}")
        return "\n".join(lines)


def _content(path):
    """config-cache documents wrap their payload in a JSON string."""
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    for entry in doc.get("keys", {}).values():
        if "contentString" in entry:
            return json.loads(entry["contentString"])
    return None


def my_decks():
    """Decks in the local per-account deck state, newest edit first.

    This is whatever the client last wrote out for the signed-in account, which
    includes the server's win/loss metadata for each deck.
    """
    out = []
    for profile in paths.profile_dirs():
        path = os.path.join(profile, "unsaved-deckinfo.json")
        try:
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        for entry in payload:
            record = entry.get("ServerAuthoritativeMetadata") or {}
            out.append(Deck(
                name=entry.get("deckName") or "(unnamed)",
                counts=entry.get("cards") or {},
                source=os.path.basename(profile),
                meta={
                    "wins": record.get("wins"),
                    "losses": record.get("losses"),
                    "last_played": entry.get("lastPlayed"),
                    "valid_standard": entry.get("valid"),
                    "deck_id": entry.get("id"),
                    "mtime": os.path.getmtime(path),
                },
            ))
    out.sort(key=lambda d: d.meta.get("last_played") or "", reverse=True)
    return out


def ai_decks():
    """Every AI / precon deck definition the client has cached."""
    out = []
    for name in AI_DECK_CONFIGS:
        try:
            payload = _content(paths.config(name))
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, list):
            continue
        for entry in payload:
            if not isinstance(entry, dict) or "cards" not in entry:
                continue
            out.append(Deck(
                name=entry.get("id") or entry.get("name") or "(unnamed)",
                counts=entry["cards"],
                source=name,
                meta={"featured": entry.get("featuredCardId"),
                      "released": entry.get("releaseDate")},
            ))
    return out
