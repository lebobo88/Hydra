"""Rendering + naming helpers for the tracked `Plan` artifact (P2).

This module turns a validated `hydra_core.schemas.Plan` envelope into two
git-diffable, on-disk representations:

* :func:`render_plan_html` -- a self-contained HTML fragment meant to live at
  ``docs/plans/<slug>.html`` (written via
  ``hydra_core.artifact_store.write_repo_artifact``). It doubles as a valid
  Claude Code Artifact body (no ``<!doctype>``/``<html>``/``<head>``/``<body>``
  wrapper, no external resources, theme-aware) so the exact same file works
  whether it is opened locally or ever published.
* :func:`render_plan_json` -- a machine-readable companion. This is also what
  a future AgentSmith ``checkPlan`` validator would read, since the HTML has
  no frontmatter to inspect.

Production wiring (cross-vendor judge finding, b1baf30 revise round, item 2):
``render_plan_html`` IS on the production path -- ``hydra_core.ingest.
dispatch_ingested_envelopes`` calls it directly for every ingested ``PLAN``
envelope and writes the result via ``write_repo_artifact``, THEN stores the
same envelope's ``model_dump(mode="json")`` on ``state.plan_ref``.
``render_plan_json`` is a STANDALONE helper nothing in the engine calls yet
-- the live judge path instead reads ``state.plan_ref`` and serializes it
through ``judge.dispatcher._envelope_to_text`` (which shares this module's
strict-JSON guarantee via ``hydra_core.strict_json.dumps_strict``, not
``render_plan_json`` itself). Both writers protect against a non-finite
value independently; see each function's own docstring for its specific
backstop.

**Diffability is the whole point.** Both renderers are deterministic
(rendering the same `Plan` twice is byte-identical): no timestamps, no
random ordering, no minification. Every step is emitted as its own line/row
so a one-step edit produces a one-line diff.
"""
from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from typing import Sequence

from .schemas import Plan, PlanStep
from .strict_json import dumps_strict

__all__ = [
    "PlanFigure",
    "PlanFigureError",
    "append_governance_note",
    "plan_slug",
    "render_plan_html",
    "render_plan_json",
]


# ---------------------------------------------------------------------------
# Slugging
# ---------------------------------------------------------------------------

_SLUG_MAX_GOAL_CHARS = 48
_NON_SLUG_RE = re.compile(r"[^a-z0-9]+")


def plan_slug(goal: str, workflow_id: object) -> str:
    """Kebab-case ``goal``, truncate to 48 chars, and append a short hash of
    ``workflow_id`` so two workflows that share identical goal text can never
    collide on the same artifact path.

    Stable across calls for the same inputs (no randomness, no wall-clock).
    """
    kebab = _NON_SLUG_RE.sub("-", goal.strip().lower()).strip("-")
    kebab = kebab[:_SLUG_MAX_GOAL_CHARS].rstrip("-") or "plan"
    digest = hashlib.sha256(str(workflow_id).encode("utf-8")).hexdigest()[:8]
    return f"{kebab}-{digest}"


# ---------------------------------------------------------------------------
# Figures (optional imagery attached to a plan or a specific step)
# ---------------------------------------------------------------------------


class PlanFigureError(ValueError):
    """Raised when a `PlanFigure.relative_path` fails the relative-only guard."""


_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")

# ASCII whitespace (\x00-\x20, which covers space/tab/newline/CR/etc.) plus
# DEL and the C1 control range. Browsers trim leading C0 controls and spaces
# before resolving a URL, so a leading "\n" or " " in front of an otherwise
# blocked scheme (e.g. "\nhttps://evil.example/x.png" or " javascript:...")
# would still pass a scheme check performed on the untrimmed string. Reject
# outright rather than trim: trimming would silently accept the attack input.
_WHITESPACE_OR_CONTROL_RE = re.compile(r"[\x00-\x20\x7f-\x9f]")


