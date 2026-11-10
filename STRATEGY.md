# Strategy — werewolf-agent-01

## The game

8 players, seats 1–8. Roles: 2 werewolves, 1 seer, 1 doctor, 4 villagers.
Night 1 comes first. Each night the wolves vote a kill (it lands only if
every naming wolf agreed), the doctor protects one living seat other than
themself and other than the previous night's target, and the seer inspects
one living seat other than themself. Each day has a debate (2 round-robin
cycles, 1–500 char statements or passes) then a public vote; the unique
plurality is exiled and their role revealed. Village wins when no wolf is
alive; wolves win when living wolves ≥ living non-wolves; undecided at the
end of day 10 is a draw. A trial is 30 games: 15 as wolf, 5 seer, 5 doctor,
5 villager.

## Design choice

No API key was available, so the agent is **purely rule-based**: Python 3.13
stdlib only, no model calls. The strategy layer is clean (a `Strategy`
class fed by an event stream rebuilt from the game log), so an LLM hook
could be added later, but the entry works fully without one.

## Belief model

Every living player carries a suspicion score (0–1, starts 0.5). It is
updated from every public event, replayed from the full game log on each
decision so the model is always consistent with what has been seen:

- **Exile flip:** voting to exile a player who flipped werewolf is
  clearing (−suspicion for the voter); voting to exile a flipped
  villager is suspicious (+). A voter who breaks from a majority that
  later proves wrong gets credit; a voter who follows a wrong majority
  gets suspicion.
- **Defenses:** defending a player who later flips werewolf is suspicious
  (+0.18). Defending a flipped villager is mildly clearing.
- **Accusations:** accusing a player who later flips werewolf is clearing.
- **Claims:** a seer claim is recorded with the target it names. A second
  seer claim must be fake (there is exactly one seer). A claim that names
  the agent or its partner is a threat.
- **Night kills:** the victim's accusers gain suspicion (wolves kill the
  players who threaten them); a player who was pushing the victim is
  treated as more dangerous by the wolves, so their suspicion of others
  is weighted accordingly.
- **Known sets:** a flipped role, a seer inspection result, and a claim
  that checks out move a player into `known_wolf` / `known_village`,
  which override the score.

## Per-role play

- **Werewolf.** Night: kill the most dangerous villager (the seer
  claimant first, then the most trusted / most accurate voter); match
  the partner's night vote when it is visible. Day: blend in — agree
  with the room, accuse the most suspicious villager with cited evidence,
  defend the partner only lightly. Vote: if a seer claimant named the
  agent or its partner, vote the claimant; if the partner (or the agent)
  leads the day tally, vote the runner-up to force a tie; otherwise pile
  onto the leader. Bus the partner only if partner suspicion is very
  high (> 0.85).
- **Seer.** Night: inspect the most suspicious living player. On finding
  a wolf, claim immediately ("I am the seer, X is a werewolf") and push
  that target every day. Vote a known wolf first; otherwise vote a rival
  seer claimant (a second claim must be fake).
- **Doctor.** Night: protect the seer claimant if there is one, else the
  most trusted villager; never self, never the previous night's target.
  Day: follow the earliest seer claim.
- **Villager.** Follow the earliest seer claim (a later claim is usually
  a counter-claim) if its target is alive, votable, and not known-village
  to the agent; otherwise vote the most suspicious player. Statements
  cite the evidence: who voted to exile whom and what they flipped as,
  who defended a flipped wolf, who claimed what.

## Statements

Every statement is built from real named events in the log, e.g.
"Ana voted to exile Ben, who flipped villager" or "Chen defended Dana
before they flipped werewolf". Statements are 1–500 chars, varied by
round and by which evidence is strongest, and never repeat the same
sentence twice in a game. The agent passes only when it has nothing
new to add.

## v1 → v2 changes

v1 (mean 567) already had claim-following votes and flip-based suspicion.
v2 (mean 622) added two village-side signals found by analysing the
v1 loss transcripts, plus evidence-rich statements:

1. Majority-break credit: a voter who breaks from a majority that later
   proves wrong is credited; following a wrong majority is suspicious.
2. Defense tracking: defending a flipped wolf is now suspicious
   (+0.18); defenses are also cited in statements.
3. Statements now name the specific vote/flip/defense/claim behind the
   accusation instead of stating a bare suspicion.

## Known limits

- Practice scores are **self-play against a stub house** that plays the
  same heuristics — they measure internal consistency and protocol
  correctness, not performance against the real claude-haiku-5-5 house
  players, who will produce messier, more human-like statements and
  votes. The real score will differ.
- The agent cannot read tone or hesitation; it only sees the structured
  log (votes, flips, claims, kills). Against LLM opponents, a statement
  that contradicts a player's earlier statement is a signal this agent
  does not model.
- Wolf play is strong in self-play (wolf 600–733) but the village side
  is swingy across instances (village 467–800), driven by whether the
  seer claim lands early and whether the doctor's protects hit.
- The baseline to beat is claude-opus-5-5 asked plainly — a frontier
  model reasoning in full context. A rule-based agent is at a real
  disadvantage; the honest expectation is a respectable mid-pack score,
  not a guaranteed win.
