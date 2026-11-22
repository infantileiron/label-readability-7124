import { chip, empty, formError, heading, icon } from '../components/common/ui.js';
import { ORDER } from '../constants/enums.js';
import { store } from '../stores/workspace.js';
import { esc, timestamp } from '../utils/format.js';

const REASON = {
  shortage: ['批次人少', 'amber'],
  shared_pool: ['争用同一批人', 'amber'],
  pool_bound: ['合计观众用尽', 'amber'],
  none: ['已达上限', 'green'],
};

function runLabel(run) {
  const stale = run.current_revision !== run.run_revision ? ' · 样本已更新' : '';
  return `复验 #${run.id} · R${run.run_revision} · ${run.confidence}% 区间${stale}`;
}

function picker(runs, rosters, staleRuns = []) {
  const groups = new Map();
  for (const run of runs) {
    if (!groups.has(run.study_id)) groups.set(run.study_id, []);
    groups.get(run.study_id).push(run);
  }
  const rows = [...groups.values()].map(group => {
    const study = group[0];
    const options = group.map(run =>
      `<option value="${run.id}" ${run.id === group[0].id ? 'selected' : ''}>${esc(runLabel(run))}</option>`).join('');
    return `<label class="roster-run"><span><b class="mono">${esc(study.study_code)}</b> ${esc(study.study_title)}<small>${esc(study.station)} · 当前样本 R${study.current_revision}</small></span>
      <select name="run-${study.study_id}"><option value="">不参加本次合并</option>${options}</select></label>`;
  }).join('');
  const waiting = spentRuns(staleRuns, groups);
  const previous = rosters.length
    ? `<label>保留上一份名单的批次（同样多时优先留在原批次）
        <select name="previous_id"><option value="">不使用（首次名单）</option>${rosters.map(row =>
          `<option value="${row.id}">名单 #${row.id} · ${row.batches} 批 · 每人每读序 ${row.k} 人 · ${timestamp(row.created_at)}</option>`).join('')}</select></label>`
    : '';
  return `<form data-form="roster"><div class="section-title"><h2>选择要一起复验的批次</h2>${chip(`${groups.size} 个批次可选`)}</div>
    <p class="muted">每批选一次已完成的复验。只有两次都测到的观众可以入组，每人只进一批，每批两个读序人数相同，取能一起达到的最大值。答对与否不影响选人。</p>
    <div class="roster-runs">${rows || '<p class="muted">还没有已完成的复验运行。请先在复验分析中提交并等待完成。</p>'}</div>
    ${waiting}${previous}${formError}<div class="form-actions"><button class="primary" type="submit" ${rows ? '' : 'disabled'}>生成合并名单</button></div></form>`;
}

function spentRuns(staleRuns, groups) {
  const pending = staleRuns.filter(run => run.current_revision !== run.run_revision
    && !groups.has(run.study_id));
  if (!pending.length) return '';
  const codes = [...new Map(pending.map(run => [run.study_id, run.study_code])).values()];
  return `<p class="warning">${codes.map(esc).join('、')} 的样本已更新，现有复验是旧修订，需先在复验分析换用新修订重算才能加入。</p>`;
}

function findings(roster) {
  const rows = roster.findings || [];
  if (!rows.length) return '';
  const names = { shortage: '批次人少', contention: '争用同一批人', pool_bound: '合计观众用尽',
    stale: '样本已更新', limit: '已达上限' };
  return `<section class="sheet details-sheet roster-findings"><div class="section-title"><h2>人数上不去的原因</h2></div>
    <ul>${rows.map(note => `<li class="finding ${esc(note.kind)}"><b>${esc(names[note.kind] || note.kind)}</b><span>${esc(note.text)}</span></li>`).join('')}</ul></section>`;
}

