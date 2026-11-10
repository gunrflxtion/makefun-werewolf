#!/usr/bin/env python3
"""Deterministic, rule-based Werewolf agent for the make.fun environment."""

import hashlib
import json
import os
import random
import re
import sys
import time
import unicodedata
import urllib.request

ENV_URL = os.environ.get("MAKEFUN_ENV_URL", "")
TRIAL_SEED = os.environ.get("MAKEFUN_TRIAL_SEED", "local")
PROMPT_FILE = os.environ.get("MAKEFUN_PROMPT_FILE", "")
MAX_OBSERVE_ERRORS = 60
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Env:
    """The sole HTTP adapter for POST /observe and POST /act."""

    def __init__(self, base_url):
        self.base = base_url.rstrip("/")

    def _post(self, path, payload, timeout=90):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=data, method="POST",
            headers={"Content-Type": "application/json"})
        with _OPENER.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def observe(self):
        return self._post("/observe", {})

    def act(self, body):
        return self._post("/act", body, timeout=30)


def stable_seed(*parts):
    raw = "|".join(str(p) for p in parts).encode("utf-8")
    return int(hashlib.sha256(raw).hexdigest()[:16], 16)


def clamp(x, lo=0.0, hi=1.0):
    return lo if x < lo else hi if x > hi else x


def normalize_role(role):
    return "werewolf" if role == "wolf" else (role or "villager")


def fold_statement(text):
    text = re.sub(r"[\t\n\v\f\r\u0085\u2028\u2029]+", " ", text)
    return "".join(c for c in text if unicodedata.category(c) != "Cc").strip()


def cut_statement(text, max_chars):
    chars = list(text)
    return text if len(chars) <= max_chars else "".join(chars[:max_chars]).strip()


class Beliefs:
    PRIOR = 2.0 / 7.0

    def __init__(self, seats, me, rng=None):
        self.me = me
        self.rng = rng
        self.score = {s: self.PRIOR for s in seats if s != me}
        self.known_wolf = set()
        self.known_village = set()

    def alive_scores(self, alive):
        return {s: self.score.get(s, self.PRIOR)
                for s in alive if s != self.me}

    def _pick_tied(self, cands, best):
        tied = sorted(s for s, value in cands.items() if value == best)
        return self.rng.choice(tied) if self.rng and len(tied) > 1 else (
            tied[0] if tied else None)

    def most_suspicious(self, alive):
        cands = self.alive_scores(alive)
        return self._pick_tied(cands, max(cands.values())) if cands else None

    def least_suspicious(self, alive):
        cands = self.alive_scores(alive)
        return self._pick_tied(cands, min(cands.values())) if cands else None

    def bump(self, seat, delta):
        if seat == self.me or seat in self.known_wolf or seat in self.known_village:
            return
        self.score[seat] = clamp(self.score.get(seat, self.PRIOR) + delta)

    def set_wolf(self, seat):
        if seat != self.me:
            self.known_wolf.add(seat)
            self.known_village.discard(seat)
            self.score[seat] = 1.0

    def set_village(self, seat):
        if seat != self.me:
            self.known_village.add(seat)
            self.known_wolf.discard(seat)
            self.score[seat] = 0.0

    def on_flip(self, seat, team):
        if team == "wolf":
            self.set_wolf(seat)
        else:
            self.set_village(seat)

    def on_vote(self, voter, target, flipped):
        # Voting for a flipped wolf is clearing; voting for a villager is a
        # weak signal, strengthened when it joined a decisive pile-on.
        if flipped == "wolf":
            self.bump(voter, -0.20)
        elif flipped == "village":
            self.bump(voter, +0.15)

    def on_defense(self, defender, defended, flipped):
        if defender == defended:
            return
        if flipped == "wolf":
            self.bump(defender, +0.18)

    def on_accusation(self, accuser, accused, flipped):
        if accuser != accused and flipped == "wolf":
            self.bump(accuser, -0.15)

    def on_night_kill(self, victim, accusers):
        # A kill may silence an accurate accuser; treat this as a modest clue.
        for target in accusers:
            self.bump(target, +0.10)


