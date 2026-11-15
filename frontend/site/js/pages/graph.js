import { api } from '../api.js';
import { ENUMS, badgeTone, cycleTone } from '../config.js';
import { chips, cyclePath, drawer, emptyState, errorBox, errorMessage, esc, filterBar, fmtTime, impactRows, kv, table } from '../ui.js';

const state = { kind: '', recordStatus: '' };

function edgeLabel(edge) {
  return `${edge.from} → ${edge.to}`;
}

function timelineRows(events) {
  if (!events || !events.length) return '<p class="muted">还没有处置事件。</p>';
  return `<div class="list-tight">${events
    .map(
      (event) => `<div class="impact-row">
        <span><span class="badge ${event.event === 'resolved' ? 'ok' : event.event === 'reopened' ? 'warn' : 'info'}">${esc(
          ENUMS.cycleEvent[event.event] || event.event,
        )}</span> <span class="mono tiny">${esc(event.actor_email)}</span></span>
        <span class="tiny muted">${esc(event.run || '—')} · ${fmtTime(event.created_at)}</span>
      </div>`,
    )
    .join('')}</div>`;
}

async function openCycleRecord(view, ctx, recordNo) {
  let record;
  try {
    record = await api.get(`/api/cycles/${encodeURIComponent(recordNo)}`);
  } catch (err) {
    view.insertAdjacentHTML('beforeend', errorBox(errorMessage(err)));
    return;
  }
  const resolution = record.resolution || {};
  const brokenEdges = resolution.broken_edges || [];
  drawer({
    title: `环处置记录 · ${record.record_no}`,
    subtitle: `${record.path.join(' → ')} · ${ENUMS.cycleStatus[record.status] || record.status}`,
    body: `
      ${cyclePath(record.path)}
      ${kv([
        ['状态', `<span class="badge ${cycleTone(record.status)}">${esc(ENUMS.cycleStatus[record.status] || record.status)}</span>`],
        ['环上指标', esc((record.metric_names || []).join(' / ') || '—')],
        ['成员编码', `<span class="mono">${esc(record.members.join(' , '))}</span>`],
        ['严重度', `<span class="badge ${badgeTone(record.severity)}">${esc(ENUMS.severity[record.severity] || record.severity)}</span>`],
        ['影响分', `<span class="mono">${esc(record.score)}</span>（核心报表 ×3）`],
        ['首次发现', `${esc(record.first_seen_run || '—')} · ${fmtTime(record.first_seen_at)}`],
        ['最后确认', `${esc(record.last_seen_run || '—')} · ${fmtTime(record.last_seen_at)}`],
        ['内容版本', `<span class="mono">${esc(record.revision_count)}</span> 版（只追加不覆盖）`],
      ])}
      <div class="dsl-box">
        <span class="label">环内引用边（识别同一问题的依据）</span>
        ${chips(record.edges.map(edgeLabel), { withIndex: false })}
      </div>
      ${
        record.status === 'resolved'
          ? `<div class="dsl-box">
              <span class="label">闭环结果</span>
              ${kv([
                ['闭环运行', `${esc(record.resolved_run || '—')} · ${fmtTime(record.resolved_at)}`],
                ['闭环操作人', `<span class="mono">${esc(record.resolved_by || '—')}</span>`],
              ])}
              <p class="section-note">${esc(resolution.note || '环已不存在。')}</p>
              ${brokenEdges.length ? chips(brokenEdges.map(edgeLabel), { withIndex: false }) : ''}
            </div>`
          : ''
      }
      <div class="dsl-box">
        <span class="label">当时的下游报表影响面</span>
        ${impactRows(record.impact || {})}
      </div>
      <div class="dsl-box">
        <span class="label">处置时间线</span>
        ${timelineRows(record.events)}
      </div>
      <div class="dsl-box">
        <span class="label">内容版本（${esc((record.revisions || []).length)} 版）</span>
        <div class="list-tight">${(record.revisions || [])
          .map(
            (revision) => `<div class="impact-row">
              <span>v${esc(revision.revision)} · ${esc(revision.note || '—')}</span>
              <span class="tiny muted">${esc(revision.run || '—')} · 影响分 ${esc(revision.score)} · ${fmtTime(revision.created_at)}</span>
            </div>`,
          )
          .join('')}</div>
      </div>
      <div class="dsl-box">
        <span class="label">建议动作</span>
        <p class="section-note">${esc(record.suggestion)}</p>
      </div>`,
  });
}

