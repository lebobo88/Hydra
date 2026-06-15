---
id: game-design-pillars-testable@1
bare_id: game-design-pillars-testable
kind: spec
version: 1
title: "Game design pillars are testable claims, not vibes"
owner_head: game-director
---
# Game design pillars — testability rubric

The Director gates every greenlight on this. A pillar is a **falsifiable design
claim** that downstream artifacts (mechanics, levels, encounters) can be checked
against — not a mood word.

Score 0..1 per cluster:

- **falsifiable**: each pillar can be proven wrong by a build. "Combat rewards
  reading enemy tells" is testable; "Engaging combat" is not.
- **decision_content**: each pillar excludes some design it would otherwise
  permit. A pillar that forbids nothing is decoration.
- **audience_singular**: one named target player, not "casual to hardcore".
- **comp_set**: 2–4 reference titles with explicit share/differ deltas.
- **usp**: at least one claim the comp set cannot make.
- **anti_pillars**: at least one named "we will NOT" to bound scope.
- **traceability**: every pillar maps to ≥1 planned mechanic/system that proves it.

Reject on sight (auto-fail any cluster): three-bullet vibe pillars
("Engaging combat / Deep story / Open world"), generic verbs ("explore /
experience / discover"), "hero saves the world" with no specific hook,
"realistic/satisfying/deep" unmeasurable adjectives.

Outcome:
- pass: every cluster ≥ 0.7 AND traceability present for all pillars.
- revise: any cluster in [0.5, 0.7).
- fail: any cluster < 0.5 OR any vibe-pillar present.