def _validate_figure_relative_path(relative_path: str) -> None:
    """Reject anything that is not a plain, repo-relative path.

    Guards against HTML-escaping being mistaken for URL-scheme safety: an
    attribute value can be perfectly escaped and still carry a `https://`,
    `data:`, or protocol-relative `//` scheme that an `<img src>` will happily
    fetch or inline. Also rejects absolute paths, `..` traversal segments, and
    backslashes (a Windows-style absolute/drive path or an escape attempt
    written with the "wrong" separator).
    """
    if not relative_path:
        raise PlanFigureError("figure relative_path must not be empty")
    if _WHITESPACE_OR_CONTROL_RE.search(relative_path):
        raise PlanFigureError(
            f"figure relative_path must not contain whitespace or control characters: {relative_path!r}"
        )
    if "\\" in relative_path:
        raise PlanFigureError(f"figure relative_path must not contain backslashes: {relative_path!r}")
    if relative_path.startswith("//"):
        raise PlanFigureError(f"figure relative_path must not be protocol-relative: {relative_path!r}")
    if relative_path.startswith("/"):
        raise PlanFigureError(f"figure relative_path must not be absolute: {relative_path!r}")
    if _SCHEME_RE.match(relative_path):
        raise PlanFigureError(f"figure relative_path must not carry a URL scheme: {relative_path!r}")
    segments = relative_path.split("/")
    if any(seg == ".." for seg in segments):
        raise PlanFigureError(f"figure relative_path must not contain '..' segments: {relative_path!r}")


@dataclass(frozen=True)
class PlanFigure:
    """A figure referenced by RELATIVE path only -- never a data: URI.

    ``relative_path`` is relative to the rendered HTML's own location, e.g.
    ``assets/<slug>/architecture.png`` for a file living alongside it under
    ``docs/plans/assets/<slug>/``. ``step_id`` ties the figure to one step's
    detail block; leave it ``None`` for a plan-level figure (referenced only
    from the Provenance section).

    Validated eagerly at construction (not merely HTML-escaped at render
    time): a `PlanFigure` with a `https://`, `data:`, protocol-relative
    `//`, absolute, backslash-bearing, or `..`-traversing `relative_path`
    raises :class:`PlanFigureError` rather than ever being handed to a
    renderer. HTML-escaping an attribute value constrains its *characters*,
    not its URL *scheme* -- those are two different guards and this class
    only ever gets to rely on both.
    """

    relative_path: str
    alt_text: str
    generated_by: str | None = None
    step_id: str | None = None

    def __post_init__(self) -> None:
        _validate_figure_relative_path(self.relative_path)


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

_STYLE = """<title>Plan: {title}</title>
<style>
:root {
  --plan-bg: #fbfbfa;
  --plan-fg: #1b1b18;
  --plan-muted: #6b6b63;
  --plan-border: #ddd9d0;
  --plan-surface: #ffffff;
  --plan-accent: #7c5cff;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --plan-bg: #17171a;
    --plan-fg: #eceae5;
    --plan-muted: #9a988f;
    --plan-border: #35342f;
    --plan-surface: #202024;
    --plan-accent: #a78bfa;
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --plan-bg: #17171a;
  --plan-fg: #eceae5;
  --plan-muted: #9a988f;
  --plan-border: #35342f;
  --plan-surface: #202024;
  --plan-accent: #a78bfa;
  color-scheme: dark;
}
body {
  background: var(--plan-bg);
  color: var(--plan-fg);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  padding-inline: 20px;
  padding-block: 24px;
  max-width: 920px;
  margin-inline: auto;
}
h1, h2, h3 { color: var(--plan-fg); }
h2 { border-bottom: 1px solid var(--plan-border); padding-bottom: 6px; margin-top: 2em; }
.plan-meta { color: var(--plan-muted); font-size: 0.9em; }
.plan-table-wrap { overflow-x: auto; }
table.plan-steps { border-collapse: collapse; width: 100%; font-size: 0.92em; }
table.plan-steps th, table.plan-steps td {
  border: 1px solid var(--plan-border);
  padding: 6px 10px;
  text-align: left;
  vertical-align: top;
}
table.plan-steps th { background: var(--plan-surface); }
.plan-step-detail {
  border: 1px solid var(--plan-border);
  border-radius: 6px;
  padding: 12px 16px;
  margin-block-end: 12px;
  background: var(--plan-surface);
}
.plan-list { margin: 0; padding-inline-start: 1.4em; }
.plan-figure img { max-width: 100%; border: 1px solid var(--plan-border); border-radius: 4px; }
</style>"""


