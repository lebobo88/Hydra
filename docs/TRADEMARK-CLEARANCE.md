# Trademark Clearance — Hydra

**Status as of 2026-05-19:** open. Trademark research and clearance must
precede public launch. The manifesto's Caveat #1 names this explicitly.

## Known prior art

| Mark | Holder | Class | Note |
|---|---|---|---|
| HYDRA | Marvel Entertainment, LLC | various (entertainment) | Villainous fictional organization; the dominant pop-culture association. Public domain reframing is non-trivial. |
| HYDRA | Lockheed Martin | military/cybersecurity | HYDRA cybersecurity system; specialized B2G usage. |
| Hydra | The Hydra (older distributed systems platform, Microsoft Research) | computing | Largely retired; obscure. |
| Apache Hydra / Hydra-Head | open-source | software (digital repository) | Active OSS but a different domain. |
| Hydra (gene-editing assay) | various biotech | biotech | Adjacent science usage. |

None of these are presently dominant in the **AI orchestration** trade
class. The risk is brand-association rather than direct conflict —
"HYDRA" still reads "villain" in pop culture, which the brand strategy
must counter through repeated explicit reframing (constellation, not
monster; Pentecost, not Legion).

## Actions before public launch

1. **Formal USPTO + EUIPO search** for "Hydra" / "Hydra AI" / variations
   in the relevant Nice Classification classes (most likely 9, 42).
   Out of scope for the founder; needs IP counsel.
2. **Domain audit**: which .com/.ai/.io variants are clean and
   available. (Not in this doc.)
3. **Wordmark and design-mark filings** — the sigil (see `docs/BRAND.md`)
   is independently registrable as a design mark and should be filed
   alongside the wordmark to anchor the visual reframing.
4. **Social handle availability** across the major platforms.
5. **Co-existence agreement** consideration if any current registrant
   in an adjacent class raises an opposition.

## Fallback wordmarks

If "Hydra" cannot be cleared in the trade class, candidates that gesture
to the same imagery while keeping the architecture intact:

- **Polycephalon** — direct Greek root, clinical register, very low
  collision probability. Loses the constellation thread.
- **Lerna** — the lake where the Hydra lived; geographic, evocative,
  short. Quietly literary.
- **Iolaus** — the cauterizer, the *operator* of the Hydra rather than
  the Hydra itself. Reframes the brand as the *user*, which is
  philosophically aligned with the manifesto's inversion.
- **TheEights** — promote the memory substrate to the product name.
  Lower brand-collision risk; loses the head/crown architecture as the
  primary metaphor.
- **Constellation** + a qualifier — "Constellation Council", "Hydra
  Constellation", etc. Lifts the wayfinding metaphor to the foreground.

The recommendation, if a fallback is required: keep "Hydra" as the
**internal persona name** (the way the system refers to itself, the
voice in synthesized output) and pick a clean customer-facing wordmark
that gestures to the same imagery. The persona doesn't need a
registered mark; the product does.

## What does not need clearance

- The mythic head names (Solon, Athena, Hermes, Daedalus, Prometheus,
  Cerberus, Calliope, etc.). These are public-domain mythology and
  appear in countless prior products without dilution effects.
- The eight cells (Qian/Kun/…). I Ching trigrams are unregistrable.
- The infinity/lemniscate. Mathematical figure, not protectable.
- The "Pentecost vs. Legion" framing. Theological language is not
  ownable.

## Decision

Do not ship the public wordmark "Hydra" without clearance. The
internal codebase, internal docs, and the manifesto can use "Hydra"
freely; that's pre-launch identity, not trade use. Public launch
materials (marketing site, app store listings, npm/pypi package
names if commercial) require the cleared mark.