ACCUSE_WORDS = ("wolf", "werewolf", "sus", "suspicious", "vote",
                "lynch", "exile", "kill", "trust", "protect")
DEFENSE_WORDS = ("trust", "defend", "innocent", "clear", "not a wolf",
                 "not wolf", "should not vote", "don't vote", "do not vote",
                 "leave", "protect")


class Strategy:
    """Per-game role strategy; all persistent evidence is rebuilt from view."""

    def __init__(self, game_id, me, role, seats, trial_seed="local"):
        self.game_id = game_id
        self.me = me
        self.role = normalize_role(role)
        self.seats = list(seats)
        self.rng = random.Random(stable_seed(trial_seed, game_id, me))
        self.beliefs = Beliefs(self.seats, me, self.rng)
        self.partner = None
        self.labels = {}
        self.alive = set(self.seats)
        self.day = 1
        self.phase = "night"
        self.seer_results = {}
        self.claims = {}
        self.claim_targets = {}
        self.last_protect = None
        self.partner_pick = None
        self.votes_today = {}
        self.accused_wolf = {}
        self.accusations = {}
        self.defenses = {}
        self.claimed_seer = False
        self.my_last_statement = ""
        self._history_cache = []
        self._flips = {}

    def sync(self, view):
        self.day = view.get("day", self.day)
        self.phase = view.get("phase", self.phase)
        you = view.get("you") or {}
        self.me = you.get("seat", self.me)
        self.role = normalize_role(you.get("role", self.role))
        partner = you.get("partner")
        self.partner = partner.get("seat") if partner else None
        players = view.get("players") or []
        if players:
            self.labels = {p["seat"]: p["name"] for p in players
                           if "seat" in p and "name" in p}
            self.alive = {p["seat"] for p in players if p.get("alive")}
        for result in view.get("seerResults") or []:
            self.seer_results[result["seat"]] = (
                "wolf" if result.get("werewolf") else "village")
        protect = view.get("lastProtect")
        if protect:
            self.last_protect = protect.get("seat")
        self.partner_pick = None
        for vote in view.get("wolfVotes") or []:
            if vote.get("night") == self.day and vote.get("seat") == self.partner:
                self.partner_pick = vote.get("target")
        self._rebuild_from_log(view.get("log") or [])

    def _team_of_role(self, role):
        return "wolf" if role == "werewolf" else "village"

    def _statement_target(self, text, speaker):
        low = text.lower()
        matches = []
        for seat, name in self.labels.items():
            if seat == speaker or not name:
                continue
            match = re.search(r"\b" + re.escape(name.lower()) + r"\b", low)
            if match:
                matches.append((match.start(), seat))
        return min(matches)[1] if matches else None

    def _log_to_events(self, log):
        events = []
        for item in log:
            kind = item.get("kind")
            day = item.get("day", self.day)
            if kind == "speech":
                text = item.get("text") or ""
                seat = item.get("seat")
                target = self._statement_target(text, seat)
                events.append({"type": "statement", "seat": seat,
                               "text": text, "day": day, "target": target})
                low = text.lower()
                if (("i am the seer" in low or "i'm the seer" in low
                     or "i am seer" in low) and target is not None):
                    events.append({"type": "claim", "seat": seat,
                                   "role": "seer", "target": target, "day": day})
            elif kind == "vote":
                events.append({"type": "vote", "seat": item.get("seat"),
                               "target": item.get("target"), "day": day})
            elif kind in ("exile", "dawn"):
                seat = item.get("seat") if kind == "exile" else item.get("died")
                role = item.get("role")
                if seat is not None:
                    events.append({"type": "flip", "seat": seat,
                                   "team": self._team_of_role(role),
                                   "role": role, "day": day,
                                   "tally": item.get("tally")})
                    if kind == "dawn":
                        events.append({"type": "night_kill", "seat": seat,
                                       "day": day})
        return events

    @staticmethod
    def _flip_of(history, seat):
        for event in history:
            if event.get("type") == "flip" and event.get("seat") == seat:
                return event.get("team")
        return None

    def _rebuild_from_log(self, log):
        history = self._log_to_events(log)
        self._history_cache = history
        self.beliefs = Beliefs(self.seats, self.me, self.rng)
        if self.role == "werewolf" and self.partner is not None:
            self.beliefs.set_wolf(self.partner)
        for seat, team in self.seer_results.items():
            (self.beliefs.set_wolf if team == "wolf"
             else self.beliefs.set_village)(seat)
        self.votes_today = {}
        self.accusations = {}
        self.defenses = {}
        self.accused_wolf = {}
        self.claim_targets = {}
        self.claims = {}
        self._flips = {}
        pending_votes = {}
        for event in history:
            self._apply_event(event, pending_votes)
        for (day, voter), target in pending_votes.items():
            team = self._flips.get(target)
            if team:
                self.beliefs.on_vote(voter, target, team)

    def _apply_event(self, event, pending_votes=None):
        kind = event.get("type")
        seat = event.get("seat")
        if kind == "flip":
            team = event.get("team")
            self._flips[seat] = team
            self.beliefs.on_flip(seat, team)
            if event.get("role") is not None:
                self.alive.discard(seat)
            tally = event.get("tally") or []
            if team == "village" and tally:
                top = max((row.get("votes", 0) for row in tally), default=0)
                if top:
                    leaders = {row.get("seat") for row in tally
                               if row.get("votes") == top}
                    for ballot in history_votes_for_day(pending_votes,
                                                        event.get("day")):
                        voter, target = ballot
                        if target == seat and seat in leaders:
                            self.beliefs.bump(voter, +0.20)
                        elif target is not None and target != seat:
                            self.beliefs.bump(voter, -0.08)
            # Apply the accumulated public speech around the flipped player:
            # defenses and attacks become evidence only once a flip is known.
            for speaker, targets in self.defenses.items():
                if seat in targets:
                    self.beliefs.on_defense(speaker, seat, team)
            for speaker, targets in self.accusations.items():
                if seat in targets:
                    self.beliefs.on_accusation(speaker, seat, team)
        elif kind == "night_kill":
            self.alive.discard(seat)
            self.beliefs.on_night_kill(seat, self._accusers_of(seat))
        elif kind == "vote":
            day = event.get("day", self.day)
            if day == self.day:
                self.votes_today[seat] = event.get("target")
            if pending_votes is not None:
                pending_votes[(day, seat)] = event.get("target")
        elif kind == "claim":
            self.claims[seat] = event.get("role")
            if event.get("target") is not None:
                self.claim_targets[seat] = event.get("target")
        elif kind == "statement":
            text = (event.get("text") or "").lower()
            target = event.get("target")
            if target is None or seat is None:
                return
            if any(word in text for word in ACCUSE_WORDS):
                self.accusations.setdefault(seat, set()).add(target)
                if self._flips.get(target) == "wolf":
                    self.accused_wolf[seat] = self.accused_wolf.get(seat, 0) + 1
                    self.beliefs.on_accusation(seat, target, "wolf")
            if any(phrase in text for phrase in DEFENSE_WORDS):
                self.defenses.setdefault(seat, set()).add(target)
                team = self._flips.get(target)
                if team:
                    self.beliefs.on_defense(seat, target, team)

    def _accusers_of(self, victim):
        return sorted(self.accusations.get(victim, set()))

    def label(self, seat):
        return self.labels.get(seat, "seat %s" % seat)

    def alive_others(self):
        return sorted(s for s in self.alive if s != self.me)

    def seer_claimant(self):
        for seat, role in self.claims.items():
            if role == "seer" and seat in self.alive and seat != self.me:
                return seat
        return None

    def danger_rank(self, seat):
        suspicion = self.beliefs.score.get(seat, Beliefs.PRIOR)
        return (1.0 - suspicion
                + min(0.3, 0.15 * self.accused_wolf.get(seat, 0))
                + (0.4 if self.claims.get(seat) == "seer" else 0.0))

    def _tally_today(self):
        tally = {}
        for target in self.votes_today.values():
            if target is not None:
                tally[target] = tally.get(target, 0) + 1
        return sorted(tally.items(), key=lambda pair: (-pair[1], pair[0]))

    def my_speeches_today(self):
        return sum(1 for event in self._history_cache
                   if event.get("type") == "statement"
                   and event.get("seat") == self.me
                   and event.get("day") == self.day)

    def night_action(self, kind, options):
        options = [seat for seat in (options or []) if seat in self.alive]
        if not options:
            return None
        if kind == "kill":
            if self.partner_pick in options:
                return self.partner_pick
            targets = [s for s in options if s != self.partner]
            if not targets:
                return None
            best = max(self.danger_rank(s) for s in targets)
            tied = [s for s in targets if self.danger_rank(s) == best]
            return self.rng.choice(sorted(tied)) if len(tied) > 1 else tied[0]
        if kind == "inspect":
            target = self.beliefs.most_suspicious(options)
            return target if target is not None else options[0]
        if kind == "protect":
            claimant = self.seer_claimant()
            if claimant in options:
                return claimant
            best = max(self.danger_rank(s) for s in options)
            tied = [s for s in options if self.danger_rank(s) == best]
            return self.rng.choice(sorted(tied)) if len(tied) > 1 else tied[0]
        return None

    def vote(self, options):
        options = [seat for seat in (options or []) if seat in self.alive]
        if not options:
            return None
        claimants = [seat for seat in self.claims
                     if self.claims[seat] == "seer" and seat in self.alive
                     and seat != self.me and seat in self.claim_targets]
        if self.role == "werewolf":
            for claimant in claimants:
                if self.claim_targets[claimant] in (self.me, self.partner):
                    if claimant in options:
                        return claimant
            ranked = self._tally_today()
            if ranked:
                leader = ranked[0][0]
                runner_up = ranked[1][0] if len(ranked) > 1 else None
                if leader in (self.me, self.partner):
                    if runner_up in options:
                        return runner_up
                    alternatives = [s for s in options if s != self.partner]
                    return self.beliefs.most_suspicious(alternatives)
                if leader in options:
                    return leader
            alternatives = [s for s in options if s != self.partner]
            target = self.beliefs.most_suspicious(alternatives)
            return target if target is not None else (
                alternatives[0] if alternatives else None)
        if self.role == "seer":
            wolves = sorted(s for s in options
                            if s in self.beliefs.known_wolf)
            if wolves:
                return wolves[0]
            if claimants and claimants[0] in options:
                return claimants[0]
        else:
            for claimant in claimants:
                named = self.claim_targets[claimant]
                if named in options and named not in self.beliefs.known_village:
                    return named
        target = self.beliefs.most_suspicious(options)
        return target if target is not None else options[0]

    def _vote_reason(self, target):
        """Return a concrete public vote/flip fact involving this suspect."""
        for event in reversed(self._history_cache):
            if event.get("type") != "vote" or event.get("seat") != target:
                continue
            voted = event.get("target")
            team = self._flip_of(self._history_cache, voted)
            if team:
                role_word = "werewolf" if team == "wolf" else "villager"
                return ("%s voted to exile %s, who flipped %s" %
                        (self.label(target), self.label(voted), role_word))
        # The suspect publicly defended a player whose role is now known.
        for defended in sorted(self.defenses.get(target, set())):
            team = self._flips.get(defended)
            if team == "wolf":
                return ("%s defended %s before they flipped werewolf" %
                        (self.label(target), self.label(defended)))
        # Or they were the person a known wolf tried to clear.
        for wolf, defended_set in sorted(self.defenses.items()):
            if self._flips.get(wolf) == "wolf" and target in defended_set:
                return ("%s was defended by %s after %s flipped werewolf" %
                        (self.label(target), self.label(wolf), self.label(wolf)))
        # Public seer claims are useful only when the claim itself can be named.
        for claimant, named in self.claim_targets.items():
            if self.claims.get(claimant) == "seer" and named == target:
                return ("%s named %s as a werewolf while claiming seer" %
                        (self.label(claimant), self.label(target)))
        # A kill immediately after a player's accusation is a weaker but real clue.
        for event in reversed(self._history_cache):
            if (event.get("type") == "night_kill"
                    and target in self.accusations.get(event.get("seat"), set())):
                return ("%s was among %s's accusations before that night kill" %
                        (self.label(target), self.label(event.get("seat"))))
        return None

    def _line_about(self, target, round_no=1):
        name = self.label(target)
        reason = self._vote_reason(target)
        if reason:
            forms = [
                "Look at the record: %s. That is why %s is my vote." %
                (reason, name),
                "%s deserves scrutiny because %s." % (name, reason),
                "I am not guessing here: %s. I want answers from %s." %
                (reason, name),
            ]
            lead = "After hearing the table, " if round_no >= 2 else ""
            return lead + self.rng.choice(forms)
        templates = [
            "%s's vote and explanation still do not line up for me." % name,
            "I have not heard a clear reason from %s for that vote." % name,
            "I am watching %s; their read has shifted without new evidence." % name,
            "My current vote is %s. I want them to explain their choice today." % name,
        ]
        return self.rng.choice(templates)

    def statement(self, round_no=1, max_chars=500):
        if self.role == "werewolf":
            text = self._wolf_statement(round_no)
        elif self.role == "seer":
            text = self._seer_statement(round_no)
        else:
            text = self._village_statement(round_no)
        if text == self.my_last_statement:
            text = self._fallback_statement()
        self.my_last_statement = text
        return cut_statement(fold_statement(text), max_chars)

    def _wolf_statement(self, round_no):
        alive = self.alive_others()
        if not alive:
            return self._fallback_statement()
        if self.day == 1 and not self._flips:
            return self.rng.choice([
                "No flips yet, so I am tracking who commits to a case and who follows it.",
                "Let's compare today's votes with the reasons people give before settling on anyone.",
                "There is little hard evidence yet. I want to hear a specific case before I choose.",
            ])
        targets = [s for s in alive if s != self.me]
        target = self.vote(targets)
        if target is None:
            return self._fallback_statement()
        if target == self.partner:
            return "%s's votes deserve a direct explanation; I will not clear anyone automatically." % self.label(target)
        return self._line_about(target, round_no)

    def _seer_statement(self, round_no):
        wolves = sorted(s for s in self.alive_others()
                        if s in self.beliefs.known_wolf)
        if wolves:
            target = wolves[0]
            self.claimed_seer = True
            result = next((r for r in reversed(self._history_cache)
                           if r.get("type") == "flip" and r.get("seat") == target),
                          None)
            if result:
                return "I am the seer. I inspected %s and confirmed they are a werewolf; vote %s." % (
                    self.label(target), self.label(target))
            return self.rng.choice([
                "I am the seer: my inspection found %s to be a werewolf. Vote %s today." %
                (self.label(target), self.label(target)),
                "I checked %s last night as seer; the result was werewolf. We should exile %s." %
                (self.label(target), self.label(target)),
            ])
        return self._village_statement(round_no)

    def _village_statement(self, round_no):
        alive = self.alive_others()
        if not alive:
            return self._fallback_statement()
        if self.day == 1 and not self._flips and not self.claim_targets:
            return self.rng.choice([
                "No one has flipped yet. I am listening for reasons tied to actual votes.",
                "We have little evidence today; please say who you are voting for and why.",
                "Let's keep track of who starts a case and who joins it once votes appear.",
            ])
        claimant = self.seer_claimant()
        if claimant is not None:
            target = self.claim_targets.get(claimant)
            if target is not None:
                return "%s claims seer and named %s. We should test that claim against the public votes." % (
                    self.label(claimant), self.label(target))
        target = self.vote([s for s in alive if s != self.me])
        if target is not None:
            return self._line_about(target, round_no)
        return self._fallback_statement()

    def _fallback_statement(self):
        return self.rng.choice([
            "I am weighing the votes and the confirmed flips before deciding.",
            "I do not have a strong read yet; I am listening for concrete reasons.",
            "Let's avoid a rushed vote and compare each accusation with the record.",
        ])


