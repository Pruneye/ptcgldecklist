# How it works (plain-language walkthrough)

This explains, start to finish, how `ptcgl` reads a Pokémon TCG Live match — and
what every file in the project does. No security jargon; if you can read a
recipe, you can follow this.

---

## The one-sentence version

The game already has the opponent's whole deck sitting in your computer's memory
during a match, so instead of hacking anything, we just **read it back out**.

---

## Background: two places the game keeps data

**1. Files on disk.** When you launch the game it downloads a big pile of data
and caches it in a folder on your PC (the card database, every AI opponent's
deck, art, etc.). Your own saved decks are written there too. These are just
files we can open.

**2. Live memory (RAM).** While the game runs, everything it's currently doing —
the match, both players, the cards — lives in memory as **objects**. An "object"
is just a bundle of related values sitting together at some address in RAM. A
running program is millions of these.

`ptcgl` reads from both. The files give us the "dictionary" (what every card id
means). Memory gives us the live match.

---

## Part 1 — Turning card ids into card names (the files)

Everywhere in the game, cards are referred to by short ids like `sv6_130`, not
"Dragapult ex". To translate, we need the card database.

The game stores it in `config-cache\card-database-*.json`. Inside each file is a
blob of packed binary (a ".NET BinaryWriter table" — think of a spreadsheet
squashed into bytes: a header listing the columns, then row after row of values).

- **`ptcgl/tablebin.py`** unpacks that binary format back into rows. The one
  gotcha: each cell starts with a flag byte where `1` means "this cell is empty,
  skip it" — miss that and every row after the first empty cell is garbage.
- **`ptcgl/cards.py`** runs that over all ~223 database files, builds a lookup of
  `sv6_130 → Dragapult ex (SV6 130, Pokémon)` for all ~25,000 cards, and caches
  it to `.card-index.json` so it only has to do this once.

- **`ptcgl/paths.py`** just knows *where* all these files live on Windows.
- **`ptcgl/decks.py`** reads two more things from disk: your own saved decks
  (`unsaved-deckinfo.json`) and the 283 built-in AI decklists.

At this point we can already print your decks and the AI decks. The hard part is
the opponent.

---

## Part 2 — Reading the live match (memory)

### Why we can't just "search for the deck"

First instinct: scan memory for card ids and find the opponent's deck. That
fails, because **the entire card database is loaded in memory** (~24,700 of
25,000 ids are present just sitting at the menu). Searching for a card is like
searching for a word in the dictionary — it's always there. Presence means
nothing.

Worse: **your entire saved-deck library is also in memory** (the deck manager
loads all of them), each a complete 60-card list. So even "find things shaped
like a deck" returns 20+ decks that all look identical in kind. We can't tell
the opponent's from your own by *content*.

### The insight: objects have a type-tag, and the match links players to decks

Two facts about how the game's engine (Mono/.NET) lays out objects made this
solvable:

1. **Every object starts with a "type pointer" (a vtable).** All objects of the
   same class share the exact same one. So a specific 8-byte value at the start
   of an object is effectively a label saying "I am a DeckInfo." Find that value,
   and you've found every deck object in memory at once.

2. **A deck object (`DeckInfo`) is a fixed bundle of fields**: a dictionary of
   `cardId → count` (the actual list), the deck name, and cosmetics (box/coin/
   sleeve). Once you know the layout, you read those fields straight out.

3. **The two decks *in the match* are special**: each is pointed at by a
   `PlayerDetails` object that also holds a player *name*, a player id, and the
   deck size. Your other 20 library decks are only referenced by menu/UI stuff.
   So "which deck is a real match deck?" = "which decks does a PlayerDetails
   point at?" That cleanly separates the 2 match decks from your whole library.

### The actual steps `ptcgl opponent` runs

1. **Find the DeckInfo type.** Look up one of *your* deck names (we know those
   from disk) as text in memory, follow the pointer to it, and read the type-tag
   of the object it belongs to. That tag is the `DeckInfo` class. (Cached for the
   rest of the session so we only do it once.)
2. **Find every deck.** Scan memory for that type-tag → every `DeckInfo` object.
3. **Read each one.** Pull its name, cosmetics, and card dictionary (cardId →
   count), translating ids to names with the Part 1 index.
4. **Keep only the match decks.** Scan for objects that *point at* those decks
   and look like a `PlayerDetails` (have a name + id + deck size). Those are the
   two players. Everything else is your library, discarded.
5. **Print** both players and their full decklists.

- **`ptcgl/memscan.py`** does all the memory work: opening the process
  read-only, walking regions, reading objects and pointers, parsing the card
  dictionary, and the player/deck linking. It never writes to the game.

### Speed tricks

A full memory sweep is ~2 GB and takes ~50 s, so:
- We **read the big memory regions first** (objects live there) and **stop early**
  the moment we've found what we need.
- We **cache the type-tag** for the session, so repeat runs skip the lookup.

A run lands in about a minute. Since the deck stays in memory for the whole
match, there's no rush to catch a specific moment.

---

## Part 3 — The commands

- **`ptcgl/cli.py`** ties it together into subcommands (`check`, `cards`,
  `mydecks`, `aidecks`, `opponent`, `autowatch`, …) and formats the output.
- **`ptcgl/watch.py`** is a live follower of the client's log files and network
  connections (used by the `watch` command).

`autowatch` just runs the `opponent` scan on a loop and prints whenever a new
match appears.

---

## The punchline

Nothing here breaks into the game. Every step reads data the client already
fetched and holds. The reason the opponent's deck is gettable at all is that the
**server sends it to your client** — the client just doesn't show it to you. That
part is the security finding; see [SECURITY.md](SECURITY.md).
