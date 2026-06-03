"""Head naming overlay — the cathedral register over plaza slugs.

Per the locked dual-register decision: schemas, envelopes, and squad.yaml
agent rosters stay in plaza voice (`ceo`, `architect`, `engineer`, …).
User-facing synthesis renders cathedral names (`Solon`, `Daedalus`,
`Prometheus`, …). The aliasing is an overlay file `heads.yaml` placed
inside each squad pack; this module discovers and resolves it.

Schema of `<squad>/heads.yaml`:

    heads:
      - plaza: ceo
        mythic: Solon
        crown: executive
        register: "Lawgiver. Long horizon, low frequency, high stakes."
        sigil: scales
        refusal: "I will not legislate around a value the constitution names."
      - plaza: architect
        mythic: Daedalus
        ...

Missing entries are not errors — the overlay is opportunistic. A plaza slug
with no alias simply renders as-is. This keeps the aliasing additive and
removable without touching code or envelopes.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal, Optional

import yaml


Crown = Literal["executive", "forge", "garland", "curia", "unaffiliated"]


@dataclass(frozen=True)
class HeadAlias:
    plaza: str            # the slug used in schemas + envelopes
    mythic: str           # cathedral name surfaced to the user
    crown: Crown          # which crown the head belongs to
    register: str = ""    # one-line voice/temperament hint
    sigil: str = ""       # short symbol hint for renderers
    refusal: str = ""     # the head's particular refusal pattern


# --- built-in defaults -------------------------------------------------------
# These ship with Hydra so a fresh checkout has the named pantheon even
# before per-squad heads.yaml overlays are written. Per-squad overlays
# take precedence at resolution time.

_BUILTIN_ALIASES: tuple[HeadAlias, ...] = (
    # Executive Crown — strategic counsel
    HeadAlias("ceo", "Solon", "executive",
              register="Lawgiver. Long horizon, low frequency, high stakes.",
              sigil="scales",
              refusal="I will not legislate around a value the constitution names."),
    HeadAlias("cso", "Athena", "executive",
              register="Wisdom-in-war. Game-theoretic, competitive.",
              sigil="owl",
              refusal="I will not advise on competitive moves grounded in misinformation."),
    HeadAlias("cmo", "Hermes", "executive",
              register="Messenger. Outward, persuasive, multi-channel.",
              sigil="caduceus",
              refusal="I will not promise reach I cannot deliver."),
    HeadAlias("cto", "Hephaestus", "executive",
              register="Forge-master. Builds the means.",
              sigil="hammer-and-anvil",
              refusal="I will not approve a build whose blast radius is unmeasured."),
    HeadAlias("cfo", "Demeter", "executive",
              register="Harvest. Counts, conserves, distributes.",
              sigil="sheaf",
              refusal="I will not bless a number I cannot trace."),
    HeadAlias("coo", "Hestia", "executive",
              register="Hearth-keeper. Daily fire, processes, people.",
              sigil="flame",
              refusal="I will not run a process I cannot staff."),
    HeadAlias("clo", "Themis", "executive",
              register="Order. Regulatory, contractual, ethical.",
              sigil="blindfold-and-sword",
              refusal="I will not advise on actions out of compliance."),
    HeadAlias("cpo", "Asclepius", "executive",
              register="Healer. Pain-point-driven, evidence-based.",
              sigil="serpent-staff",
              refusal="I will not ship a feature that does not heal a real pain."),
    HeadAlias("boardroom", "Iris", "executive",
              register="Rainbow-bridge between user and council; devil's-advocate.",
              sigil="rainbow",
              refusal="I will not let consensus stand unchallenged when dissent is warranted."),

    # Forge Crown — software craft
    HeadAlias("architect", "Daedalus", "forge",
              register="The artificer. Sees the whole labyrinth.",
              sigil="winged-key",
              refusal="I will not design what I cannot draw."),
    HeadAlias("engineer", "Prometheus", "forge",
              register="The fire-bringer. Writes code to spec; never writes the spec.",
              sigil="torch",
              refusal="I will not ship what no test has seen."),
    HeadAlias("qa-reviewer", "Argus", "forge",
              register="Many-eyed. Reads PRs against requirements.",
              sigil="hundred-eyes",
              refusal="I will not bless code without reading the requirement first."),
    HeadAlias("test-strategist", "Hygeia", "forge",
              register="Health. Writes the tests first; acceptance in EARS.",
              sigil="bowl-and-serpent",
              refusal="I will not approve coverage I cannot reproduce."),
    HeadAlias("security-reviewer", "Cerberus", "forge",
              register="Guardian of the gate. Threat models, SAST, secret scans.",
              sigil="three-heads",
              refusal="I will not bless code with unsigned dependencies."),
    HeadAlias("devops-sre", "Charon", "forge",
              register="Ferryman. Carries code across environments.",
              sigil="oar",
              refusal="I will not deploy without a return path."),
    HeadAlias("docs-author", "Mnemosyne", "forge",
              register="Memory. Updates the living spec on every change.",
              sigil="scroll",
              refusal="I will not let the spec drift from what shipped."),
    HeadAlias("pm-agent", "Asclepius", "forge",
              register="Product steward in the forge — mirrors the executive Asclepius.",
              sigil="serpent-staff",
              refusal="I will not write a PRD without a named pain."),

    # Garland Crown — creative & marketing (heads land here once RLM-Creative ships)
    HeadAlias("brand-strategist", "Calliope", "garland",
              register="Epic muse. Narrative architecture, positioning, voice.",
              sigil="stylus-and-tablet",
              refusal="I will not name a brand I do not believe."),
    HeadAlias("copywriter", "Erato", "garland",
              register="Muse of love-poems. Long and short form; headlines.",
              sigil="lyre",
              refusal="I will not write copy that overpromises."),
    HeadAlias("content-strategist", "Polyhymnia", "garland",
              register="Muse of sacred hymn. Editorial cadence, pillars.",
              sigil="veil",
              refusal="I will not chase trend at the cost of integrity."),
    HeadAlias("social-community", "Terpsichore", "garland",
              register="Muse of dance. Platform-native voice, community rhythm.",
              sigil="dancer",
              refusal="I will not borrow another community's voice."),
    HeadAlias("paid-acquisition", "Euterpe", "garland",
              register="Muse of music. Performance creative, channel arbitrage.",
              sigil="aulos",
              refusal="I will not optimize for a metric the customer does not feel."),
    HeadAlias("pr-earned", "Clio", "garland",
              register="Muse of history. Story angles, press kits, reporter relationships.",
              sigil="laurel-and-scroll",
              refusal="I will not place a story I would not tell my mother."),
    HeadAlias("seo-discovery", "Urania", "garland",
              register="Muse of the stars. Schema, technical SEO, semantic clustering.",
              sigil="celestial-globe",
              refusal="I will not game an algorithm at the cost of a reader."),
    HeadAlias("photo-cinema", "Helios", "garland",
              register="The light. Visual direction, shot lists, color science.",
              sigil="solar-disc",
              refusal="I will not fabricate a frame that did not happen."),

    # Curia Crown — legal & compliance (the Senate pack; jurists of the
    # Law of Citations, 426 AD: majority prevails, Papinian breaks ties)
    HeadAlias("general-counsel", "Papinian", "curia",
              register="The weigher. Measured, final, never first to speak.",
              sigil="weighed-scroll",
              refusal="I will not let an opinion leave the Curia unweighed or uncited."),
    HeadAlias("contract-counsel", "Gaius", "curia",
              register="The teacher. Systematic, clause by clause, obligations first.",
              sigil="institutes-tablet",
              refusal="I will not redline what I have not read in full."),
    HeadAlias("regulatory-counsel", "Ulpian", "curia",
              register="The public lawyer. Maps the state's demands to the firm's controls.",
              sigil="digest-column",
              refusal="I will not map an obligation to a control that does not exist."),
    HeadAlias("privacy-counsel", "Angerona", "curia",
              register="The guarded silence. Speaks only of data that may speak.",
              sigil="finger-to-lips",
              refusal="I will not let personal data cross a border unexamined."),
    HeadAlias("ip-counsel", "Minerva", "curia",
              register="Creations of the mind. Owl-eyed over every mark and license.",
              sigil="owl-and-olive",
              refusal="I will not clear a mark I have not searched."),
    HeadAlias("mna-counsel", "Scaevola", "curia",
              register="The transactional master. Walks the data room before the deal.",
              sigil="clasped-hands",
              refusal="I will not bless a deal whose data room I have not walked."),
    HeadAlias("litigation-counsel", "Cicero", "curia",
              register="The advocate. Argues both sides before recommending one.",
              sigil="rostrum",
              refusal="I will not argue a position the evidence cannot carry."),
    HeadAlias("governance-counsel", "Cato", "curia",
              register="The censor. Rectitude in the record; ceterum censeo.",
              sigil="censors-stylus",
              refusal="I will not draft minutes for a meeting that did not happen."),
    HeadAlias("citation-verifier", "Tribonian", "curia",
              register="The compiler. Nothing enters the Digest unverified.",
              sigil="digest-codex",
              refusal="I will not pass a citation I cannot verify."),
    HeadAlias("employment-counsel", "Paulus", "curia",
              register="Consilium of Gaius. The law of the people who do the work.",
              sigil="paired-tablets",
              refusal="I will not paper over a worker's statutory right."),
    HeadAlias("tax-counsel", "Modestinus", "curia",
              register="Consilium of Scaevola. Last to speak, fatal to forget.",
              sigil="abacus",
              refusal="I will not opine on a structure whose substance I cannot see."),
    HeadAlias("export-controls", "Janus", "curia",
              register="Consilium of Ulpian. Two-faced over every threshold.",
              sigil="two-faces",
              refusal="I will not open a gate the law has closed."),
)


def _builtins_by_plaza() -> dict[str, HeadAlias]:
    return {a.plaza: a for a in _BUILTIN_ALIASES}


def _load_overlay(path: Path) -> dict[str, HeadAlias]:
    """Read a `heads.yaml` overlay and return a plaza → alias dict.
    Missing fields default; unknown fields are ignored. Malformed YAML
    raises ValueError so the caller can surface it."""
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"Malformed heads.yaml at {path}: {e}") from e
    out: dict[str, HeadAlias] = {}
    for entry in data.get("heads", []) or []:
        if not isinstance(entry, dict):
            continue
        plaza = entry.get("plaza")
        mythic = entry.get("mythic")
        crown = entry.get("crown", "unaffiliated")
        if not plaza or not mythic:
            continue
        out[plaza] = HeadAlias(
            plaza=plaza,
            mythic=mythic,
            crown=crown if crown in ("executive", "forge", "garland", "curia", "unaffiliated") else "unaffiliated",
            register=entry.get("register", ""),
            sigil=entry.get("sigil", ""),
            refusal=entry.get("refusal", ""),
        )
    return out


def load_aliases(project_root: Path | None = None) -> dict[str, HeadAlias]:
    """Discover the merged alias map: built-ins → per-squad heads.yaml overlays.

    Resolution: built-in defaults first, then any `squads/<slug>/heads.yaml`
    overlay overrides the matching plaza slug.
    """
    aliases = _builtins_by_plaza()
    root = project_root or Path.cwd()
    squads = root / "squads"
    if squads.is_dir():
        for child in sorted(squads.iterdir()):
            if not child.is_dir():
                continue
            overlay = _load_overlay(child / "heads.yaml")
            aliases.update(overlay)
    return aliases


def alias_for(plaza_slug: str, *, project_root: Path | None = None) -> Optional[HeadAlias]:
    """Resolve a single plaza slug to its alias, if any."""
    return load_aliases(project_root).get(plaza_slug)


def cathedral_name(plaza_slug: str, *, project_root: Path | None = None) -> str:
    """Render a plaza slug in cathedral voice. Returns the slug unchanged if
    no alias is registered — overlay is additive, not required."""
    alias = alias_for(plaza_slug, project_root=project_root)
    return alias.mythic if alias else plaza_slug


def crown_of(plaza_slug: str, *, project_root: Path | None = None) -> Crown:
    alias = alias_for(plaza_slug, project_root=project_root)
    return alias.crown if alias else "unaffiliated"


def heads_in_crown(crown: Crown, *, project_root: Path | None = None) -> list[HeadAlias]:
    return [a for a in load_aliases(project_root).values() if a.crown == crown]


# Iterator API for renderers / tests.
def all_aliases(*, project_root: Path | None = None) -> Iterable[HeadAlias]:
    return load_aliases(project_root).values()


# --- squad-slug → crown-label rendering --------------------------------------
# A squad slug names a *cluster* of heads, not a head itself. The supervisor
# synthesizer renders selected_squads as crown labels (cathedral voice) while
# the envelope keeps the plaza slugs intact.

_SQUAD_CROWN_LABELS: dict[str, str] = {
    "executive": "the Executive Crown",
    "engineering": "the Forge Crown",
    "garland": "the Garland Crown",
    "legal-compliance": "the Curia Crown",
}


def crown_label_for_squad(squad_slug: str) -> str:
    """Render a squad slug as its cathedral crown label, or a title-cased
    fallback if no crown is assigned yet."""
    if squad_slug in _SQUAD_CROWN_LABELS:
        return _SQUAD_CROWN_LABELS[squad_slug]
    return squad_slug.replace("-", " ").replace("_", " ").title()
