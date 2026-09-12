"""Card index built from the client's own card databases.

The client caches one table per expansion under config-cache. Together they map
the short ids that appear in deck payloads (``sv6_130``) to printable card data.
"""

import json
import re
import os

from . import paths, tablebin

POKEMON, TRAINER, ENERGY = 1, 2, 3
CATEGORY_NAMES = {POKEMON: "Pokémon", TRAINER: "Trainer", ENERGY: "Energy"}

CACHE = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".card-index.json")

# card ids as they appear in text: sv6_130, me2-5_196, ec_24_ph
CARD_ID = re.compile(r"\b[a-z0-9-]{2,8}_[0-9]{1,3}(?:_[a-z]{2})?\b")


class Card:
    __slots__ = ("id", "name", "number", "set_code", "category", "regulation", "hp", "type")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    @property
    def category_name(self):
        return CATEGORY_NAMES.get(self.category, "Other")

    def export_line(self, count):
        """One line of a PTCGL decklist export."""
        return f"{count} {self.name} {self.set_code} {self.number}"

    def as_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}


def _set_code(card_id):
    """`sv6_130` and `ec_24_ph` -> `SV6` / `EC`.

    Ids are `<set>_<number>` with an optional variant suffix such as `_ph`, so
    the set is everything before the first underscore.
    """
    return card_id.split("_", 1)[0].upper()


def build():
    """Parse every card database into an id -> Card mapping."""
    index = {}
    for path in paths.card_databases():
        table = tablebin.load(path)
        for row in table.rows:
            card_id = row.get("cardID")
            if not card_id or card_id in index:
                continue
            index[card_id] = Card(
                id=card_id,
                name=row.get("EN Card Name") or card_id,
                number=row.get("EN Card #") or "",
                set_code=_set_code(card_id),
                category=row.get("category"),
                regulation=row.get("Regulations symbol") or "",
                hp=row.get("HP"),
                type=row.get("EN Type") or "",
            )
    return index


def load(refresh=False):
    """Card index, memoised to disk so repeated runs stay fast."""
    if not refresh and os.path.exists(CACHE):
        stale = os.path.getmtime(CACHE) < max(
            os.path.getmtime(p) for p in paths.card_databases()
        )
        if not stale:
            with open(CACHE, encoding="utf-8") as fh:
                return {k: Card(**v) for k, v in json.load(fh).items()}

    index = build()
    with open(CACHE, "w", encoding="utf-8") as fh:
        json.dump({k: v.as_dict() for k, v in index.items()}, fh)
    return index