def _esc(text: str | None) -> str:
    return html.escape(text or "", quote=True)


def _format_budget(value: float | None) -> str:
    """Format a USD amount for the HTML report, or refuse a non-finite one.

    Cross-vendor judge finding (item 5): ``Constraints.budget_usd`` and
    ``PlanStep.estimated_budget_usd`` reject NaN/Infinity/-Infinity at
    construction, so a normally-validated ``Plan`` can never carry one here.
    A ``Plan`` reaching this renderer via ``model_construct``/``model_copy``
    (both skip validation) could, though — and ``f"${value:.2f}"`` happily
    formats a NaN/Infinity float as the literal string ``$nan``/``$inf``
    rather than erroring. Refuse instead of silently rendering that.
    """
    if value is None:
        return "—"
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(
            f"refusing to render a non-finite budget value in the plan HTML: {value!r}"
        )
    return f"${value:.2f}"


def _sum_step_budgets(steps: Sequence[PlanStep]) -> tuple[float | None, bool]:
    """Sum finite step budgets, guarding against float overflow.

    Cross-vendor judge finding (item 2/4): ``PlanStep.estimated_budget_usd``
    rejects NaN/Infinity at construction, so every addend here is
    individually finite and valid — but two accepted values near
    ``sys.float_info.max`` (e.g. two steps at 1e308) still overflow a plain
    ``sum()`` to ``inf``. That is a READ combining already-validated data,
    not a fresh write, so the guiding principle applies: report it, never
    raise. Returns ``(total, overflowed)``; when ``overflowed`` is True the
    caller renders the total as unavailable instead of calling
    ``_format_budget`` on an ``inf``.
    """
    total = 0.0
    for step in steps:
        value = step.estimated_budget_usd
        if value is None:
            continue
        total += value
        if total != total or total in (float("inf"), float("-inf")):
            return None, True
    return total, False


def _list_block(items: Sequence[str], empty_text: str) -> str:
    if not items:
        return f"<p class=\"plan-meta\">{_esc(empty_text)}</p>"
    lines = ["<ul class=\"plan-list\">"]
    for item in items:
        lines.append(f"  <li>{_esc(item)}</li>")
    lines.append("</ul>")
    return "\n".join(lines)


def _step_table(steps: Sequence[PlanStep]) -> str:
    rows = [
        "<div class=\"plan-table-wrap\">",
        "<table class=\"plan-steps\">",
        "<thead>",
        "<tr>"
        "<th>step_id</th><th>title</th><th>squad</th><th>envelope</th>"
        "<th>depends_on</th><th>priority</th><th>budget</th>"
        "</tr>",
        "</thead>",
        "<tbody>",
    ]
    for step in steps:
        depends = ", ".join(step.depends_on) if step.depends_on else "—"
        budget = _format_budget(step.estimated_budget_usd)
        rows.append(
            "<tr>"
            f"<td>{_esc(step.step_id)}</td>"
            f"<td>{_esc(step.description)}</td>"
            f"<td>{_esc(step.target_squad)}</td>"
            f"<td>{_esc(step.envelope_type)}</td>"
            f"<td>{_esc(depends)}</td>"
            f"<td>{_esc(step.priority)}</td>"
            f"<td>{_esc(budget)}</td>"
            "</tr>"
        )
    rows.append("</tbody>")
    rows.append("</table>")
    rows.append("</div>")
    return "\n".join(rows)


