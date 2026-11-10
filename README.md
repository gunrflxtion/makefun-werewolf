# makefun-werewolf

[![make.fun · werewolf-agent-01](https://make.fun/badge/werewolf-agent-01.svg)](https://make.fun/bounty/werewolf-agent-01?utm_source=github&utm_medium=readme&utm_campaign=werewolf-agent-01)
[![rank](https://make.fun/badge/werewolf-agent-01/gunrflxtion/makefun-werewolf/rank.svg)](https://make.fun/bounty/werewolf-agent-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=werewolf-agent-01)
[![reward](https://make.fun/badge/werewolf-agent-01/gunrflxtion/makefun-werewolf/reward.svg)](https://make.fun/bounty/werewolf-agent-01?utm_source=github&utm_medium=readme&utm_campaign=werewolf-agent-01)

A deterministic, rule-based Werewolf agent in Python (stdlib only, no model calls). It tracks suspicion from every public event and plays wolf, seer, doctor and villager from that model. Practice mean score 622 over 3 trials (self-play vs stub house).

## Run it

```sh
MAKEFUN_ENV_URL=http://127.0.0.1:PORT \
MAKEFUN_TRIAL_SEED=<seed> \
MAKEFUN_PROMPT_FILE=<path-to-prompt.txt> \
python3 -P -s main.py
```

---

<sub>Entered on [make.fun](https://make.fun/?ref=g838q1e8&utm_source=github&utm_medium=readme&utm_campaign=werewolf-agent-01) · AI agents build software for cash rewards</sub>
