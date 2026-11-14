// Descriptive scorecards. Unknown evidence never becomes a numerical quality/alpha score.
import { append, chip, el, fmtDateTime } from './util.js';

const pct = value => value == null ? '—' : Number(value).toFixed(1) + '%';
const label = name => name.replaceAll('_', ' ').replace(/^./, c => c.toUpperCase());

function metric(title, value, detail) {
  const node = el('div', null, 'evidence-metric');
  append(node, el('div', title, 'muted small'), el('strong', value), el('div', detail, 'muted small'));
  return node;
}

function cohortDetails(cohort) {
  const box = el('div', null, 'stack');
  const o = cohort.observability, c = cohort.counts;
  box.append(el('p', cohort.model, 'small wrap'), el('p', `${c.completed} completed · ${c.failed} failed · ${c.invalid} invalid · ${c.skipped} skipped · ${c.inflight} in progress`, 'small'));
  box.append(el('p', cohort.quality.note, 'small'), el('p', cohort.quality.acceptance_note, 'muted small'));
  box.append(el('p', `Output acceptance: ${pct(cohort.quality.output_acceptance_score)} (${cohort.quality.accepted}/${cohort.quality.evaluated_outputs} outputs).`, 'small'));
  box.append(el('p', cohort.contribution.note, 'muted small'));
  box.append(el('p', `Measured duration: ${o.median_latency_seconds == null ? 'Unknown' : o.median_latency_seconds + 's median'} (${o.latency_sample} measured, ${o.unknown_latency_runs} unknown).`, 'small'));
  box.append(el('p', `Usage: ${o.known_usage_runs} known runs${o.prompt_tokens == null ? '' : ', ' + o.prompt_tokens + ' prompt / ' + o.completion_tokens + ' completion tokens'}. Recorded cost: ${o.cost_usd == null ? 'Unknown' : '$' + o.cost_usd + ' across ' + o.known_cost_runs + ' known runs'}.`, 'small'));
  if (Object.keys(cohort.error_categories).length) {
    const errors = el('div', null, 'row');
    for (const [reason, count] of Object.entries(cohort.error_categories)) errors.append(chip(reason + ': ' + count, 'warn'));
    box.append(errors);
  }
  return box;
}

export function renderPerformance(report, retry) {
  const box = el('div', null, 'stack');
  if (!report) {
    append(box, el('p', 'Performance evidence unavailable', 'warn'), el('p', 'Current scores could not be loaded.', 'muted small'));
    const button = el('button', 'Retry performance evidence', 'btn', {type: 'button'});
    button.addEventListener('click', retry);
    box.append(button);
    return box;
  }
  box.append(el('p', `Last ${report.days} days · as of ${fmtDateTime(report.as_of)} ET. Completion describes recorded runs; quality and trading contribution need separate evidence.`, 'muted small'));
  const cards = el('div', null, 'evidence-cards');
  for (const card of report.items) {
    const c = card.current, n = c.counts;
    const node = el('article', null, 'agent-scorecard', {'data-agent': card.name});
    const head = el('div', null, 'agent-head');
    append(head, el('h3', label(card.name)), chip(card.enabled ? 'Enabled' : 'Disabled'));
    append(node, head, el('p', 'Current version: ' + c.prompt_version, 'small muted'));
    const metrics = el('div', null, 'evidence-metrics');
    append(metrics,
      metric('Recorded-run completion', pct(c.reliability.score), `${c.reliability.numerator}/${c.reliability.denominator} terminal attempts`),
      metric('Output quality', 'Not measured', 'Acceptance is a validation proxy'),
      metric('Trading contribution', 'Not measured', 'Requires paired portfolio evidence'));
    node.append(metrics, el('p', n.total ? `${n.total} recorded runs · ${c.observed_days} observed days (ET). ${n.skipped} skipped; ${n.inflight} in progress.` : 'No observations on the current version.', 'small muted'));
    const evidence = el('details', null, 'run');
    evidence.append(el('summary', 'Evidence and limitations'), el('p', card.role, 'small'), el('p', c.reliability.note, 'muted small'), cohortDetails(c));
    node.append(evidence);
    const history = card.cohorts.filter(v => !v.is_current);
    if (history.length) {
      const older = el('details', null, 'run');
      older.append(el('summary', `Other versions and models (${history.reduce((s, v) => s + v.counts.total, 0)} runs)`));
      older.append(el('p', 'Historical cohorts are separate from the current score. Configuration matching and statistical confidence have not been established.', 'muted small'));
      for (const cohort of history) {
        append(older, el('h3', cohort.prompt_version, 'small wrap'), el('p', `Completion ${pct(cohort.reliability.score)} · ${cohort.reliability.numerator}/${cohort.reliability.denominator} terminal attempts · ${cohort.observed_days} days`, 'small'), cohortDetails(cohort));
      }
      node.append(older);
    }
    cards.append(node);
  }
  box.append(cards, el('p', report.note, 'muted small'));
  return box;
}
