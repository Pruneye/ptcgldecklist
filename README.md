# ptcgl

A command-line tool that reads decklists and live match data from a local
**Pokémon TCG Live** client on Windows. Read-only: it parses the files the client
already writes and inspects its process memory. It never modifies the game,
injects code, or touches the network session.

Built as a security-research / debugging exercise. See
[SECURITY.md](SECURITY.md) for the vulnerability write-up (the opponent's full
decklist is recoverable from the client during a match).

## Install

Python 3.9+, no dependencies.

```
git clone <your-repo-url>
cd ptcgldecklist
pip install -e .        # or: pipx install .   (for a global `ptcgl` command)
```

## Usage

```
ptcgl check                 # verify the client is found; show live connections
ptcgl cards sv6_130         # look up card ids in the client's own database
ptcgl mydecks               # your decks, from the client's local files
ptcgl mydecks --format export
ptcgl aidecks --list        # the 283 AI/precon decklists the client ships
ptcgl opponent              # in a match: print both players' full decks
ptcgl autowatch             # leave running; auto-prints the opponent each match
```

### `ptcgl opponent`

Run it while you're on the board in a match. It prints each player's deck —
name, cosmetics, deck size, and the full 60-card list with exact counts, grouped
by Pokémon / Trainer / Energy:

```
match has 2 player(s)

──────────────────────────────────────────
  PLAYER    Pruneye
  deck size 60
  DECK      Flareon
  box       db_ghostlygathering_pcen
  SIZE      60 cards, 35 distinct
──────────────────────────────────────────
  POKÉMON: 23
    3  Hoothoot SV7 114
    3  Noctowl SVBSP 141
    ...
```

Your own deck is shown too; the opponent is the one that isn't your account.
A run takes ~1 minute (it scans process memory). The data lives in memory for
the whole match, so there's no need to catch it at any particular moment.

### `ptcgl autowatch`

Start it once and just play. It prints the opponent's deck automatically each
time a **new** match begins, and stays quiet otherwise (it dedupes on the two
players, so it won't re-print the same match). `Ctrl+C` to stop.

```
ptcgl autowatch --interval 10
```

Note: each check is a full memory scan (~1 min), and `--interval` is the pause
*between* scans - so it scans near-continuously, including at the menu. Launch
it around when you're actually playing.

## Docs

- [HOW-IT-WORKS.md](HOW-IT-WORKS.md) - plain-language walkthrough of the whole
  method and every file.
- [SECURITY.md](SECURITY.md) - the vulnerability write-up (opponent decklist
  disclosure), in disclosure format.

## How it works

Everything below was established by reverse-engineering the client (build
`1.42.0`, Unity 6000.3, Mono) with `ilspycmd` plus read-only memory inspection.

- **Local files** (`%USERPROFILE%\AppData\LocalLow\pokemon\Pokemon TCG Live\`):
  the card database and all AI decklists are cached under `config-cache\` as
  `.NET BinaryWriter` column tables; your own decks are in
  `<account>\unsaved-deckinfo.json`. [`ptcgl/tablebin.py`](ptcgl/tablebin.py)
  parses the tables; [`ptcgl/cards.py`](ptcgl/cards.py) builds a 25k-card index.

- **Live memory** ([`ptcgl/memscan.py`](ptcgl/memscan.py)): the client keeps
  match state as live Mono objects. A `DeckInfo` object holds
  `cards` (a `Dictionary<string,int>` of cardId→count), `deckName`, and
  cosmetics. The two decks *in the match* are the ones a `PlayerDetails` object
  (player name + id + deck size) points at; the rest are your resident deck
  library. The tool finds the `DeckInfo` class by its shared vtable, reads each
  object's fields directly, and walks to the owning `PlayerDetails` for the
  player name.

## Files

| File | What it does |
| --- | --- |
| `ptcgl/cli.py` | command-line interface; all `ptcgl <command>` subcommands |
| `ptcgl/paths.py` | locates the client's files on Windows |
| `ptcgl/tablebin.py` | parses the `.NET BinaryWriter` card-database tables |
| `ptcgl/cards.py` | builds/caches the card index (id → name/set/type) |
| `ptcgl/decks.py` | reads your local decks and the AI decklists |
| `ptcgl/memscan.py` | read-only process-memory inspection and object walking |
| `ptcgl/watch.py` | follows the client's logs/sockets live |

## Scope / ethics

This reads a local client you are running, for research and debugging. The
central finding — that a client receives its opponent's full decklist — is a
server-side information-disclosure issue that should be reported to the vendor,
not exploited. Do not use it to gain an advantage against other players.