def _step_details(steps: Sequence[PlanStep], figures_by_step: dict[str, list[PlanFigure]]) -> str:
    blocks: list[str] = []
    for step in steps:
        lines = [
            f"<div class=\"plan-step-detail\" id=\"step-{_esc(step.step_id)}\">",
            f"<h3>{_esc(step.step_id)}</h3>",
            f"<p>{_esc(step.description)}</p>",
            "<p class=\"plan-meta\">"
            f"squad: {_esc(step.target_squad)} &middot; "
            f"envelope: {_esc(step.envelope_type)} &middot; "
            f"priority: {_esc(step.priority)}"
            "</p>",
            "<p><strong>Acceptance criteria</strong></p>",
            _list_block(step.acceptance_criteria, "None recorded."),
        ]
        if step.rationale:
            lines.append(f"<p><strong>Rationale:</strong> {_esc(step.rationale)}</p>")
        for figure in figures_by_step.get(step.step_id, []):
            lines.append(
                "<p class=\"plan-figure\">"
                f"<img src=\"{_esc(figure.relative_path)}\" alt=\"{_esc(figure.alt_text)}\">"
                "</p>"
            )
        lines.append("</div>")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


# Characters that are syntactically meaningful inside a quoted Mermaid node
# label (`n0["..."]`), plus every control character including newline/CR.
_MERMAID_UNSAFE_RE = re.compile(r'["\[\]{}()<>`;|#\x00-\x1f\x7f]')


def _mermaid_sanitize(text: str) -> str:
    """Strip Mermaid-syntax-meaningful characters from raw label text.

    HTML-escaping is not Mermaid-language escaping: a browser decodes HTML
    entities (`&quot;` -> `"`) *before* handing the text of a `<pre>` block
    to the Mermaid parser, so a `step_id` containing a quote, a closing
    bracket, or a newline can close the quoted label and inject a bogus
    edge between the synthetic node ids -- even though the HTML itself was
    perfectly escaped. This must run on the RAW text, before `_esc()`, so
    the character is actually gone rather than merely re-encoded into a form
    the browser will decode right back.
    """
    return _MERMAID_UNSAFE_RE.sub(" ", text)


def _mermaid_graph(steps: Sequence[PlanStep]) -> str:
    """Render the dependency graph with SYNTHETIC node ids (`n0`, `n1`, ...).

    `step_id` is operator/LLM-authored free text -- `Plan`'s schema places no
    charset restriction on it. If it were used as the literal Mermaid node id
    (as an earlier version of this function did), a `step_id` containing a
    newline or `-->` could forge an edge that the envelope's `depends_on`
    never declared, and one containing `</pre><script>` could break out of
    the surrounding `<pre>` block entirely. Neither is possible here: graph
    topology (node ids and edges) is built exclusively from array position,
    and `step_id` appears ONLY inside a label that is BOTH Mermaid-sanitized
    (see `_mermaid_sanitize`) and HTML-escaped, where it can affect the
    rendered text of that one node and nothing else.
    """
    node_ids = {step.step_id: f"n{i}" for i, step in enumerate(steps)}
    lines = ["<pre class=\"mermaid\">", "graph TD"]
    for step in steps:
        raw_label = f"{step.step_id}: {step.description}"
        if len(raw_label) > 60:
            raw_label = raw_label[:57] + "..."
        label = _esc(_mermaid_sanitize(raw_label))
        lines.append(f"    {node_ids[step.step_id]}[\"{label}\"]")
    edges: list[tuple[str, str]] = []
    for step in steps:
        for dep in sorted(step.depends_on):
            # `Plan` validates every `depends_on` entry against a known
            # `step_id` (unknown-dep and self-dep are rejected at schema
            # construction), so `dep` is always a key of `node_ids` here.
            edges.append((node_ids[dep], node_ids[step.step_id]))
    for src, dst in edges:
        lines.append(f"    {src} --> {dst}")
    lines.append("</pre>")
    return "\n".join(lines)