function openNode(view, ctx, node, edges) {
  const outgoing = edges.filter((edge) => edge.from === node.code && edge.kind === 'metric');
  drawer({
    title: `${node.name} · ${node.code}`,
    subtitle: `${node.domain_label} / 版本 v${node.version} / 下游报表 ${node.report_count} 张`,
    body: `
      ${kv([
        ['解析状态', `<span class="badge ${node.parse_status === 'ok' ? 'ok' : 'danger'}">${esc(ENUMS.parseStatus[node.parse_status] || node.parse_status)}</span>`],
        ['分级', esc(ENUMS.metricTier[node.tier] || node.tier)],
        ['是否在环上', node.in_cycle ? '<span class="badge danger">在环上</span>' : '否'],
        ['依赖指标数', esc(node.dep_count)],
        ['下游报表数', esc(node.report_count)],
      ])}
      <div class="dsl-box">
        <span class="label">该指标引用的指标（出边）</span>
        ${outgoing.length ? chips(outgoing.map((edge) => edge.to)) : '<p class="muted">没有引用其他指标。</p>'}
      </div>
      <div class="dsl-box">
        <span class="label">消费该指标的报表</span>
        ${chips(node.reports || [])}
      </div>`,
  });
}

export const graphPage = {
  async render(view, ctx) {
    let graph;
    let deps;
    let ledger;
    try {
      graph = await api.get('/api/graph');
      deps = await api.get('/api/dependencies', { kind: state.kind, limit: 200 });
      ledger = await api.get('/api/cycles', { status: state.recordStatus, limit: 200 });
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => graphPage.render(view, ctx));
      return;
    }
    const stats = graph.stats;
    const openCycles = graph.cycles;
    view.innerHTML = `
      ${
        graph.stale
          ? `<div class="error-box" style="margin-bottom:14px"><span>依赖图自上次校验后已变化：台账状态仍以最近一次校验为准，请跑一次校验刷新处置记录。</span></div>`
          : ''
      }
      <section class="panel">
        <div class="panel-head"><h2>依赖图概览</h2><span class="meta">algorithm B · tarjan</span></div>
        <div class="panel-body">
          <div class="stat-strip">
            <div class="stat"><span class="k">NODES</span><span class="v">${esc(stats.nodes)}</span><span class="hint">指标节点</span></div>
            <div class="stat"><span class="k">DEP EDGES</span><span class="v">${esc(stats.dep_edges)}</span><span class="hint">指标 → 指标</span></div>
            <div class="stat"><span class="k">REPORT EDGES</span><span class="v">${esc(stats.report_edges)}</span><span class="hint">报表引用</span></div>
            <div class="stat"><span class="k">CYCLES</span><span class="v">${esc(stats.cycles)}</span><span class="hint">未闭环 · 环上节点 ${esc(stats.cycle_nodes)} 个</span></div>
          </div>
        </div>
      </section>
      <section class="panel">
        <div class="panel-head"><h2>依赖环 · 未闭环处置</h2><span class="meta">open records</span></div>
        <div class="panel-body">
          ${
            openCycles.length
              ? `<div class="list-tight">${openCycles
                  .map(
                    (cycle, index) => `<div class="impact-row" data-cycle="${index}" style="cursor:pointer;align-items:flex-start">
                      <div style="display:grid;gap:6px">
                        <span class="index-tag">${esc(cycle.record_no)} · 长度 ${esc(cycle.length)}</span>
                        ${cyclePath(cycle.path)}
                        <span class="muted tiny">首次发现 ${esc(cycle.first_seen_run || '—')} · 最后确认 ${fmtTime(cycle.last_seen_at)}</span>
                      </div>
                      <div style="display:flex;gap:8px;align-items:center">
                        <span class="badge ${badgeTone(cycle.severity)}">${esc(ENUMS.severity[cycle.severity] || cycle.severity)}</span>
                        <span class="mono tiny">${esc(cycle.score)} 分</span>
                        <button class="btn sm ghost" data-open-cycle="${esc(cycle.record_no)}">展开</button>
                      </div>
                    </div>`,
                  )
                  .join('')}</div>`
              : emptyState({ title: '没有未闭环的依赖环', hint: '台账中所有环记录均已闭环，或还没有跑过校验。' })
          }
        </div>
      </section>
      <section class="panel">
        <div class="panel-head"><h2>环处置台账</h2><span class="meta">${ledger.total} 条记录 · 只追加不覆盖</span></div>
        ${filterBar('cycle-record-filters', [
          { label: '状态', name: 'status', value: state.recordStatus, options: [{ value: '', label: '全部状态' }, ...Object.entries(ENUMS.cycleStatus).map(([value, label]) => ({ value, label }))] },
        ])}
        ${table({
          rows: ledger.items,
          columns: [
            { key: 'record_no', label: '记录号', className: 'mono', render: (row) => `<span class="index-tag">${esc(row.record_no)}</span>` },
            { key: 'path', label: '环路径', render: (row) => `<span class="mono tiny">${esc(row.path.join(' → '))}</span>` },
            {
              key: 'status',
              label: '状态',
              render: (row) => `<span class="badge ${cycleTone(row.status)}">${esc(ENUMS.cycleStatus[row.status] || row.status)}</span>`,
            },
            {
              key: 'severity',
              label: '严重度',
              render: (row) => `<span class="badge ${badgeTone(row.severity)}">${esc(ENUMS.severity[row.severity] || row.severity)}</span>`,
            },
            { key: 'score', label: '影响分', className: 'num' },
            { key: 'first_seen_at', label: '首次发现', render: (row) => `<span class="tiny">${esc(row.first_seen_run || '—')}<br/>${fmtTime(row.first_seen_at)}</span>` },
            { key: 'last_seen_at', label: '最后确认', render: (row) => `<span class="tiny">${esc(row.last_seen_run || '—')}<br/>${fmtTime(row.last_seen_at)}</span>` },
            {
              key: 'resolution',
              label: '闭环结果',
              render: (row) =>
                row.status === 'resolved'
                  ? `<span class="tiny">${esc((row.resolution?.broken_edges || []).map(edgeLabel).join('；') || '环已不存在')}<br/><span class="muted">${esc(
                      row.resolved_by || '—',
                    )} · ${fmtTime(row.resolved_at)}</span></span>`
                  : '<span class="muted tiny">待闭环</span>',
            },
          ],
          empty: { title: '还没有环处置记录', hint: '跑一次校验后，检出的每个基本环都会建档。' },
        })}
      </section>
      <section class="panel">
        <div class="panel-head"><h2>指标节点</h2><span class="meta">${graph.nodes.length} 个</span></div>
        ${table({
          rows: graph.nodes,
          columns: [
            { key: 'code', label: '索引号', className: 'mono', render: (row) => `<span class="index-tag">${esc(row.code)}</span>` },
            { key: 'name', label: '指标名称', render: (row) => `<b>${esc(row.name)}</b>` },
            { key: 'domain_label', label: '业务域' },
            { key: 'dep_count', label: '出边', className: 'num' },
            { key: 'report_count', label: '报表', className: 'num' },
            {
              key: 'in_cycle',
              label: '状态',
              render: (row) => (row.in_cycle ? '<span class="badge danger">在环上</span>' : '<span class="badge ok">正常</span>'),
            },
            {
              key: 'parse_status',
              label: '解析',
              render: (row) => `<span class="badge ${row.parse_status === 'ok' ? 'ok' : 'danger'}">${esc(ENUMS.parseStatus[row.parse_status] || row.parse_status)}</span>`,
            },
          ],
          empty: { title: '没有节点', hint: '先 seed 或注册指标。' },
        })}
      </section>
      <section class="panel">
        <div class="panel-head"><h2>依赖边清单</h2><span class="meta">${deps.total} 条</span></div>
        ${filterBar('dep-filters', [
          { label: '边类型', name: 'kind', value: state.kind, options: [{ value: '', label: '全部类型' }, ...Object.entries(ENUMS.dependencyKind).map(([value, label]) => ({ value, label }))] },
        ])}
        ${table({
          rows: deps.items,
          columns: [
            { key: 'from_metric_code', label: '起点指标', className: 'mono' },
            { key: 'kind', label: '边类型', render: (row) => `<span class="badge info">${esc(ENUMS.dependencyKind[row.kind] || row.kind)}</span>` },
            { key: 'to_ref', label: '终点 / 字段 / 报表', className: 'mono' },
            { key: 'weight', label: '权重', className: 'num' },
            { key: 'note', label: '说明' },
          ],
          empty: { title: '还没有依赖边', hint: '跑一次校验运行会按当前口径重建依赖边。' },
        })}
      </section>`;

    view.querySelector('#dep-filters')?.addEventListener('change', (event) => {
      state.kind = event.target.value;
      graphPage.render(view, ctx);
    });
    view.querySelector('#cycle-record-filters')?.addEventListener('change', (event) => {
      state.recordStatus = event.target.value;
      graphPage.render(view, ctx);
    });
    view.querySelectorAll('[data-open-cycle]').forEach((button) => {
      button.addEventListener('click', (event) => {
        event.stopPropagation();
        openCycleRecord(view, ctx, button.dataset.openCycle);
      });
    });
    view.querySelectorAll('table.data tbody tr').forEach((row) => {
      row.addEventListener('click', () => {
        const recordNo = row.querySelector('.index-tag')?.textContent;
        const record = ledger.items.find((item) => item.record_no === recordNo);
        if (record) {
          openCycleRecord(view, ctx, record.record_no);
          return;
        }
        const code = row.querySelector('.index-tag')?.textContent;
        const node = graph.nodes.find((item) => item.code === code);
        if (node) openNode(view, ctx, node, graph.edges);
      });
    });
  },
};

export default graphPage;
