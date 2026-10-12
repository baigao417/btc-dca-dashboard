const $ = (id) => document.getElementById(id);
let currentDecision = null;
let busy = false;
const fmtTime = (value) => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '--';
const fmtUsd = (value) => value == null ? '--' : `$${Number(value).toLocaleString('en-US', { maximumFractionDigits: 2 })}`;
const fmtPct = (value) => value == null ? '--' : `${(Number(value) * 100).toFixed(1)}%`;
const actionText = { base_dca: '基础定投', increase_dca: '提高演算倍率', slow_dca: '降低演算倍率', data_insufficient: '数据不足' };
const statusText = { pending: '待记录', confirmed: '已确认', skipped: '已跳过', blocked: '已阻断' };

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...options });
  const data = await response.json();
  if (!response.ok || data.status === 'failed') throw new Error(data.error || 'request_failed');
  return data;
}

function render(data) {
  const { settings, scheduler, snapshot, decision, events } = data;
  $('serviceBadge').textContent = scheduler.alive ? '调度运行中' : '调度已停用';
  $('scheduleValue').textContent = `${settings.schedule_time} ${settings.timezone}`;
  $('nextRun').textContent = fmtTime(data.next_run);
  $('heartbeat').textContent = fmtTime(scheduler.last_heartbeat);
  $('observedAt').textContent = fmtTime(snapshot?.observed_at);
  if (document.activeElement !== $('scheduleInput')) $('scheduleInput').value = settings.schedule_time;
  if (document.activeElement !== $('budgetInput')) $('budgetInput').value = settings.monthly_budget;
  $('timezoneLabel').textContent = settings.timezone;
  $('refreshBtn').disabled = busy || !data.live_enabled;
  $('sourceLabel').textContent = snapshot?.source || '--';
  $('price').textContent = fmtUsd(snapshot?.price);
  $('sma200').textContent = fmtUsd(snapshot?.sma200);
  $('drawdown').textContent = fmtPct(snapshot?.drawdown365);
  $('volatility').textContent = fmtPct(snapshot?.volatility30);
  $('dataNote').textContent = snapshot?.status === 'demo' ? '合成数据，不是真实行情。' : `样本数量：${snapshot?.meta?.samples ?? '--'}`;
  currentDecision = decision;
  $('actionLabel').textContent = decision ? (actionText[decision.action] || decision.action) : '等待数据';
  $('amountLabel').textContent = decision ? `${decision.multiplier}x / ${fmtUsd(decision.planned_amount)}` : '--';
  $('decisionHint').textContent = '研究草稿，确认不会下单。';
  $('decisionStatus').textContent = decision ? (statusText[decision.status] || decision.status) : '--';
  $('confirmBtn').disabled = busy || !decision || decision.status !== 'pending';
  $('skipBtn').disabled = busy || !decision || decision.status !== 'pending';
  $('rationaleList').replaceChildren(...(decision?.rationale || []).map(item => {
    const li = document.createElement('li'); li.textContent = item; return li;
  }));
  $('timeline').replaceChildren(...(events || []).slice(0, 12).map(item => {
    const row = document.createElement('div'); row.className = 'timeline-item';
    const time = document.createElement('time'); time.textContent = fmtTime(item.created_at);
    const text = document.createElement('span'); text.textContent = item.message;
    row.append(time, text); return row;
  }));
}

async function refresh() { render(await api('/api/state')); }
async function perform(task) {
  if (busy) return;
  busy = true;
  $('errorMessage').textContent = '';
  document.querySelectorAll('button').forEach(button => { button.disabled = true; });
  try { await task(); } catch (error) { $('errorMessage').textContent = `请求失败：${error.message}`; }
  finally {
    busy = false;
    document.querySelectorAll('button').forEach(button => { button.disabled = false; });
    try { await refresh(); } catch (error) { $('errorMessage').textContent = `连接失败：${error.message}`; }
  }
}
const run = (mode) => perform(() => api('/api/run', { method: 'POST', body: JSON.stringify({ mode }) }));
$('refreshBtn').addEventListener('click', () => run('live'));
$('demoBtn').addEventListener('click', () => run('demo'));
$('confirmBtn').addEventListener('click', () => perform(() => api(`/api/decisions/${currentDecision.id}/confirm`, { method: 'POST', body: '{}' })));
$('skipBtn').addEventListener('click', () => perform(() => api(`/api/decisions/${currentDecision.id}/skip`, { method: 'POST', body: '{}' })));
$('settingsForm').addEventListener('submit', (event) => {
  event.preventDefault();
  perform(() => api('/api/settings', { method: 'POST', body: JSON.stringify({ schedule_time: $('scheduleInput').value, monthly_budget: $('budgetInput').value }) }));
});
refresh().catch(error => { $('errorMessage').textContent = `连接失败：${error.message}`; });
setInterval(() => { if (!busy) refresh().catch(() => {}); }, 15000);