def render_plan_html(
    plan: Plan,
    *,
    constitution_hash: str | None = None,
    judge_verdict: str | None = None,
    approval_record: str | None = None,
    figures: Sequence[PlanFigure] = (),
) -> str:
    """Render ``plan`` as a deterministic, git-diffable HTML fragment.

    No ``<!doctype>``/``<html>``/``<head>``/``<body>`` wrapper, no external
    scripts/stylesheets/images, no ``data:`` URIs. Theme-aware per the
    Artifact contract. Heading order is fixed (the diff contract): Goal,
    Rigor and why, Non-goals, Open questions, Assumptions/Risks, step table,
    per-step detail, Mermaid dependency graph, budget estimate, judge verdict
    and dissents, approval record, Provenance.
    """
    figures_by_step: dict[str, list[PlanFigure]] = {}
    unassigned_figures: list[PlanFigure] = []
    for fig in figures:
        if fig.step_id:
            figures_by_step.setdefault(fig.step_id, []).append(fig)
        else:
            unassigned_figures.append(fig)

    total_budget, total_overflowed = _sum_step_budgets(plan.steps)
    plan_budget_cap = plan.constraints.budget_usd

    parts: list[str] = []
    parts.append(_STYLE.replace("{title}", _esc(plan.goal_restatement)[:80]))

    parts.append(f"<h1>Plan: {_esc(plan.goal_restatement)}</h1>")

    parts.append("<h2>Goal</h2>")
    parts.append(f"<p>{_esc(plan.goal_restatement)}</p>")

    parts.append("<h2>Rigor and Why</h2>")
    parts.append(f"<p><strong>{_esc(plan.rigor)}</strong> &mdash; {_esc(plan.summary)}</p>")

    parts.append("<h2>Non-Goals</h2>")
    parts.append(_list_block(plan.non_goals, "None recorded."))

    parts.append("<h2>Open Questions</h2>")
    parts.append(_list_block(plan.open_questions, "None recorded."))

    parts.append("<h2>Assumptions / Risks</h2>")
    parts.append(_list_block(plan.risks, "None recorded."))

    parts.append("<h2>Steps</h2>")
    parts.append(_step_table(plan.steps))

    parts.append("<h2>Step Details</h2>")
    parts.append(_step_details(plan.steps, figures_by_step))

    parts.append("<h2>Dependency Graph</h2>")
    parts.append(_mermaid_graph(plan.steps))

    parts.append("<h2>Budget Estimate</h2>")
    total_budget_text = (
        "unavailable (sum of step budgets overflowed float range)"
        if total_overflowed else _format_budget(total_budget)
    )
    cap_text = _format_budget(plan_budget_cap) if plan_budget_cap is not None else "not set"
    parts.append(
        f"<p>Sum of per-step estimates: {_esc(total_budget_text)} &middot; "
        f"Plan-level cap: {_esc(cap_text)}</p>"
    )

    parts.append("<h2>Judge Verdict and Dissents</h2>")
    parts.append(f"<p>{_esc(judge_verdict) if judge_verdict else 'No verdict recorded yet.'}</p>")
    parts.append(_list_block(plan.dissents, "No dissents recorded."))

    parts.append("<h2>Approval Record</h2>")
    parts.append(
        f"<p>{_esc(approval_record) if approval_record else 'Not yet approved.'}</p>"
    )

    parts.append("<h2>Provenance</h2>")
    prov_lines = [
        "<ul class=\"plan-list\">",
        f"  <li>workflow_id: {_esc(str(plan.workflow_id))}</li>",
        f"  <li>plan envelope id: {_esc(str(plan.id))}</li>",
        f"  <li>plan_revision: {_esc(str(plan.plan_revision))}</li>",
        f"  <li>constitution hash: {_esc(constitution_hash or 'not recorded')}</li>",
    ]
    if unassigned_figures:
        for fig in unassigned_figures:
            gen = fig.generated_by or "unrecorded generator"
            prov_lines.append(
                f"  <li>imagery {_esc(fig.relative_path)}: generated by {_esc(gen)}</li>"
            )
        for fig in unassigned_figures:
            prov_lines.append(
                "  <li class=\"plan-figure\">"
                f"<img src=\"{_esc(fig.relative_path)}\" alt=\"{_esc(fig.alt_text)}\">"
                "</li>"
            )
    else:
        prov_lines.append("  <li>imagery: none</li>")
    prov_lines.append("</ul>")
    parts.append("\n".join(prov_lines))

    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# Governance notes -- P5c
