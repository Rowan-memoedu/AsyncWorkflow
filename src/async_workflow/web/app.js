'use strict';
const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
let recordings = [], selected = params.get('recording') || '', shown = 50, request = 0, detailRequest = 0;
let detailSignature = '', listSignature = '', listScroll = 0, searchTimer, toastTimer;
const kinds = {command:'执行命令', workflow_feedback:'录音工作流维护', record:'专门笔记', todo:'待办记录', shopping:'购物清单', information:'信息清单', inspiration:'想法笔记', needs_clarification:'待分类记录'};
const dateFormat = new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai', dateStyle:'short', timeStyle:'medium'});
const dateOnly = new Intl.DateTimeFormat('en-CA', {timeZone:'Asia/Shanghai', year:'numeric', month:'2-digit', day:'2-digit'});
function date(value) { return value ? dateFormat.format(new Date(value)) : '录音时间未识别'; }
function el(tag, text, cls) { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (cls) node.className = cls; return node; }
function badge(status, label) { const node = el('span', label, 'badge'); node.dataset.status = status; return node; }
function toast(message) { $('toast').textContent = message; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, 4000); }
async function copy(value) { try { await navigator.clipboard.writeText(value); toast('链接已复制'); } catch { toast('复制未成功，可选中下方 Thread ID 复制。'); } }
function saveURL() {
  const next = new URLSearchParams();
  if ($('search').value.trim()) next.set('q', $('search').value.trim());
  if (selected) next.set('recording', selected);
  if ($('status').value) next.set('status', $('status').value);
  if ($('period').value !== 'all') next.set('period', $('period').value);
  history.replaceState(null, '', next.size ? '?' + next : location.pathname);
}
function threads(values, container) {
  if (!values.length) { container.append(el('p', '未建立或投递 Thread。', 'muted')); return; }
  for (const thread of values) {
    const block = el('div', undefined, 'thread');
    block.append(el('strong', thread.role), el('p', thread.id, 'path muted'));
    const actions = el('div', undefined, 'actions');
    const link = el('a', '在 Codex 中打开', 'thread-link'); link.href = thread.url;
    const button = el('button', '复制链接'); button.addEventListener('click', () => copy(thread.url));
    actions.append(link, button); block.append(actions); container.append(block);
  }
}
function disclosure(title, body, open = false) {
  const details = el('details'); details.open = open;
  details.append(el('summary', title), el('pre', body || '尚未生成。', 'prose'));
  return details;
}
function section(title) { const node = el('section', undefined, 'detail-section'); node.append(el('h3', title)); return node; }
function renderList() {
  const status = $('status').value, period = $('period').value;
  const today = dateOnly.format(new Date());
  const filtered = recordings.filter(row => (!status || row.status === status) && (period === 'all' ||
    (row.recorded_at && (period === 'today' ? dateOnly.format(new Date(row.recorded_at)) === today :
      new Date(row.recorded_at).getTime() >= Date.now() - 7 * 86400000))));
  $('count').textContent = `找到 ${filtered.length} 条录音 · 新录音在前`;
  const signature = JSON.stringify([filtered.slice(0, shown), selected, shown]);
  if (signature === listSignature) return;
  listSignature = signature;
  const list = document.createDocumentFragment();
  for (const row of filtered.slice(0, shown)) {
    const button = el('button', undefined, 'recording');
    button.setAttribute('aria-pressed', String(row.id === selected));
    button.setAttribute('aria-label', row.name + '，' + row.status_label);
    button.append(el('span', row.name, 'recording-title'));
    const meta = el('span', undefined, 'recording-meta');
    meta.append(badge(row.status, row.status_label), el('span', row.threads.length ? `${row.threads.length} 个 Thread` : '无 Thread', 'small muted'));
    meta.append(el('span', date(row.recorded_at), 'small muted'));
    button.append(meta);
    if (row.match_excerpt) button.append(el('span', row.match_excerpt, 'excerpt'));
    button.addEventListener('click', () => { selected = row.id; detailSignature = ''; saveURL(); renderList(); loadDetail(true); });
    list.append(button);
  }
  if (!filtered.length) {
    const empty = el('div', undefined, 'empty'); empty.append(el('h3', '没有匹配的录音'), el('p', '试试更短的正文关键词，或切换到全部日期、全部状态。', 'muted')); list.append(empty);
  }
  $('list').replaceChildren(list); $('more').hidden = filtered.length <= shown;
}
function renderDetail(row) {
  const content = document.createDocumentFragment();
  const top = el('div', undefined, 'detail-top');
  const back = el('button', '← 返回录音列表', 'back'); back.onclick = () => {
    document.body.classList.remove('detail-open');
    window.scrollTo(0, listScroll);
  };
  top.append(back, el('p', row.type, 'eyebrow'), el('h2', row.name), badge(row.status, row.status_label),
    el('p', date(row.recorded_at) + (row.duration ? ` · ${Math.round(row.duration / 60)} 分钟` : ''), 'muted small'));
  content.append(top);
  const routeSection = section('内容如何处理');
  if (!row.parts.length) {
    const descriptions = {retained:'这条录音按现有准入规则保留，未进入自动归档。', no_content:'这条录音只有未说完的开场说明，没有实际待办正文；已复核，不创建空笔记或执行任务。', blocked:'处理尚未完成，后台会按周期继续恢复。', pending:'录音已在本地，等待扫描或分析。', running:'正在转写、整理或归档，完成后会显示分块去处。'};
    routeSection.append(el('p', descriptions[row.status] || '尚未生成内容分块。', 'muted'));
    if (row.eligibility) routeSection.append(el('p', '准入结果：' + row.eligibility, 'small muted'));
  }
  row.parts.forEach((part, index) => {
    const block = el('div', undefined, 'part');
    const heading = el('div', undefined, 'part-heading');
    heading.append(el('strong', `${index + 1}. ${kinds[part.kind] || part.kind}`), el('span', part.status_label, 'small muted'));
    block.append(heading);
    const routing = part.routing || {};
    const destination = [routing.focus_document_title, routing.focus_branch_title].filter(Boolean).join(' / ') ||
      routing.path || routing.title || [routing.type, routing.category, routing.software].filter(Boolean).join(' / ') || part.destination;
    block.append(el('p', '去处：' + (destination || (part.threads.length ? '下方对应 Thread' : '待确认实际归档位置'))));
    if (routing.parent_id) block.append(el('p', '目标节点：' + routing.parent_id, 'small muted path'));
    if (part.note_id) block.append(el('p', '笔记节点：' + part.note_id, 'small muted path'));
    if (part.reason) block.append(el('p', part.reason, 'small muted'));
    if (part.markdown) block.append(disclosure('整理后的正文', part.markdown, row.parts.length === 1));
    if (part.raw_text) block.append(disclosure('这一段的录音原文', part.raw_text));
    if (part.error) block.append(el('p', String(part.error), 'notice'));
    if (part.threads.length) threads(part.threads, block);
    routeSection.append(block);
  });
  content.append(routeSection);
  const threadSection = section('对应 Thread'); threads(row.threads, threadSection); content.append(threadSection);
  const original = section('完整转写');
  original.append(row.transcript ? disclosure('展开原始转写', row.transcript, Boolean($('search').value.trim())) : el('p', '尚无可用转写，当前只能按文件名搜索这条录音。', 'muted'));
  content.append(original);
  const process = section('处理记录');
  if (row.asr.attempt) process.append(el('p', `转写已尝试 ${row.asr.attempt} 次`));
  if (row.asr.failure_code) process.append(el('p', row.asr.failure_code, 'notice'));
  for (const run of row.runs) process.append(disclosure(`${run.state} · 已运行 ${run.run_count ?? '—'} 次`, run.message || '暂无错误信息。'));
  if (row.sources?.length) process.append(disclosure('源文件位置', row.sources.join('\n')));
  content.append(process); $('detail').replaceChildren(content);
}
async function loadDetail(userSelected = false) {
  if (!selected) return;
  const current = ++detailRequest, identity = selected;
  if (userSelected) {
    listScroll = window.scrollY;
    document.body.classList.add('detail-open'); $('detail').setAttribute('aria-busy', 'true');
    if (innerWidth < 960) $('detail').scrollIntoView({block: 'start'});
  }
  try {
    const response = await fetch('api/recordings/' + encodeURIComponent(identity));
    if (!response.ok) throw new Error('详情暂时无法读取，请刷新重试。');
    const row = await response.json();
    if (current !== detailRequest || identity !== selected) return;
    const signature = JSON.stringify(row);
    if (signature !== detailSignature) { renderDetail(row); detailSignature = signature; }
  } catch (error) { if (current === detailRequest) toast(error.message); }
  finally { if (current === detailRequest) $('detail').removeAttribute('aria-busy'); }
}
async function refresh() {
  const current = ++request; $('refresh').disabled = true; $('refresh').textContent = '正在读取…';
  try {
    const response = await fetch('api/recordings?q=' + encodeURIComponent($('search').value.trim()));
    if (!response.ok) throw new Error(response.status === 401 ? '需要重新登录后查看。' : '查看服务暂不可用，请稍后刷新。');
    const data = await response.json(); if (current !== request) return;
    $('processed').textContent = data.last_processed_at ? `最近处理：${date(data.last_processed_at)}` : '最近处理：暂无处理记录';
    recordings = data.recordings; $('total').textContent = data.total;
    $('verified').textContent = data.summary.verified || 0; $('blocked').textContent = data.summary.blocked || 0;
    const checked = data.recovery.checked_at;
    const overdue = checked && Date.now() - new Date(checked).getTime() > 2 * data.interval_seconds * 1000;
    $('health').textContent = checked ? `每 30 分钟扫描并恢复 · 最近检查 ${date(checked)}${overdue ? ' · 检查已延迟' : ''}` : '每 30 分钟扫描并恢复 · 等待恢复服务首次检查';
    $('updated').textContent = `显示更新于 ${date(data.updated_at)} · ${data.total - data.without_transcript} 条可检索正文`;
    const warnings = [];
    if (data.remote) {
      $('location').textContent = '云端';
      $('source-label').textContent = '录音去向 · 按录音日期排列';
      $('health').textContent = `电脑在线时约每分钟同步 · 最后同步 ${date(data.synced_at)}`;
      if (Date.now() - new Date(data.synced_at).getTime() > 180000)
        warnings.push('电脑可能离线或同步暂不可用，当前显示最后一次同步的记录；新录音仍需等待电脑处理。');
    }
    $('error').classList.toggle('info', data.prefect_available && !data.errors.length);
    if (!data.prefect_available) warnings.push('无法连接调度服务；已保存的写入收据仍可查看，未完成录音的实时状态暂不可核对。');
    if (data.errors.length) warnings.push(`${data.errors.length} 条读取异常，部分记录暂不可见。`);
    if ($('search').value.trim() && data.without_transcript) warnings.push(`${data.without_transcript} 条录音尚无可搜索正文（包括未纳入的历史录音）；这些录音仍可按文件名查找。`);
    $('error').textContent = warnings.join(' '); $('error').hidden = !warnings.length;
    renderList(); if (selected) loadDetail();
  } catch (error) {
    if (current !== request) return;
    $('error').textContent = error.message || '无法连接查看服务，请检查网络后刷新。'; $('error').hidden = false;
  } finally { if (current === request) { $('refresh').disabled = false; $('refresh').textContent = '刷新状态'; } }
}
$('search').value = params.get('q') || '';
$('status').value = params.get('status') || '';
$('period').value = params.get('period') || 'all';
$('search').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { shown = 50; saveURL(); refresh(); }, 250); });
for (const id of ['status', 'period']) $(id).addEventListener('change', () => { shown = 50; saveURL(); renderList(); });
$('refresh').onclick = refresh;
$('more').onclick = () => { shown += 50; renderList(); };
document.addEventListener('keydown', event => { if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); $('search').focus(); } });
refresh(); setInterval(() => { if (!document.hidden) refresh(); }, 30000);
