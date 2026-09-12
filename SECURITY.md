# Security write-up: opponent decklist disclosure in Pokémon TCG Live

> Practice / educational disclosure report. Findings were produced against the
> author's own client and account. No other players were targeted.

| | |
| --- | --- |
| **Title** | Opponent's full decklist is transmitted to the client and recoverable in-match |
| **Component** | Pokémon TCG Live (desktop, Unity/Mono), client build `1.42.0.1209999` |
| **Class** | Information disclosure / excessive data exposure (CWE-200, CWE-213) |
| **Severity** | High (competitive-integrity impact; server-side root cause) |
| **Attacker requirement** | A running client the attacker controls; a normal match |
| **Fix location** | Server-side (do not send the field); no client fix is sufficient |

---

## Summary

During any match, the client receives its **opponent's complete 60-card
decklist** — every card and quantity — as part of the match-setup data. The
client does not display it, but the data is present in client memory for the
duration of the match and can be read out with a read-only tool. A player can
therefore see exactly what their opponent is running, including cards not yet
played, from the first turn. Hidden information is core to the game's fairness,
so this is a competitive-integrity break.

This is a **server-side over-sharing** issue: the server sends data the client
has no legitimate need for. No amount of client-side hardening (obfuscation,
anti-tamper) fixes it, because the sensitive data is already on the attacker's
machine.

## Affected data flow

Reverse engineering the client assemblies (`ilspycmd`) shows the match-setup
message carries a `PlayerDetails` object per player:

```
PlayerDetails
├─ playerId, playerName            (identity)
├─ deckInfo : DeckInfo             <-- the problem
│    ├─ cards : Dictionary<string,int>   // full cardId -> count, all 60
│    ├─ deckName
│    └─ deckBox / coin / sleeve          (cosmetics)
└─ deckSize
```

Two observations from the code:

1. The gameplay board is *correctly* built from only `deckSize` — the opponent's
   deck is instantiated as N face-down "PRIVATE" placeholder cards
   (`SetCountOfUndefinedCardsInPlayerDecks`). So the **engine** treats the
   opponent's cards as hidden, as intended.
2. But the received `PlayerDetails.deckInfo` — a separate object — is fully
   populated with the real `cards` dictionary and retained (`opPlayerInfo.details
   .deckInfo`). The client keeps the answer key next to the face-down deck.

The intent appears to be an opt-in "share your decklist" feature (the pipe and
UI hooks exist), but in the observed build the opponent's `deckInfo.cards` is
populated regardless, and simply not rendered.

## Proof of concept

Read-only; opens the client process with `PROCESS_VM_READ` and never writes.

1. Enter a match and reach the board.
2. Run the tool: `ptcgl opponent`.
3. It locates the opponent's `DeckInfo` object in memory (via the class vtable,
   then the owning `PlayerDetails`) and prints the full decklist with counts.

Verified: the recovered list matched the opponent account's actual deck exactly
(60/60 cards, correct quantities), available before any of those cards were
played. See the repo's [HOW-IT-WORKS.md](HOW-IT-WORKS.md) for the technique.

## Impact

- **Competitive integrity:** perfect information about the opponent's deck from
  turn 1 — matchup, tech cards, counts, win conditions — in a game built on
  hidden information. This advantage is undetectable to the victim and the
  server.
- **Scope, per match:** the opponent's single active deck.
- **Secondary — data retention:** the client does not promptly free a past
  opponent's `DeckInfo`. Over a session, the decks of *every* opponent faced
  accumulate in memory, so a single capture late in a session can expose many
  players' decklists, not just the current one.

## Root cause

The server includes the opponent's `deckInfo.cards` in match-setup data sent to
each client. The client needs only `deckSize` to render a hidden deck (and it
does), making the `cards` payload unnecessary and sensitive.

## Remediation

**Primary (server-side, required):** do not send an opponent's card list to a
client that isn't entitled to it.

- Strip `cards` (and any non-cosmetic deck detail) from the opponent's
  `PlayerDetails` before transmission — send a `DeckBrief` (name + cosmetics
  only), which the codebase already defines, instead of a full `DeckInfo`.
- Send only `deckSize` for the opponent, which is all the board build uses.
- If the "share decklist" feature is intended, gate the `cards` payload behind
  the recipient having opted in / both players consenting, enforced server-side.

**Secondary (client-side, defense in depth):**

- Don't retain `opPlayerInfo.details.deckInfo.cards`; discard opponent deck
  detail once the board is built, and free it at match end so past opponents
  don't linger in memory.

## Notes

Client-side mitigations alone are insufficient: once the data reaches the
attacker's process it is readable regardless of obfuscation. The fix must be to
not send it.