function detail(roster) {
  const result = roster.result;
  const [label, style] = REASON[result.reason] || ['已达上限', 'green'];
  const perBatch = roster.items.map(item => {
    const row = result.per_study.find(entry => entry.study_id === item.study_id);
    return `<tr><td class="mono">${esc(item.code)}</td><td class="mono">#${item.run_id}</td><td>R${item.run_revision}</td>
      <td>${row.selected_old} / ${row.eligible_old}</td><td>${row.selected_new} / ${row.eligible_new}</td></tr>`;
  }).join('');
  const retention = result.retention;
  const keptNote = retention.previous
    ? `<p class="muted">与上一份名单相比：留在原批次 ${retention.kept} 人，换批次 ${retention.moved} 人，未入选 ${retention.dropped} 人，新入选 ${retention.added} 人。</p>`
    : '';
  const stale = roster.stale?.length
    ? `<p class="warning">${roster.stale.map(row => `${esc(row.code)} 已更新到 R${row.current_revision}，本名单用的是 R${row.run_revision}`).join('；')}。源数据更新后请换用新复验重算。</p>`
    : '';
  return `<section class="sheet roster-summary"><div class="section-title"><h2>名单 #${roster.id}</h2>${chip(label, style)}</div>
    <div class="roster-headline"><div><b>${result.k}</b><span>每批每读序</span></div><div><b>${result.k * 2}</b><span>每批合计</span></div><div><b>${result.people}</b><span>全体入选</span></div><div><b>${result.batches}</b><span>批次数</span></div></div>
    <dl class="facts"><div><dt>两个读序</dt><dd>先读旧版 ${result.k} 人 · 先读样张 ${result.k} 人（每批）</dd></div><div><dt>可选观众合计</dt><dd class="mono">${result.visitor_pool} 人</dd></div><div><dt>生成时间</dt><dd>${timestamp(roster.created_at)}</dd></div><div><dt>输入校验值</dt><dd><code title="${esc(roster.checksum)}">${esc(roster.checksum.slice(0, 16))}…</code></dd></div></dl>
    ${keptNote}
    ${stale}
    <div class="actions roster-actions"><button data-download="json" data-roster="${roster.id}">${icon('export')} 名单 JSON</button><button data-download="members.csv" data-roster="${roster.id}">${icon('export')} 名单 CSV（含来源复验）</button></div>
  </section>
  ${findings(roster)}
  <div class="result-grid evidence-grid">
    <section class="sheet details-sheet"><div class="section-title"><h2>各批入选与可选人数</h2></div>
      <table><thead><tr><th>批次</th><th>来源复验</th><th>修订</th><th>先读旧版 入选/可选</th><th>先读样张 入选/可选</th></tr></thead><tbody>${perBatch}</tbody></table></section>
    <section class="sheet details-sheet"><div class="section-title"><h2>入选观众</h2>${chip(`${roster.members.length} 人`)}</div>
      <div class="missing-scroll"><table><thead><tr><th>观众编号</th><th>批次</th><th>来源复验</th><th>读序</th></tr></thead><tbody>${roster.members.map(row =>
        `<tr><td class="mono">${esc(row.visitor_code)}</td><td class="mono">${esc(row.study_code)}</td><td class="mono">#${row.run_id}</td><td>${ORDER[row.reading_order]}</td></tr>`).join('')}</tbody></table></div></section>
  </div>`;
}

export function rostersPage() {
  const { user, rosterData, roster } = store;
  const head = heading('跨批复验', '合并名单', '每批选一次已完成的复验，把两次都测到的观众分到各批，使每批两个读序人数相同且总数最大。');
  if (!rosterData) return head + empty('正在载入', '请稍候。');
  const batches = new Set((rosterData.runs || []).map(run => run.study_id)).size;
  const blocked = user.role === 'analyst' && batches < 2;
  const pickerBlock = blocked
    ? `<p class="muted">至少需要两个已完成且各有完整配对的复验运行。当前 ${batches} 个批次有可用运行。请先在各批复验分析中提交并等待完成。</p>`
    : picker(rosterData.runs || [], rosterData.rosters, rosterData.stale_runs || []);
  const history = rosterData.rosters.length
    ? `<section class="sheet history"><div class="section-title"><h2>名单记录</h2><span class="muted">${rosterData.rosters.length} 份</span></div>
        <div class="run-list">${rosterData.rosters.map(row => `<a href="#rosters/${row.id}"><div><b class="mono">名单 #${row.id}</b><small>${row.batches} 批 · 每人每读序 ${row.k} 人 · ${row.people} 人入选 · ${timestamp(row.created_at)}</small></div>${chip((REASON[row.reason] || [''])[0], (REASON[row.reason] || [])[1])}${icon('arrow')}</a>`).join('')}</div></section>`
    : '';
  const body = `<div class="run-grid"><section class="sheet settings roster-settings"><div class="section-title"><h2>新建或重算</h2>${roster ? chip(`当前 名单 #${roster.id}`) : ''}</div>${user.role === 'analyst' ? pickerBlock : '<p class="muted">由分析员生成合并名单。</p>'}</section>${history}</div>`;
  const blank = !rosterData.rosters.length && user.role !== 'analyst' && !roster
    ? empty('暂无合并名单', '分析员生成后会显示在这里。') : '';
  const summary = roster ? detail(roster) : '';
  return `${head}${summary}${body}${blank}`;
}
