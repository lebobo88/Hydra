---
id: loot-box-jurisdiction@1
bare_id: loot-box-jurisdiction
kind: compliance
version: 1
title: "Randomized monetization is legal per shipping region"
owner_head: economy-designer
co_owner: compliance-cert
hitl_required: true
---
# Loot-box & randomized-reward jurisdiction rubric

The Economist designs it; The Arbiter signs it. Any randomized paid reward
(loot box, gacha, card pack, paid roll) MUST clear this before a monetized build
ships. HITL is mandatory — a human approves the per-region matrix.

Per-region required behavior (the artifact MUST include this matrix, current as
of ship date — re-verify, law moves):

| Region | Randomized paid reward | Disclosed odds required | Age gate | Note |
|---|---|---|---|---|
| Belgium (BE) | enforcement-restricted | n/a | n/a | Belgian Gaming Commission's 2018 position treats paid loot boxes as gambling; publishers commonly disable paid randomization for BE accounts. Position is enforcement-driven, not a clean statutory ban — confirm current state with counsel. |
| Netherlands (NL) | contested / fact-specific | yes | yes | The Dutch Gaming Authority fined EA in 2019, but the Council of State **overturned** that ruling in 2022 (FIFA packs not a standalone "game of chance"). Status is fact-specific (tradeable/cashable items carry the most risk) — confirm with counsel, do not assume a flat ban. |
| China (CN) | allowed | YES — mandatory published rates | yes | also probability + duration limits |
| Japan (JP) | allowed | yes | yes | "kompu gacha" (complete-gacha) is banned |
| South Korea (KR) | allowed | YES — 2024 disclosure law | yes | drop rates legally mandated |
| US (Apple App Store) | allowed | yes (guideline 3.1.1) | yes | publish odds |
| US (Google Play) | allowed | yes (Play policy) | yes | publish odds |
| EU general | allowed | yes | 18+ recommended | DSA/consumer-law pressure; anticipate tightening |
| Australia (AU) | allowed | yes | R/M cues | 2024 mandatory M rating for paid loot boxes |
| Brazil (BR) | allowed | yes | yes | consumer-protection disclosure |

Score 0..1 per cluster:

- **region_matrix**: complete, dated, with a documented disable/alternate path
  for banned regions (esp. BE).
- **odds_published**: actual drop rates published where required, in-client and
  pre-purchase.
- **pity_floor**: pity timer / hard floor documented and player-visible.
- **age_gate**: age verification path per region; minors blocked from paid
  randomization where required; COPPA/GDPR-K respected.
- **no_kompu_gacha**: no complete-gacha mechanic (illegal in JP).
- **spend_safeguards**: spend caps / parental controls / refund path documented.
- **alternate_offering**: a non-random direct-purchase path exists (de-risks
  "gambling" classification).

Outcome:
- pass: every cluster ≥ 0.8 AND a human (The Arbiter) approved the matrix.
- revise: any cluster in [0.6, 0.8).
- fail: any cluster < 0.6 OR BE not disabled OR odds not published where required
  OR kompu-gacha present.
