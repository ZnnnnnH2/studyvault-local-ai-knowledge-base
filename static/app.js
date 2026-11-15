const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const state = {view: 'library', selected: null, offset: 0, limit: 20, total: 0, items: [], cursor: -1,
  listSeq: 0, detailSeq: 0, summarySeq: 0, reviewSeq: 0, statsSeq: 0, loading: false,
  searchQuery: '', historySeq: 0, historyOffset: 0};

async function api(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let message = `请求失败（${response.status}）`, detail = null;
    try {
      const error = await response.json();
      detail = error.detail ?? null;
      if (typeof detail === 'string') message = detail;
      else if (detail && typeof detail.message === 'string') message = detail.message;
    } catch {}
    const problem = new Error(message);
    problem.status = response.status;
    problem.detail = detail;
    throw problem;
  }
  return response.json();
}
function post(url, payload) {
  return api(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
    ...(payload === undefined ? {} : {body: JSON.stringify(payload)})});
}
function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'}[c]));
}
function highlightHtml(value, query = state.searchQuery) {
  const text = String(value ?? ''), needle = String(query || '');
  if (!needle) return escapeHtml(text);
  let output = '', cursor = 0, index;
  while ((index = text.toLowerCase().indexOf(needle.toLowerCase(), cursor)) !== -1) {
    output += escapeHtml(text.slice(cursor, index)) + '<mark>' + escapeHtml(text.slice(index, index + needle.length)) + '</mark>';
    cursor = index + needle.length;
  }
  return output + escapeHtml(text.slice(cursor));
}
function toast(text) {
  $('#toast').textContent = text;
  $('#toast').classList.add('show');
  setTimeout(() => $('#toast').classList.remove('show'), 2600);
}
function showError(error) { $('#app-status').textContent = error.message || '操作失败，请重试。'; }
function run(action) { return async event => { try { $('#app-status').textContent = ''; await action(event); } catch (error) { showError(error); } }; }
function updateSelection() {
  $$('.conversation-item').forEach(item => {
    const selected = item.dataset.id === String(state.selected);
    item.classList.toggle('active', selected);
    item.setAttribute('aria-pressed', String(selected));
  });
}
function clearDetail() {
  state.selected = null;
  state.detailSeq++;
  $('#detail').classList.add('empty');
  $('#detail').innerHTML = '<div class="empty-state">← 挑一条，右边读全文</div>';
  updateSelection();
}
function conversationItemHtml(item) {
  const unread = !item.last_opened_at;
  const fav = Number(item.is_favorite) === 1;
  const readMark = unread
    ? '<span class="tag tag-new">未读</span>'
    : (item.last_read_seq ? `<span class="tag tag-read">读到 #${item.last_read_seq}</span>` : '');
  const openMark = item.unresolved_count > 0 ? `<span class="tag tag-open">${item.unresolved_count} 条没解决</span>` : '';
  return `<div class="conversation-item${unread ? ' unread' : ''}" role="button" tabindex="0" data-id="${item.conversation_id}" data-match="${item.matched_message_id || ''}" aria-pressed="false">
    <span class="dot" aria-hidden="true"></span>
    <button class="fav-star${fav ? ' on' : ''}" type="button" aria-pressed="${fav}" aria-label="${fav ? '取消收藏' : '收藏'}" title="f">${fav ? '★' : '☆'}</button>
    <strong>${highlightHtml(item.title)}</strong>
    <span class="meta"><span>${escapeHtml(item.course_name)}</span><span>${escapeHtml(item.platform_name)}</span><span>${item.message_count} 条</span>${readMark}${openMark}</span>
    ${item.match_snippet ? `<span class="match-snippet">#${item.match_seq_no}：${highlightHtml(item.match_snippet)}</span>` : ''}
  </div>`;
}
async function toggleFavorite(id, makeFavorite, star) {
  await post('/api/library/favorite', {conversation_id: Number(id), is_favorite: makeFavorite});
  if (star) {
    star.classList.toggle('on', makeFavorite);
    star.setAttribute('aria-pressed', String(makeFavorite));
    star.setAttribute('aria-label', makeFavorite ? '取消收藏' : '收藏');
    star.textContent = makeFavorite ? '★' : '☆';
  }
  const item = (state.items || []).find(i => String(i.conversation_id) === String(id));
  if (item) item.is_favorite = makeFavorite ? 1 : 0;
  if ($('#favorites-only')?.checked) await loadConversations();
  toast(makeFavorite ? '已收藏' : '已取消收藏');
}
function cursorRows() { return $$('#conversation-list .conversation-item'); }
function moveCursor(delta) {
  const rows = cursorRows();
  if (!rows.length) { state.cursor = -1; return; }
  state.cursor = Math.max(0, Math.min(rows.length - 1, (state.cursor < 0 ? 0 : state.cursor) + delta));
  rows.forEach((row, index) => row.classList.toggle('cursor', index === state.cursor));
  if (delta !== 0) rows[state.cursor].scrollIntoView({block: 'nearest'});
}
function openCursor() { const rows = cursorRows(); if (rows[state.cursor]) rows[state.cursor].click(); }
function favCursor() {
  const rows = cursorRows(); const row = rows[state.cursor];
  if (!row) return;
  const star = row.querySelector('.fav-star');
  if (star) star.click();
}
function updatePagination() {
  const pages = Math.max(1, Math.ceil(state.total / state.limit));
  $('#page-info').textContent = state.loading ? '正在加载…' : `第 ${state.total ? Math.floor(state.offset / state.limit) + 1 : 0} / ${state.total ? pages : 0} 页 · 共 ${state.total} 条`;
  $('#previous-page').disabled = state.loading || state.offset === 0;
  $('#next-page').disabled = state.loading || state.offset + state.limit >= state.total;
  $('#conversation-list').setAttribute('aria-busy', String(state.loading));
}
async function switchView(view) {
  state.view = view;
  $$('.view').forEach(element => element.classList.toggle('hidden', element.id !== view));
  $$('.nav').forEach(element => element.classList.toggle('active', element.dataset.view === view));
  $('#page-title').textContent = {overview: '学习总览', library: '会话库', review: '复习队列', stats: '数据统计', vocab: '主题词库', ask: '问答查询', todos: '学习待办', data: '导入与备份'}[view];
  if (view === 'library') await loadConversations();
  if (view === 'review') await Promise.all([loadReviews(), loadReviewHistory(), window.usability?.loadCards()]);
  if (view === 'stats') await loadStats();
  if (view === 'vocab') await window.vocabulary?.load();
  if (view === 'ask') await window.askData?.load();
  if (view === 'overview') await loadSummary();
  if (view === 'todos') await window.usability.loadTodos();
  window.usability?.savePreferences();
}
async function loadSummary() {
  const sequence = ++state.summarySeq;
  const data = await api('/api/summary');
  if (sequence !== state.summarySeq) return;
  const labels = [['conversations', '学习会话'], ['messages', '消息总数'], ['annotations', '未解决批注'], ['due', '今日待复习']];
  $('#cards').innerHTML = labels.map(([key, label]) => `<div class="card" data-count="${key}"><div class="label">${label}</div><div class="value">${data.counts[key]}</div><div class="label">数据库实时统计</div></div>`).join('');
  $('#recent').innerHTML = data.recent.length ? data.recent.map(item => `<button class="recent-item" data-id="${item.conversation_id}"><span><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.course_name)} · ${escapeHtml(item.platform_name)} · ${item.message_count} 条消息</small></span><span class="pill">打开</span></button>`).join('') : '<div class="empty-state">还没有会话。去「导入」恢复附带数据，或点右上「扫描本机」。</div>';
  $$('.recent-item').forEach(item => item.onclick = run(async () => { await switchView('library'); await openConversation(item.dataset.id); }));
  await window.usability?.loadRecent();
}
function renderPointOptions(points) {
  const current = $('#point-filter').value;
  $('#point-filter').innerHTML = '<option value="">全部知识点</option>' + points.map(item => `<option value="${item.point_id}">${escapeHtml(item.point_name)}</option>`).join('');
  if (points.some(item => String(item.point_id) === current)) $('#point-filter').value = current;
}
async function loadPointOptions() {
  const course = $('#course-filter').value;
  renderPointOptions(await api(`/api/knowledge-points${course ? `?course_id=${course}` : ''}`));
}
async function loadFilters() {
  const [courses, sources, points] = await Promise.all([api('/api/courses'), api('/api/sources'), api('/api/knowledge-points')]);
  renderPointOptions(points);
  state.courses = courses;
  $('#course-filter').innerHTML = '<option value="">全部课程</option>' + courses.map(item => `<option value="${item.course_id}">${escapeHtml(item.course_name)}</option>`).join('');
  $('#source-filter').innerHTML = '<option value="">全部来源</option>' + sources.map(item => `<option value="${item.source_id}">${escapeHtml(item.platform_name)}</option>`).join('');
}
async function loadConversations() {
  const sequence = ++state.listSeq;
  state.loading = true;
  updatePagination();
  $$('.conversation-item').forEach(item => item.disabled = true);
  const params = new URLSearchParams({q: $('#query').value || '', limit: state.limit, offset: state.offset});
  const course = $('#course-filter').value;
  if (course) params.set('course_id', course);
  if ($('#source-filter').value) params.set('source_id', $('#source-filter').value);
  if ($('#favorites-only').checked) params.set('favorites_only', 'true');
  [['point_id', '#point-filter'], ['annotation_status', '#annotation-filter'], ['review_status', '#review-filter']].forEach(([key, selector]) => { if ($(selector).value) params.set(key, $(selector).value); });
  $('#course-export').classList.toggle('hidden', !course);
  if (course) $('#course-export').href = `/api/export/course/${course}.md`;
  try {
    const data = await api(`/api/conversations?${params}`);
    if (sequence !== state.listSeq) return;
    state.searchQuery = params.get('q');
    state.total = data.total;
    state.items = data.items;
    state.cursor = data.items.length ? 0 : -1;
    if (!data.items.some(item => String(item.conversation_id) === String(state.selected))) clearDetail();
    $('#conversation-list').innerHTML = data.items.length ? data.items.map(conversationItemHtml).join('') : '<div class="list-empty">没找到。换个词，或清掉筛选试试。</div>';
    $$('.conversation-item').forEach(item => {
      item.onclick = run(() => { state.cursor = cursorRows().indexOf(item); moveCursor(0); return openConversation(item.dataset.id, item.dataset.match); });
      item.onkeydown = event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); item.click(); } };
      const star = item.querySelector('.fav-star');
      if (star) star.onclick = run(async event => {
        event.stopPropagation();
        await toggleFavorite(item.dataset.id, star.getAttribute('aria-pressed') !== 'true', star);
      });
    });
    updateSelection();
    moveCursor(0);
    window.usability?.savePreferences();
  } catch (error) {
    if (sequence === state.listSeq) {
      clearDetail();
      $('#conversation-list').innerHTML = '<div class="panel empty-state">加载失败，请点击“检索”重试。</div>';
      showError(error);
    }
  } finally {
    if (sequence === state.listSeq) { state.loading = false; updatePagination(); }
  }
}
const POINT_STATUS = {PROPOSED: '待确认', CONFIRMED: '已确认', REJECTED: '已拒绝'};
function pointChip(point) {
  const status = point.status || 'CONFIRMED';
  const actions = (status === 'PROPOSED' ? `<button class="text-button point-decision" type="button" data-point="${point.point_id}" data-status="CONFIRMED">确认</button><button class="text-button point-decision" type="button" data-point="${point.point_id}" data-status="REJECTED">拒绝</button>` : '') +
    `<button class="text-button point-remove" type="button" data-point="${point.point_id}">移除</button>`;
  return `<span class="point-chip ${status.toLowerCase()}"><button class="text-button point-notes-link" type="button" data-point="${point.point_id}" title="查看该知识点的汇总笔记">${escapeHtml(point.point_name)}</button> · ${POINT_STATUS[status] || status}${actions}</span>`;
}
const TODO_TYPES = new Set(['QUESTION', 'CORRECTION', 'IMPORTANT']);
const NOTE_TYPE_LABELS = {NOTE: '笔记', QUESTION: '疑问', CORRECTION: '纠错', IMPORTANT: '重点'};
function jsonList(value) { try { const result = JSON.parse(value || '[]'); return Array.isArray(result) ? result : []; } catch { return []; } }
function pointChoiceList(points, selected = []) {
  const chosen = new Set((selected || []).map(Number));
  if (!points.length) return '<span class="muted">本课程暂无知识点</span>';
  return points.map(point => `<label class="point-choice"><input type="checkbox" name="point_ids" value="${point.point_id}" ${chosen.has(Number(point.point_id)) ? 'checked' : ''}> ${escapeHtml(point.point_name)}</label>`).join('');
}
function annotationPointSummary(points) {
  if (!points?.length) return '<div class="annotation-points muted">未关联知识点</div>';
  return `<div class="annotation-points">知识点：${points.map(point => `<button type="button" class="text-button note-point-link" data-point="${point.point_id}" title="查看该知识点的汇总笔记">${escapeHtml(point.point_name)}</button>`).join('、')}</div>`;
}
async function openConversation(id, focusMessageId = null) {
  const sequence = ++state.detailSeq;
  state.selected = String(id);
  updateSelection();
  $('#detail').classList.remove('empty');
  $('#detail').innerHTML = '<div class="empty-state">正在加载会话…</div>';
  try {
    if (!state.courses) await loadFilters();
    const data = await api(`/api/conversations/${id}`);
    if (sequence !== state.detailSeq) return;
    const conversation = data.conversation, manual = data.manual_annotation;
    const coursePoints = await api(`/api/knowledge-points?course_id=${conversation.course_id}`);
    if (sequence !== state.detailSeq) return;
    const topics = manual ? jsonList(manual.topics_json) : [];
    const manualHtml = manual ? `<div class="manual-review"><strong>会话深读标注（附带标注快照，只读）</strong><p>${escapeHtml(manual.summary_manual)}</p><div class="meta">意图：${escapeHtml(manual.intent)} · 质量：${escapeHtml(manual.quality)} · 置信度：${escapeHtml(manual.confidence)}</div><p>主题：${topics.length ? topics.map(topic => `<span class="topic-chip">${escapeHtml(topic)}<button type="button" class="text-button adopt-topic" data-topic="${escapeHtml(topic)}" title="把该主题带入标签编辑器">加入标签</button></span>`).join('') : '无'}<br>关键点：${jsonList(manual.key_points_json).map(escapeHtml).join('；') || '无'}<br>未解决：${jsonList(manual.unresolved_questions_json).map(escapeHtml).join('；') || '无'}<br>待办：${jsonList(manual.action_items_json).map(escapeHtml).join('；') || '无'}</p></div>` : '';
    $('#detail').innerHTML = `<div class="panel-head"><div><p class="eyebrow">${escapeHtml(conversation.platform_name)} · ${escapeHtml(conversation.course_name)}</p><h2>${escapeHtml(conversation.title)}</h2><p class="muted">原始会话 ID：${escapeHtml(conversation.source_conversation_id)}</p></div><span class="pill">${data.messages.length} 条消息</span></div>${manualHtml}<div class="detail-summary"><strong>结构化摘要</strong><p>${escapeHtml(conversation.summary || '暂无摘要')}</p></div><div class="meta detail-meta">知识点：${data.points.map(pointChip).join('') || '待归类'}　标签：${(data.tags || []).map(escapeHtml).join('、') || '暂无'}</div><div class="vocab-editors"><section class="vocab-editor-block"><div class="vocab-editor-head"><h3>标签</h3><button type="button" class="text-button" data-vocab-link="tags">管理词库 →</button></div><div id="tag-editor"></div></section><section class="vocab-editor-block"><div class="vocab-editor-head"><h3>知识点</h3><button type="button" class="text-button" data-vocab-link="points">管理词库 →</button></div><div id="point-editor"></div></section></div><div>${data.messages.map(message => `<section class="message ${message.speaker}" data-message-id="${message.message_id}"><div class="speaker">${message.speaker === 'user' ? '你' : 'AI'} · #${message.seq_no}</div><div class="message-content">${escapeHtml(message.content)}</div>${message.annotations.map(note => `<div class="annotation${note.is_resolved ? ' resolved' : ''}"><strong>${escapeHtml(NOTE_TYPE_LABELS[note.annotation_type] || note.annotation_type)}</strong> · ${escapeHtml(note.content)}${TODO_TYPES.has(note.annotation_type) ? ` <button class="small-button resolve-annotation" type="button" data-annotation="${note.annotation_id}" data-resolved="${note.is_resolved ? 1 : 0}" data-message="${message.message_id}">${note.is_resolved ? '重新打开' : '标记解决'}</button>` : ''}${annotationPointSummary(note.points)}<details class="annotation-point-edit"><summary>调整关联知识点</summary><form class="annotation-points-form" data-annotation="${note.annotation_id}"><div class="point-choice-list">${pointChoiceList(coursePoints, (note.points || []).map(point => point.point_id))}</div><button class="small-button" type="submit">保存关联</button></form></details></div>`).join('')}<form class="annotation-form" data-message="${message.message_id}"><select aria-label="批注类型"><option value="NOTE">笔记</option><option value="QUESTION">疑问</option><option value="CORRECTION">纠错</option><option value="IMPORTANT">重点</option></select><input aria-label="批注内容" placeholder="为这条消息添加批注" required><details class="annotation-point-edit"><summary>关联知识点（可多选）</summary><div class="point-choice-list">${pointChoiceList(coursePoints)}</div></details><button class="small-button" type="submit">保存批注</button></form><div class="review-entry">${message.review_card ? '<span class="pill added-review">已加入复习</span>' : `<button class="small-button add-review" type="button">加入复习</button><form class="review-form hidden" data-message="${message.message_id}"><label>复习问题<textarea name="question" required placeholder="你希望复习什么问题？"></textarea></label><label>参考答案<textarea name="answer" required>${escapeHtml(message.content)}</textarea></label><p class="muted">保存后立即加入待复习队列。</p><button class="button" type="submit">创建复习卡</button></form>`}</div></section>`).join('')}</div>`;
    $$('#detail .message-content').forEach(element => { StudyRender.render(element, element.textContent, state.searchQuery); });
    window.vocabulary?.mountTagEditor($('#tag-editor'), id, data.tag_items || [], () => openConversation(id));
    window.vocabulary?.mountPointEditor($('#point-editor'), id, conversation.course_id, data.points || [], () => openConversation(id));
    $$('#detail [data-vocab-link]').forEach(button => button.onclick = run(async () => {
      await switchView('vocab');
      await window.vocabulary?.selectTab(button.dataset.vocabLink, conversation.course_id);
    }));
    $$('#detail .adopt-topic').forEach(button => button.onclick = () => {
      window.vocabulary?.focusTag(button.dataset.topic);
      toast(`已把主题「${button.dataset.topic}」带入标签编辑器；可先选已有标签，再决定是否新建`);
    });
    $('#detail .detail-summary').insertAdjacentHTML('afterend', `<details class="curation-editor"><summary>编辑整理信息</summary><form id="curation-form"><label>课程<select name="course_id">${state.courses.map(course => `<option value="${course.course_id}" ${course.course_id === conversation.course_id ? 'selected' : ''}>${escapeHtml(course.course_name)}</option>`).join('')}</select></label><label>摘要<textarea name="summary" required>${escapeHtml(conversation.summary || '')}</textarea></label><label>标签<input name="tags" list="tag-vocabulary" value="${escapeHtml((data.tags || []).join('、'))}" placeholder="用逗号或顿号分隔；留空可清除标签"><span id="curation-tag-hint" class="field-hint">输入时显示已有标签；完全同名会复用，相近名称会提示。</span></label><label class="inline-check"><input type="checkbox" name="tags_allow_similar"> 允许创建与已有标签相近的新标签</label><button class="button" type="submit">保存整理</button></form></details><p class="curation-provenance muted">${data.curation ? `人工整理：${escapeHtml(data.curation.actor_name)} · ${escapeHtml(data.curation.created_at)} UTC` : '整理信息来源：导入规则或历史快照；可人工修订。'}</p>`);
    window.vocabulary?.wireCurationInput($('#curation-form'));
    $('#curation-form').onsubmit = run(async event => {
      event.preventDefault();
      const form = event.currentTarget;
      const summary = form.elements.summary.value.trim();
      if (!summary) throw new Error('摘要不能为空');
      const tags = [...new Set(form.elements.tags.value.split(/[,，、\n]/).map(value => value.trim()).filter(Boolean))];
      const button = form.querySelector('button'); button.disabled = true;
      try {
        await api(`/api/conversations/${id}`, {method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({course_id: Number(form.elements.course_id.value), summary, tags, tags_allow_similar: form.elements.tags_allow_similar.checked})});
        window.vocabulary?.refreshTagVocabulary();
        await loadConversations();
        if (state.selected === String(id)) await openConversation(id);
        await loadSummary(); toast('整理信息已保存；原始问答保持不变');
      } catch (error) {
        if (error.status === 409) {
          form.elements.tags_allow_similar.checked = false;
          $('#curation-tag-hint').textContent = `${error.message} 如确需保留新名称，请勾选“允许创建与已有标签相近的新标签”后重试。`;
        }
        throw error;
      } finally { button.disabled = false; }
    });
    $$('.annotation-form').forEach(form => form.onsubmit = run(async event => {
      event.preventDefault();
      const content = form.querySelector('input').value.trim();
      if (!content) throw new Error('批注内容不能为空');
      const point_ids = [...form.querySelectorAll('input[name="point_ids"]:checked')].map(input => Number(input.value));
      const button = form.querySelector('button'); button.disabled = true;
      try {
        await post('/api/annotations', {message_id: Number(form.dataset.message), annotation_type: form.querySelector('select').value, content, point_ids});
        if (state.selected === String(id)) await openConversation(id, form.dataset.message);
        await loadSummary(); toast('批注已保存');
      } finally { button.disabled = false; }
    }));
    $$('.annotation-points-form').forEach(form => form.onsubmit = run(async event => {
      event.preventDefault();
      const point_ids = [...form.querySelectorAll('input[name="point_ids"]:checked')].map(input => Number(input.value));
      const button = form.querySelector('button'); button.disabled = true;
      try {
        await api(`/api/annotations/${form.dataset.annotation}`, {method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({point_ids})});
        if (state.selected === String(id)) await openConversation(id);
        toast(point_ids.length ? `已关联 ${point_ids.length} 个知识点` : '已清除该批注的知识点关联');
      } finally { button.disabled = false; }
    }));
    $$('.note-point-link').forEach(button => button.onclick = run(async () => {
      await switchView('vocab');
      await window.vocabulary?.openPointNotes(button.dataset.point);
    }));
    $$('.point-decision').forEach(button => button.onclick = run(async () => {
      button.disabled = true;
      await post(`/api/conversations/${id}/points/${button.dataset.point}`, {status: button.dataset.status});
      if (state.selected === String(id)) await openConversation(id);
      toast(button.dataset.status === 'CONFIRMED' ? '知识点已确认' : '知识点已拒绝');
    }));
    $$('.point-remove').forEach(button => button.onclick = run(async () => {
      button.disabled = true;
      await api(`/api/conversations/${id}/points/${button.dataset.point}`, {method: 'DELETE'});
      if (state.selected === String(id)) await openConversation(id);
      toast('已从本会话移除该知识点；词库中的知识点仍保留');
    }));
    $$('.point-notes-link').forEach(button => button.onclick = run(async () => {
      await switchView('vocab');
      await window.vocabulary?.openPointNotes(button.dataset.point);
    }));
    $$('.resolve-annotation').forEach(button => button.onclick = run(async () => {
      button.disabled = true;
      await api(`/api/annotations/${button.dataset.annotation}`, {method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({is_resolved: button.dataset.resolved !== '1'})});
      if (state.selected === String(id)) await openConversation(id, button.dataset.message);
      await loadSummary(); toast(button.dataset.resolved === '1' ? '批注已重新打开' : '批注已标记解决');
    }));
    $$('.add-review').forEach(button => button.onclick = () => {
      const form = button.parentElement.querySelector('.review-form');
      form.classList.toggle('hidden');
      if (!form.classList.contains('hidden')) form.querySelector('[name="question"]').focus();
    });
    $$('.review-form').forEach(form => form.onsubmit = run(async event => {
      event.preventDefault();
      const question = form.elements.question.value.trim(), answer = form.elements.answer.value.trim();
      if (!question || !answer) throw new Error('问题和答案不能为空');
      const button = form.querySelector('button'); button.disabled = true;
      try {
        await post('/api/reviews', {message_id: Number(form.dataset.message), question, answer});
        if (state.selected === String(id)) await openConversation(id, form.dataset.message);
        await loadSummary(); toast('已加入复习，可立即进入复习队列');
      } finally { button.disabled = false; }
    }));
    const targetMessage = focusMessageId || data.reading_state?.last_message_id;
    if (targetMessage) {
      const message = $(`#detail .message[data-message-id="${Number(targetMessage)}"]`);
      if (message) {
        message.classList.add('search-hit');
        const detail = $('#detail');
        detail.scrollTop += message.getBoundingClientRect().top - detail.getBoundingClientRect().top - 12;
      }
    }
    window.usability?.trackConversation(data, Number(targetMessage) || null);
    window.usability?.savePreferences();
  } catch (error) {
    if (sequence === state.detailSeq) { $('#detail').innerHTML = '<div class="empty-state">会话加载失败，请重新选择。</div>'; showError(error); }
  }
}
async function loadReviews() {
  const sequence = ++state.reviewSeq;
  $('#review-list').textContent = '正在加载复习队列…';
  try {
    const data = await api('/api/reviews/due');
    if (sequence !== state.reviewSeq) return;
    $('#due-count').textContent = `${data.length} 张`;
    $('#review-list').innerHTML = data.length ? data.map(item => `<article class="review-card"><h3>${escapeHtml(item.question)}</h3><button class="small-button reveal-answer" type="button">显示答案（空格）</button><div class="review-answer hidden"></div><div class="meta">${escapeHtml(item.course_name)} · ${escapeHtml(item.title)} · 连续答对 ${item.review_count} 次</div><div class="review-actions hidden"><button class="small-button" data-review="${item.review_card_id}" data-grade="correct">答对，延后（2）</button><button class="small-button" data-review="${item.review_card_id}" data-grade="wrong">答错，明天再来（1）</button></div></article>`).join('') : '<div class="empty-state">今天没卡要复习。</div>';
    $$('.review-card').forEach((card, index) => {
      card.querySelector('.reveal-answer').onclick = () => {
        const answer = card.querySelector('.review-answer'); answer.classList.remove('hidden');
        StudyRender.render(answer, data[index].answer);
        card.querySelector('.review-actions').classList.remove('hidden');
        card.querySelector('.reveal-answer').classList.add('hidden');
      };
    });
    $$('[data-review]').forEach(button => button.onclick = run(async () => {
      button.parentElement.querySelectorAll('button').forEach(item => item.disabled = true);
      try {
        await post(`/api/reviews/${button.dataset.review}/complete?grade=${button.dataset.grade}`);
        state.historyOffset = 0;
        await Promise.all([loadReviews(), loadSummary(), loadReviewHistory()]); toast('复习状态已更新');
        await window.usability?.loadCards();
      } catch (error) { button.parentElement.querySelectorAll('button').forEach(item => item.disabled = false); throw error; }
    }));
  } catch (error) { if (sequence === state.reviewSeq) { $('#review-list').textContent = '复习队列加载失败，请重新进入此页面重试。'; showError(error); } }
}
async function loadReviewHistory() {
  const sequence = ++state.historySeq;
  $('#history-previous').disabled = $('#history-next').disabled = true;
  try {
    const data = await api(`/api/reviews/history?limit=20&offset=${state.historyOffset}`);
    if (sequence !== state.historySeq) return;
    $('#history-info').textContent = `第 ${data.total ? Math.floor(data.offset / 20) + 1 : 0} / ${Math.ceil(data.total / 20)} 页 · 共 ${data.total} 次`;
    $('#review-history').innerHTML = data.items.length ? `<div class="table-scroll"><table><thead><tr><th>复习问题</th><th>结果</th><th>间隔变化</th><th>连续答对</th><th>复习时间（UTC）</th></tr></thead><tbody>${data.items.map(item => `<tr><td>${escapeHtml(item.question)}</td><td>${item.grade === 'correct' ? '答对' : '答错'}</td><td>${item.interval_before} → ${item.interval_after} 天</td><td>${item.streak_before} → ${item.streak_after}</td><td>${escapeHtml(item.reviewed_at)}</td></tr>`).join('')}</tbody></table></div>` : '<div class="empty-state">还没有逐次复习记录；此前未记录的历史不会补造。</div>';
    $('#history-previous').disabled = data.offset === 0;
    $('#history-next').disabled = data.offset + data.limit >= data.total;
  } catch (error) { if (sequence === state.historySeq) { $('#review-history').textContent = '复习历史加载失败，请重新进入此页面重试。'; showError(error); } }
}
async function loadStats() {
  const sequence = ++state.statsSeq;
  const data = await api('/api/stats');
  if (sequence !== state.statsSeq) return;
  const render = (items, label) => items.length ? items.map(item => `<div class="stat-row"><span>${escapeHtml(item[label])}</span><strong>${item.conversations}</strong></div>`).join('') : '<div class="empty-state">暂无数据</div>';
  $('#course-stats').innerHTML = render(data.course, 'course_name');
  $('#source-stats').innerHTML = render(data.source, 'platform_name');
  $('#point-stats').innerHTML = render(data.points, 'point_name');
  const gaps = (data.gaps || []).filter(item => item.priority > 0).slice(0, 8);
  $('#gap-stats').innerHTML = gaps.length ? gaps.map(item => `<div class="gap-row"><div><strong>${escapeHtml(item.point_name)}</strong><small>${item.unresolved_count} 条未解决批注 · ${item.overdue_count} 张逾期复习卡 · ${item.wrong_count} 次答错</small></div><span class="pill">优先级 ${item.priority}</span></div>`).join('') : '<div class="empty-state">当前没有需要优先处理的知识漏洞。</div>';
  $('#review-stats').textContent = `累计复习 ${data.reviews.total} 次 · 答对 ${data.reviews.correct} 次 · 答错 ${data.reviews.wrong} 次`;
}
async function resetSearch() { state.offset = 0; clearDetail(); await loadConversations(); }
$$('[data-view]').forEach(button => button.onclick = run(() => switchView(button.dataset.view)));
$('#search-button').onclick = run(resetSearch);
$('#query').onkeydown = run(async event => { if (event.key === 'Enter') { event.preventDefault(); await resetSearch(); } });
$('#course-filter').onchange = run(async () => { await loadPointOptions(); await resetSearch(); });
['#point-filter', '#annotation-filter', '#review-filter'].forEach(selector => $(selector).onchange = run(resetSearch));
$('#source-filter').onchange = run(resetSearch);
$('#previous-page').onclick = run(async () => { state.offset = Math.max(0, state.offset - state.limit); clearDetail(); await loadConversations(); });
$('#next-page').onclick = run(async () => { state.offset += state.limit; clearDetail(); await loadConversations(); });
$('#history-previous').onclick = run(async () => { state.historyOffset = Math.max(0, state.historyOffset - 20); await loadReviewHistory(); });
$('#history-next').onclick = run(async () => { state.historyOffset += 20; await loadReviewHistory(); });
$('#scan').onclick = run(async () => {
  const button = $('#scan'); button.disabled = true; button.textContent = '扫描中…';
  try {
    const data = await post('/api/import/scan');
    toast(`扫描完成：新增 ${data.imported}，跳过 ${data.skipped}，失败 ${data.failed}`);
    await loadSummary(); if (state.view === 'library') await resetSearch();
  } finally { button.disabled = false; button.textContent = '扫描本机'; }
});
function isTyping(element) {
  return element && (element.tagName === 'INPUT' || element.tagName === 'TEXTAREA' || element.tagName === 'SELECT' || element.isContentEditable);
}
document.addEventListener('keydown', event => {
  if (state.view !== 'library' || event.metaKey || event.ctrlKey || event.altKey) return;
  if (isTyping(event.target)) { if (event.key === 'Escape') event.target.blur(); return; }
  switch (event.key) {
    case 'j': case 'ArrowDown': event.preventDefault(); moveCursor(1); break;
    case 'k': case 'ArrowUp': event.preventDefault(); moveCursor(-1); break;
    case 'o': case 'Enter': event.preventDefault(); openCursor(); break;
    case 'f': event.preventDefault(); favCursor(); break;
    case '/': event.preventDefault(); $('#query').focus(); break;
    case 'Escape': clearDetail(); break;
  }
});
// usability.js 依赖这个 Promise：初始化完成后才解锁导航、恢复偏好。
const initialLoad = Promise.all([loadSummary(), loadFilters()]).then(() => switchView('library'));
initialLoad.catch(showError);
