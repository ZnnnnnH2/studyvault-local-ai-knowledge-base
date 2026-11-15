/* 错题本：按知识点收拢疑问、AI 答错和常错卡。数据全部来自 /api/mistakes。 */
(() => {
  const KIND = {question: '疑问', correction: 'AI 答错', card: '常错卡'};
  const filters = {status: 'open', kind: '', course: ''};
  let sequence = 0;

  function day(value) {
    if (!value) return '';
    const date = new Date(value.includes('T') ? value : value.replace(' ', 'T') + 'Z');
    return Number.isNaN(date.getTime()) ? '' : `${date.getMonth() + 1} 月 ${date.getDate()} 日`;
  }
  function dueText(item) {
    if (item.card_status === 'COMPLETED') return '已完成';
    const due = new Date(item.due_at.replace(' ', 'T') + 'Z');
    const days = Math.round((due - Date.now()) / 86400000);
    if (item.overdue) return days <= -1 ? `逾期 ${-days} 天` : '今天到期';
    return days <= 1 ? '明天复习' : `${days} 天后复习`;
  }
  function itemHtml(item) {
    const source = `<button class="eb-source text-button" type="button" data-cid="${item.conversation_id}" data-mid="${item.message_id}">《${escapeHtml(item.title)}》#${item.seq_no}</button>`;
    let meta, actions;
    if (item.kind === 'card') {
      meta = `答错 ${item.lapse_count} 次 · 连续答对 ${Math.min(item.review_count, 3)}/3 · <span class="${item.overdue && !item.is_resolved ? 'eb-due' : ''}">${dueText(item)}</span>`;
      actions = item.overdue && !item.is_resolved ? '<button class="small-button eb-review" type="button">去复习</button>' : '';
    } else {
      meta = `${day(item.created_at)}记下${item.is_resolved ? ` · ${day(item.updated_at)}弄懂` : ''}`;
      actions = `<button class="small-button eb-resolve" type="button" data-id="${item.id}" data-resolved="${item.is_resolved ? 1 : 0}">${item.is_resolved ? '其实还没懂' : '弄懂了'}</button>`
        + (item.has_card ? '<span class="eb-has-card">已有卡</span>' : `<button class="small-button eb-card" type="button" data-id="${item.id}">出一张卡</button>`);
    }
    return `<li class="eb-item kind-${item.kind}${item.is_resolved ? ' done' : ''}">
      <span class="eb-kind">${KIND[item.kind]}</span>
      <div class="eb-body"><p class="eb-text">${escapeHtml(item.content)}</p><p class="eb-meta">${source} · ${meta}</p></div>
      <div class="eb-actions">${actions}</div>
    </li>`;
  }
  function groupHtml(group, top) {
    const counts = Object.entries(group.counts).filter(([, n]) => n).map(([kind, n]) => `${KIND[kind]} ${n}`).join(' · ');
    const width = top ? Math.max(4, Math.round(group.priority / top * 100)) : 0;
    return `<section class="eb-group">
      <header class="eb-group-head">
        <div><h2>${escapeHtml(group.point_name)}</h2><p class="muted">${escapeHtml(group.course_name)} · ${counts}</p></div>
        ${group.priority ? `<div class="eb-priority" title="疑问 ×3 + AI 答错 ×2 + 不稳的卡 ×2 + 逾期 ×1"><span>优先 ${group.priority}</span><i style="--w:${width}%"></i></div>` : ''}
      </header>
      ${group.point_id === null ? '<p class="eb-unclassified">这些对话还没挂到知识点上，所以没法和同类的错放在一起。<button class="text-button eb-classify" type="button">让 AI 给没归类的对话提建议 →</button></p>' : ''}
      <ol class="eb-items">${group.items.map(itemHtml).join('')}</ol>
    </section>`;
  }
  function summaryHtml(s) {
    const cell = (value, label, extra = '') => `<div class="eb-stat${extra}"><strong>${value}</strong><span>${label}</span></div>`;
    return cell(s.question, '个疑问没弄懂') + cell(s.correction, '处 AI 答错待核对', s.correction ? ' warn' : '') + cell(s.card, '张卡还不稳')
      + cell(s.points, '个知识点有漏洞')
      + `<p class="eb-week">最近 7 天新记下 <b>${s.week.new}</b> 个错，弄懂 <b>${s.week.mastered}</b> 个；累计已攻克 ${s.mastered} 个。</p>`;
  }
  async function load() {
    const current = ++sequence;
    const params = new URLSearchParams({status: filters.status});
    if (filters.kind) params.set('kind', filters.kind);
    if (filters.course) params.set('course_id', filters.course);
    $('#eb-groups').setAttribute('aria-busy', 'true');
    const [data, courses] = await Promise.all([api(`/api/mistakes?${params}`), state.courses ? Promise.resolve(state.courses) : api('/api/courses')]);
    if (current !== sequence) return;
    const select = $('#eb-course');
    if (select.options.length <= 1) select.innerHTML = '<option value="">全部课程</option>' + courses.filter(c => c.course_code !== 'GEN').map(c => `<option value="${c.course_id}">${escapeHtml(c.course_name)}</option>`).join('');
    $('#eb-summary').innerHTML = summaryHtml(data.summary);
    const top = Math.max(0, ...data.groups.map(g => g.priority));
    const empty = filters.status === 'mastered' ? '还没有攻克的错题。' : '这里空着——要么真都懂了，要么还没在对话里标过疑问。读对话时遇到没懂的、发现 AI 说错的，在那条消息下记一笔就会出现在这里。';
    $('#eb-groups').innerHTML = data.groups.length ? data.groups.map(g => groupHtml(g, top)).join('') : `<div class="empty-state">${empty}</div>`;
    $('#eb-groups').setAttribute('aria-busy', 'false');
    $$('#eb-groups .eb-source').forEach(button => button.onclick = run(async () => { await switchView('library'); await openConversation(button.dataset.cid, button.dataset.mid); }));
    $$('#eb-groups .eb-review').forEach(button => button.onclick = run(() => switchView('review')));
    $$('#eb-groups .eb-resolve').forEach(button => button.onclick = run(async () => {
      button.disabled = true;
      const resolved = button.dataset.resolved !== '1';
      await api(`/api/annotations/${button.dataset.id}`, {method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({is_resolved: resolved})});
      toast(resolved ? '记为弄懂了' : '重新放回错题本');
      await load();
    }));
    $$('#eb-groups .eb-classify').forEach(button => button.onclick = run(async () => {
      button.disabled = true; button.textContent = '正在读这些对话…';
      try {
        const result = await post('/api/classify/unclassified');
        toast(`读了 ${result.conversations} 段对话，提出 ${result.proposed} 个知识点${result.course_suggestions ? `，${result.course_suggestions} 段可能放错了课` : ''}；去会话里确认`);
        await load();
      } finally { button.disabled = false; }
    }));
    $$('#eb-groups .eb-card').forEach(button => button.onclick = run(async () => {
      button.disabled = true;
      await post(`/api/mistakes/${button.dataset.id}/card`, {});
      toast('已出卡，复习队列里马上能做');
      await load();
    }));
  }
  $$('#eb-status input').forEach(input => input.onchange = run(async () => { filters.status = input.value; await load(); }));
  $$('#eb-kind input').forEach(input => input.onchange = run(async () => { filters.kind = input.value; await load(); }));
  $('#eb-course').onchange = run(async event => { filters.course = event.target.value; await load(); });
  window.errorbook = {load};
})();