def history_votes_for_day(pending_votes, day):
    if pending_votes is None:
        return []
    return [(voter, target) for (vote_day, voter), target in pending_votes.items()
            if vote_day == day]


def fallback_body(game, decision):
    if decision == "speak":
        return {"game": game, "decision": "speak", "pass": True}
    return {"game": game, "decision": decision, "target": None}


def decide(strat, pending):
    kind = pending.get("decision")
    options = pending.get("options") or []
    if kind == "speak":
        text = strat.statement(strat.my_speeches_today() + 1,
                               pending.get("max_chars") or 500)
        return {"text": text} if text else {"pass": True}
    if kind == "vote":
        return {"target": strat.vote(options)}
    if kind in ("kill", "protect", "inspect"):
        return {"target": strat.night_action(kind, options)}
    return None


def run_trial(env, trial_seed):
    strategies = {}
    started = set()
    errors = 0
    decisions = 0
    while True:
        try:
            obs = env.observe()
        except Exception as err:  # env may transiently be unavailable
            errors += 1
            print("werewolf: /observe failed (%d): %s" % (errors, err),
                  file=sys.stderr)
            if errors > MAX_OBSERVE_ERRORS:
                print("werewolf: giving up on the env", file=sys.stderr)
                return 1
            time.sleep(1)
            continue
        errors = 0
        if not isinstance(obs, dict):
            continue
        if obs.get("done"):
            print("werewolf: trial done (%d decisions answered)" % decisions,
                  file=sys.stderr)
            return 0
        for pending in obs.get("pending", []) or []:
            game = pending.get("game")
            kind = pending.get("decision")
            view = pending.get("view") or {}
            key = "%s:%s:%s" % (game, kind, len(view.get("log") or []))
            if key in started:
                continue
            started.add(key)
            you = view.get("you") or {}
            seat = you.get("seat")
            role = you.get("role")
            players = view.get("players") or []
            seats = [p["seat"] for p in players if "seat" in p] or list(range(1, 9))
            if seat is None or role is None:
                continue
            strat = strategies.get(game)
            if strat is None:
                strat = Strategy(game, seat, role, seats, trial_seed)
                strategies[game] = strat
            strat.sync(view)
            body = decide(strat, pending)
            if body is None:
                continue
            body.update({"game": game, "decision": kind})
            try:
                env.act(body)
                decisions += 1
            except Exception as err:  # retry the decision once as an abstention
                print("werewolf: /act failed game %s %s: %s" %
                      (game, kind, err), file=sys.stderr)
                try:
                    env.act(fallback_body(game, kind))
                    decisions += 1
                except Exception as err2:
                    print("werewolf: fallback /act failed: %s" % err2,
                          file=sys.stderr)


def main(argv):
    if not ENV_URL:
        print("werewolf: MAKEFUN_ENV_URL is not set", file=sys.stderr)
        return 2
    if PROMPT_FILE:
        try:
            with open(PROMPT_FILE, "r", encoding="utf-8") as stream:
                stream.read(1)
        except OSError as err:
            print("werewolf: cannot read MAKEFUN_PROMPT_FILE: %s" % err,
                  file=sys.stderr)
            return 2
    try:
        return run_trial(Env(ENV_URL), TRIAL_SEED)
    except KeyboardInterrupt:
        print("werewolf: interrupted", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