# ---------------------------------------------------------------------------

_GOVERNANCE_NOTES_RE = re.compile(
    r'(<h2>Governance Notes</h2>\s*<ul class="plan-list">)(.*?)(</ul>)',
    re.DOTALL,
)


def append_governance_note(html_text: str, note: str) -> str:
    """Append a governance note (e.g. a force-dispatch plan-gate bypass) to a
    rendered plan artifact's HTML, in place.

    A plan artifact is written once by the ingest PLAN branch
    (`render_plan_html`) and never re-rendered from the `Plan` model
    afterward -- a governance event that happens AFTER the artifact exists
    (an operator force-dispatching past `plan_gate`, for instance) has no
    `Plan` to re-render, only the HTML text already on disk. This function
    edits that text directly rather than requiring a `Plan` object.

    If a "Governance Notes" section already exists (from a prior note), the
    new note is appended as another ``<li>`` inside that SAME section's
    ``<ul>`` -- never a second heading. Structurally idempotent (one
    heading, ever); NOT content-idempotent (every call adds one more note --
    callers decide whether a given note has already been recorded, the same
    convention the rest of this engine's ``hitl_history`` uses).
    """
    entry = f"  <li>{_esc(note)}</li>"
    match = _GOVERNANCE_NOTES_RE.search(html_text)
    if match:
        existing_items = match.group(2).rstrip()
        merged = f"{existing_items}\n{entry}\n" if existing_items else f"{entry}\n"
        return (
            html_text[: match.start()]
            + match.group(1) + "\n" + merged + match.group(3)
            + html_text[match.end():]
        )
    section = (
        "\n<h2>Governance Notes</h2>\n"
        '<ul class="plan-list">\n'
        f"{entry}\n"
        "</ul>\n"
    )
    return html_text.rstrip("\n") + "\n" + section


# ---------------------------------------------------------------------------
# JSON rendering
# ---------------------------------------------------------------------------


def render_plan_json(plan: Plan) -> str:
    """Render ``plan`` as a deterministic, machine-readable JSON companion.

    This is what a future AgentSmith ``checkPlan`` validator would consume,
    since the HTML rendering above has no frontmatter to inspect. Keys are
    sorted and the payload is stable across calls for the same `Plan`.

    ``Constraints.budget_usd`` and ``PlanStep.estimated_budget_usd`` already
    reject NaN/Infinity/-Infinity at construction (see `hydra_core.schemas`),
    so a normally-constructed `Plan` can never reach this function holding a
    non-finite value. ``dumps_strict`` (``hydra_core.strict_json`` — the one
    shared seam every envelope/plan-to-JSON-text site routes through, see
    that module's docstring) is a BACKSTOP for a `Plan` built via
    `model_construct`/`model_copy` (both skip validation) or any other path
    that bypasses the schema: rather than silently emitting the bare word
    `NaN`/`Infinity` (valid Python-`json` output, invalid RFC 8259 JSON that
    AgentSmith's `checkPlan` refuses), this raises `ValueError` naming the
    offending field and never writes anything.
    """
    payload = plan.model_dump(mode="json")
    return dumps_strict(
        payload, label=f"Plan {plan.id}",
        sort_keys=True, indent=2, ensure_ascii=False,
    ) + "\n"
