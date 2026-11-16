/* Managed tag / knowledge-point vocabulary: existing-term pickers, near-duplicate
   confirmation, and rename/merge management.  Plain script so it shares the
   helpers loaded by app.js. */
(() => {
  const vState = {kind: 'tags', query: '', courseId: '', courses: [], items: [], allItems: [],
    total: 0, seq: 0, loaded: false, pointId: null, pointOffset: 0, pointNotesSeq: 0};
  let datalistSeq = 0;

  const termName = (kind, item) => kind === 'tag' ? item.tag_name : item.point_name;
  const termId = (kind, item) => kind === 'tag' ? item.tag_id : item.point_id;
  const kindName = kind => kind === 'tag' ? '标签' : '知识点';
  const relationLabel = relation => ({exact: '同名', similar: '相近', contains: '包含', all: '已有'}[relation] || '已有');
  const relationClass = relation => ({exact: 'exact', similar: 'similar', contains: 'contains'}[relation] || 'all');

  function endpoint(kind, q, courseId) {
    if (kind === 'tag') return `/api/tags?q=${encodeURIComponent(q || '')}&limit=200`;
    const params = new URLSearchParams({q: q || '', limit: 200});
    if (courseId) params.set('course_id', courseId);
    return `/api/knowledge-points/suggest?${params}`;
  }

  async function loadCourses() {
    if (vState.courses.length) return;
    vState.courses = await api('/api/courses');
    $('#vocab-course').innerHTML = '<option value="">全部课程</option>' +
      vState.courses.map(course => `<option value="${course.course_id}">${escapeHtml(course.course_name)}</option>`).join('');
  }

  function syncKindUi() {
    $$('[data-vocab-kind]').forEach(button => {
      const active = button.dataset.vocabKind === vState.kind;
      button.classList.toggle('active', active);
      button.setAttribute('aria-selected', String(active));
    });
    const points = vState.kind === 'points';
    $('#vocab-course').classList.toggle('hidden', !points);
    $('#vocab-title').textContent = points ? '知识点词库' : '标签词库';
    $('#vocab-description').textContent = points
      ? '知识点按课程管理；输入时先看已有词汇，相近名称会提示。合并只允许在同一课程内进行。'
      : '输入时先看已有词汇；相近名称会提示，避免“外键”“外键约束”各自成词。';
    $('#vocab-create-name').placeholder = points ? '输入新知识点…' : '输入新标签…';
    $('#vocab-create-form button').textContent = points ? '新建知识点' : '新建标签';
  }

  function mergeCandidates(item) {
    const kind = vState.kind === 'tags' ? 'tag' : 'point';
    return vState.allItems.filter(candidate => {
      if (termId(kind, candidate) === termId(kind, item)) return false;
      if (kind === 'point' && Number(candidate.course_id) !== Number(item.course_id)) return false;
      return true;
    });
  }

  function renderRows() {
    const kind = vState.kind === 'tags' ? 'tag' : 'point';
    const name = item => termName(kind, item);
    const id = item => termId(kind, item);
    $('#vocab-list').innerHTML = vState.items.length ? vState.items.map(item => {
      const candidates = mergeCandidates(item);
      const options = candidates.length
        ? candidates.map(candidate => `<option value="${id(candidate)}">${escapeHtml(name(candidate))}${kind === 'point' ? ` · ${escapeHtml(candidate.course_name)}` : ''}</option>`).join('')
        : '';
      return `<article class="vocab-row" data-id="${id(item)}" data-kind="${kind}" data-course="${item.course_id || ''}">
        <div class="vocab-row-main">
          <strong class="vocab-name">${escapeHtml(name(item))}</strong>
          <span class="pill">${item.conversation_count} 个会话</span>
          ${kind === 'point' ? `<span class="vocab-course muted">${escapeHtml(item.course_name)}</span>` : ''}
          ${item.aliases?.length ? `<span class="vocab-alias muted">别名：${item.aliases.map(escapeHtml).join('、')}</span>` : ''}
        </div>
        <div class="vocab-row-actions">
          ${kind === 'point' ? `<button class="small-button point-notes-open" type="button" data-id="${id(item)}">笔记 ${item.note_count || 0}</button>` : ''}
          <button class="small-button vocab-rename" type="button">重命名</button>
          <details class="vocab-merge"><summary class="small-button">合并…</summary>
            ${candidates.length ? `<form class="vocab-merge-form"><label>合并到<select name="target" required>${options}</select></label><button class="small-button" type="submit">确认合并</button><p class="field-hint">本词条的会话关联会转到目标；旧名保留为别名。</p></form>` : '<p class="field-hint">没有其他可合并的词汇。</p>'}
          </details>
        </div>
        <div class="vocab-row-edit hidden"></div>
      </article>`;
    }).join('') : `<div class="empty-state">${vState.query ? '没有匹配的已有词汇；可以直接在上方新建。' : '词库还是空的；导入或新建后会显示在这里。'}</div>`;
    $$('#vocab-list .vocab-row').forEach(wireRow);
  }

  function wireRow(row) {
    const kind = vState.kind === 'tags' ? 'tag' : 'point';
    const itemId = Number(row.dataset.id);
    const item = vState.items.find(entry => termId(kind, entry) === itemId);
    if (!item) return;
    const edit = row.querySelector('.vocab-row-edit');
    const notesButton = row.querySelector('.point-notes-open');
    if (notesButton) notesButton.onclick = () => openPointNotes(itemId);
    row.querySelector('.vocab-rename').onclick = () => {
      edit.classList.remove('hidden');
      edit.innerHTML = `<form class="vocab-rename-form">
        <label>新名称<input name="name" maxlength="50" required value="${escapeHtml(termName(kind, item))}"></label>
        <label class="inline-check"><input type="checkbox" name="allow_similar"> 允许与已有词汇相近（不建议）</label>
        <div class="vocab-edit-actions"><button class="small-button" type="submit">保存</button><button class="small-button" type="button" data-cancel>取消</button></div>
        <p class="vocab-edit-error" role="alert"></p>
      </form>`;
      const form = edit.querySelector('form');
      form.querySelector('[data-cancel]').onclick = () => { edit.classList.add('hidden'); edit.innerHTML = ''; };
      form.onsubmit = run(async event => {
        event.preventDefault();
        const payload = kind === 'tag'
          ? {tag_name: form.elements.name.value.trim(), allow_similar: form.elements.allow_similar.checked}
          : {point_name: form.elements.name.value.trim(), allow_similar: form.elements.allow_similar.checked};
        const url = kind === 'tag' ? `/api/tags/${itemId}` : `/api/knowledge-points/${itemId}`;
        try {
          await api(url, {method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
        } catch (error) {
          if (error.status === 409) {
            form.querySelector('.vocab-edit-error').textContent = error.message;
            form.elements.allow_similar.checked = false;
          }
          throw error;
        }
        toast(`${kindName(kind)}已重命名；旧名保留为别名`);
        await refreshAll();
      });
      form.elements.name.focus();
      form.elements.name.select();
    };
    const mergeForm = row.querySelector('.vocab-merge-form');
    if (mergeForm) mergeForm.onsubmit = run(async event => {
      event.preventDefault();
      const target = Number(mergeForm.elements.target.value);
      const targetItem = vState.allItems.find(entry => termId(kind, entry) === target);
      if (!targetItem) throw new Error('请选择合并目标');
      const payload = kind === 'tag' ? {source_tag_id: itemId} : {source_point_id: itemId};
      const url = kind === 'tag' ? `/api/tags/${target}/merge` : `/api/knowledge-points/${target}/merge`;
      const button = mergeForm.querySelector('button');
      button.disabled = true;
      try {
        await post(url, payload);
        toast(`已把「${termName(kind, item)}」合并到「${termName(kind, targetItem)}」；旧名保留为别名`);
        await refreshAll();
      } finally {
        button.disabled = false;
      }
    });
  }

  async function loadList() {
    const sequence = ++vState.seq;
    const kind = vState.kind === 'tags' ? 'tag' : 'point';
    const data = await api(endpoint(kind, vState.query, vState.courseId));
    if (sequence !== vState.seq) return;
    vState.items = data.items || [];
    vState.total = data.total || 0;
    $('#vocab-count').textContent = `${vState.total} 个`;
    const all = vState.query ? await api(endpoint(kind, '', vState.courseId)) : data;
    if (sequence !== vState.seq) return;
    vState.allItems = all.items || [];
    renderRows();
  }

  async function refreshAll() {
    await loadList();
    await refreshTagVocabulary();
    if (typeof loadPointOptions === 'function') await loadPointOptions();
  }

  async function refreshTagVocabulary() {
    const sequence = ++datalistSeq;
    try {
      const data = await api('/api/tags?limit=200');
      if (sequence !== datalistSeq) return;
      $('#tag-vocabulary').innerHTML = (data.items || [])
        .map(item => `<option value="${escapeHtml(item.tag_name)}">${item.conversation_count} 个会话</option>`).join('');
    } catch (error) {
      if (sequence === datalistSeq) console.warn('标签词库预取失败', error);
    }
  }

  const NOTE_LABELS = {NOTE: '笔记', QUESTION: '疑问', CORRECTION: '纠错', IMPORTANT: '重点'};

  function utcText(value) {
    if (!value) return '';
    const date = new Date(String(value).includes('T') ? value : String(value).replace(' ', 'T') + 'Z');
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toISOString().slice(0, 16).replace('T', ' ');
  }

  function showMain() {
    $('#point-notes-view').classList.add('hidden');
    $('#vocab-main').classList.remove('hidden');
  }

  async function openPointNotes(pointId) {
    vState.pointId = Number(pointId);
    vState.pointOffset = 0;
    vState.query = '';
    $('#vocab-query').value = '';
    $('#vocab-create-conflict').classList.add('hidden');
    $('#vocab-main').classList.add('hidden');
    $('#point-notes-view').classList.remove('hidden');
    await loadPointNotes();
  }

  async function loadPointNotes() {
    const sequence = ++vState.pointNotesSeq;
    const data = await api(`/api/knowledge-points/${vState.pointId}/notes?limit=20&offset=${vState.pointOffset}`);
    if (sequence !== vState.pointNotesSeq) return;
    vState.kind = 'points';
    vState.courseId = String(data.point.course_id);
    $('#vocab-course').value = vState.courseId;
    syncKindUi();
    $('#point-notes-title').textContent = data.point.point_name;
    $('#point-notes-eyebrow').textContent = `${data.point.course_name} · ${data.total} 条笔记`;
    $('#point-notes-summary').textContent = '消息批注保留原消息作为依据；独立笔记直接写在这个知识点下。';
    const list = $('#point-note-list');
    list.innerHTML = data.items.length ? data.items.map(item => {
      if (item.source === 'message') {
        return `<article class="point-note-item">
          <div class="point-note-meta"><span class="pill">${item.annotation_type === 'NOTE' ? '消息笔记' : `消息笔记 · ${escapeHtml(NOTE_LABELS[item.annotation_type] || item.annotation_type)}`}</span><small>${escapeHtml(item.title)} · 消息 #${item.seq_no} · ${escapeHtml(utcText(item.created_at))} UTC</small></div>
          <p class="point-note-content">${escapeHtml(item.content)}</p>
          ${item.message_excerpt ? `<p class="point-note-excerpt muted">原消息：${escapeHtml(item.message_excerpt)}</p>` : ''}
          <button class="small-button point-note-jump" type="button" data-cid="${item.conversation_id}" data-mid="${item.message_id}">查看原消息</button>
        </article>`;
      }
      const modified = item.updated_at && new Date(item.updated_at).getTime() - new Date(item.created_at).getTime() > 1000;
      return `<article class="point-note-item" data-note-id="${item.id}">
        <div class="point-note-meta"><span class="pill">独立笔记</span><small>${escapeHtml(utcText(item.created_at))} UTC${modified ? ` · 修改于 ${escapeHtml(utcText(item.updated_at))} UTC` : ''}</small></div>
        <p class="point-note-content">${escapeHtml(item.content)}</p>
        <details class="point-note-edit"><summary>编辑</summary><form class="point-note-edit-form" data-id="${item.id}"><textarea required maxlength="4000">${escapeHtml(item.content)}</textarea><button class="small-button" type="submit">保存修改</button></form></details>
        <button class="small-button point-note-delete" type="button" data-id="${item.id}">删除</button>
      </article>`;
    }).join('') : '<p class="empty-state">这个知识点还没有笔记。可以直接写一条独立笔记，或在消息下保存批注时关联它。</p>';
    $('#point-notes-info').textContent = `第 ${data.total ? Math.floor(data.offset / data.limit) + 1 : 0} / ${Math.max(1, Math.ceil(data.total / data.limit))} 页 · 共 ${data.total} 条`;
    $('#point-notes-previous').disabled = data.offset === 0;
    $('#point-notes-next').disabled = data.offset + data.limit >= data.total;
    list.querySelectorAll('.point-note-jump').forEach(button => button.onclick = run(async () => {
      await switchView('library');
      await openConversation(button.dataset.cid, button.dataset.mid);
    }));
    list.querySelectorAll('.point-note-delete').forEach(button => button.onclick = run(async () => {
      button.disabled = true;
      await api(`/api/point-notes/${button.dataset.id}`, {method: 'DELETE'});
      toast('独立笔记已删除');
      await loadPointNotes();
      await loadList();
    }));
    list.querySelectorAll('.point-note-edit-form').forEach(form => form.onsubmit = run(async event => {
      event.preventDefault();
      const button = form.querySelector('button'); button.disabled = true;
      try {
        await api(`/api/point-notes/${form.dataset.id}`, {method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({content: form.querySelector('textarea').value.trim()})});
        toast('独立笔记已保存');
        await loadPointNotes();
      } finally {
        button.disabled = false;
      }
    }));
  }

  async function load() {
    showMain();
    await loadCourses();
    vState.query = $('#vocab-query').value.trim();
    vState.courseId = $('#vocab-course').value;
    syncKindUi();
    await loadList();
    await refreshTagVocabulary();
    vState.loaded = true;
  }

  async function selectTab(kind, courseId) {
    showMain();
    const nextKind = kind === 'points' ? 'points' : 'tags';
    const changed = vState.kind !== nextKind || (courseId && String(vState.courseId) !== String(courseId));
    vState.kind = nextKind;
    if (courseId) vState.courseId = String(courseId);
    if (changed) {
      vState.query = '';
      $('#vocab-query').value = '';
    }
    syncKindUi();
    $('#vocab-course').value = vState.courseId;
    if (changed) await loadList();
  }

  function createPayload(name, allowSimilar) {
    const kind = vState.kind === 'tags' ? 'tag' : 'point';
    if (kind === 'tag') return {tag_name: name, allow_similar: allowSimilar};
    if (!vState.courseId) throw new Error('新建知识点前请先选择课程');
    return {course_id: Number(vState.courseId), point_name: name, allow_similar: allowSimilar};
  }

  async function createTerm(name, allowSimilar) {
    const kind = vState.kind === 'tags' ? 'tag' : 'point';
    const url = kind === 'tag' ? '/api/tags' : '/api/knowledge-points';
    return api(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(createPayload(name, allowSimilar))});
  }

  function renderCreateConflict(detail, name) {
    const box = $('#vocab-create-conflict');
    const kind = vState.kind === 'tags' ? 'tag' : 'point';
    const suggestions = detail?.suggestions || [];
    box.classList.remove('hidden');
    box.innerHTML = `<p>${escapeHtml(detail?.message || '已有相近词汇，请先选择已有项或确认新建。')}</p>
      <div class="vocab-conflict-actions">
        ${suggestions.map(item => `<button type="button" class="small-button vocab-use-existing" data-name="${escapeHtml(termName(kind, item))}">查看已有「${escapeHtml(termName(kind, item))}」</button>`).join('')}
        <button type="button" class="small-button vocab-create-anyway" data-name="${escapeHtml(name)}">仍然新建「${escapeHtml(name)}」</button>
      </div>
      <p class="field-hint">“仍然新建”会保留一个与已有词汇相近的新词条；也可以去列表中重命名或合并已有词条。</p>`;
    $$('#vocab-create-conflict .vocab-use-existing').forEach(button => button.onclick = run(async () => {
      $('#vocab-query').value = button.dataset.name;
      vState.query = button.dataset.name;
      box.classList.add('hidden');
      $('#vocab-create-name').value = '';
      await loadList();
      toast('已在列表中定位已有词汇');
    }));
    $('#vocab-create-conflict .vocab-create-anyway').onclick = run(async () => {
      const created = await createTerm(buttonName, true);
      box.classList.add('hidden');
      $('#vocab-create-name').value = '';
      toast(created.created
        ? `已新建${kindName(kind)}「${created.tag_name || created.point_name}」`
        : `已有同名${kindName(kind)}「${created.tag_name || created.point_name}」，已直接复用`);
      await refreshAll();
    });
  }

  let buttonName = '';
  $('#vocab-create-form').onsubmit = run(async event => {
    event.preventDefault();
    const input = $('#vocab-create-name');
    const name = input.value.trim();
    if (!name) throw new Error('词汇名称不能为空');
    const box = $('#vocab-create-conflict');
    box.classList.add('hidden');
    try {
      const created = await createTerm(name, false);
      input.value = '';
      toast(created.created
        ? `已新建${kindName(vState.kind === 'tags' ? 'tag' : 'point')}「${created.tag_name || created.point_name}」`
        : `已有同名${kindName(vState.kind === 'tags' ? 'tag' : 'point')}「${created.tag_name || created.point_name}」，已直接复用`);
      await refreshAll();
    } catch (error) {
      if (error.status !== 409 || !error.detail) throw error;
      buttonName = name;
      renderCreateConflict(error.detail, name);
    }
  });

  $$('[data-vocab-kind]').forEach(button => button.onclick = run(async () => {
    vState.kind = button.dataset.vocabKind;
    vState.query = $('#vocab-query').value.trim();
    $('#vocab-create-conflict').classList.add('hidden');
    syncKindUi();
    await loadList();
  }));
  $('#vocab-course').onchange = run(async () => {
    vState.courseId = $('#vocab-course').value;
    await loadList();
  });
  let queryTimer;
  $('#vocab-query').oninput = () => {
    clearTimeout(queryTimer);
    queryTimer = setTimeout(run(async () => {
      vState.query = $('#vocab-query').value.trim();
      await loadList();
    }), 180);
  };

  /* ---------- Reusable picker used in conversation detail ---------- */

  function pickerTerm(kind, item) {
    return {id: kind === 'tag' ? item.tag_id : item.point_id, name: kind === 'tag' ? item.tag_name : item.point_name};
  }

  async function pickerSearch(kind, q, courseId) {
    if (kind === 'tag') return api(`/api/tags?q=${encodeURIComponent(q)}&limit=8`);
    return api(`/api/knowledge-points/suggest?${new URLSearchParams({q, limit: 8, ...(courseId ? {course_id: courseId} : {})})}`);
  }

  function mountPicker(container, {kind, conversationId, courseId, onDone}) {
    if (!container) return;
    const input = container.querySelector('.vocab-picker-input');
    const menu = container.querySelector('.vocab-picker-menu');
    const conflict = container.querySelector('.vocab-picker-conflict');
    if (!input || !menu) return;
    let seq = 0, timer, active = -1, lastItems = [], menuQuery = null;

    function hideMenu() { menu.classList.add('hidden'); active = -1; }

    function showConflict(detail, name) {
      const suggestions = detail?.suggestions || [];
      hideMenu();
      conflict.classList.remove('hidden');
      conflict.innerHTML = `<p>${escapeHtml(detail?.message || '已有相近词汇，请先选择已有项或确认新建。')}</p>
        <div class="vocab-conflict-actions">
          ${suggestions.map(item => `<button type="button" class="small-button vocab-use-existing" data-id="${pickerTerm(kind, item).id}">使用已有「${escapeHtml(pickerTerm(kind, item).name)}」</button>`).join('')}
          <button type="button" class="small-button vocab-create-anyway" data-name="${escapeHtml(name)}">仍然新建「${escapeHtml(name)}」</button>
        </div>`;
      conflict.querySelectorAll('.vocab-use-existing').forEach(button => button.onclick = run(async () => {
        await linkExisting(Number(button.dataset.id));
      }));
      const anyway = conflict.querySelector('.vocab-create-anyway');
      anyway.onclick = run(async () => {
        anyway.disabled = true;
        try {
          await createNamed(name, true);
        } finally {
          anyway.disabled = false;
        }
      });
    }

    async function linkExisting(id) {
      const payload = kind === 'tag' ? {tag_id: id} : {point_id: id};
      const url = kind === 'tag' ? `/api/conversations/${conversationId}/tags` : `/api/conversations/${conversationId}/points`;
      await post(url, payload);
      hideMenu();
      conflict.classList.add('hidden');
      input.value = '';
      onDone?.();
    }

    async function createNamed(name, allowSimilar) {
      const payload = kind === 'tag'
        ? {tag_name: name, allow_similar: allowSimilar}
        : {point_name: name, allow_similar: allowSimilar};
      const url = kind === 'tag' ? `/api/conversations/${conversationId}/tags` : `/api/conversations/${conversationId}/points`;
      try {
        const result = await post(url, payload);
        hideMenu();
        conflict.classList.add('hidden');
        input.value = '';
        toast(result.created ? `已新建${kindName(kind)}「${result.tag_name || result.point_name}」` : `已使用已有${kindName(kind)}「${result.tag_name || result.point_name}」`);
        onDone?.();
      } catch (error) {
        if (error.status === 409 && error.detail) {
          showConflict(error.detail, name);
          return;
        }
        throw error;
      }
    }

    function renderMenu(data, query) {
      menuQuery = query;
      lastItems = data.items || [];
      const exact = lastItems.find(item => item.relation === 'exact');
      const similar = lastItems.filter(item => item.relation === 'similar');
      const rows = lastItems.map((item, index) => {
        const term = pickerTerm(kind, item);
        return `<button type="button" class="vocab-option ${item.relation === 'similar' ? 'is-similar' : ''}" data-action="link" data-id="${term.id}" data-index="${index}">
          <span class="vocab-option-name">${escapeHtml(term.name)}</span>
          <span class="vocab-option-meta"><span class="vocab-badge ${relationClass(item.relation)}">${relationLabel(item.relation)}</span><small>${item.conversation_count} 个会话</small></span>
        </button>`;
      }).join('');
      const hint = exact
        ? `<p class="field-hint">已有同名${kindName(kind)}，将直接复用，不会重复创建。</p>`
        : (similar.length ? `<p class="field-hint field-warn">已有相近${kindName(kind)}，建议先选上方已有项；确需新建请点下方并再确认。</p>` : '');
      const action = query
        ? (exact
          ? `<button type="button" class="vocab-option vocab-default" data-action="link" data-id="${pickerTerm(kind, exact).id}"><span>使用已有「${escapeHtml(pickerTerm(kind, exact).name)}」</span></button>`
          : `<button type="button" class="vocab-option vocab-default" data-action="create" data-name="${escapeHtml(query)}"><span>新建${kindName(kind)}「${escapeHtml(query)}」</span></button>`)
        : '';
      menu.innerHTML = `${rows}${rows ? '' : `<p class="vocab-empty">没有匹配的已有${kindName(kind)}。</p>`}${hint}${action}` || `<p class="vocab-empty">还没有已有${kindName(kind)}，输入名称即可新建。</p>`;
      menu.classList.remove('hidden');
      active = -1;
      menu.querySelectorAll('[data-action]').forEach(button => button.onclick = run(async () => {
        if (button.dataset.action === 'link') await linkExisting(Number(button.dataset.id));
        else await createNamed(button.dataset.name, false);
      }));
    }

    async function search(query) {
      const current = ++seq;
      menu.classList.remove('hidden');
      menu.innerHTML = '<p class="vocab-empty">正在查找已有词汇…</p>';
      try {
        const data = await pickerSearch(kind, query, courseId);
        if (current !== seq) return;
        renderMenu(data, query);
      } catch (error) {
        if (current === seq) menu.innerHTML = `<p class="vocab-empty">词库加载失败：${escapeHtml(error.message)}</p>`;
      }
    }

    function queueSearch() {
      clearTimeout(timer);
      timer = setTimeout(() => search(input.value.trim()), 120);
    }

    input.addEventListener('focus', () => { conflict.classList.add('hidden'); queueSearch(); });
    input.addEventListener('input', () => {
      menuQuery = null;
      menu.classList.remove('hidden');
      menu.innerHTML = '<p class="vocab-empty">正在查找已有词汇…</p>';
      queueSearch();
    });
    input.addEventListener('blur', () => setTimeout(hideMenu, 120));
    input.addEventListener('keydown', event => {
      if (event.key === 'Escape') { hideMenu(); input.blur(); return; }
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        const options = [...menu.querySelectorAll('.vocab-option')];
        if (!options.length) return;
        active = event.key === 'ArrowDown' ? Math.min(options.length - 1, active + 1) : Math.max(0, active - 1);
        options.forEach((option, index) => option.classList.toggle('active', index === active));
        options[active]?.scrollIntoView({block: 'nearest'});
        return;
      }
      if (event.key === 'Enter') {
        event.preventDefault();
        const query = input.value.trim();
        if (!query) return;
        // If the user presses Enter before the debounced search returns, do not
        // activate a stale menu row from the previous query. The create endpoint
        // still reuses an exact match and returns 409 for a near-duplicate.
        if (menuQuery !== query) {
          run(async () => createNamed(query, false))();
          return;
        }
        const activeOption = menu.querySelector('.vocab-option.active') || menu.querySelector('.vocab-option');
        if (activeOption) activeOption.click();
        else run(async () => createNamed(query, false))();
      }
    });
    menu.addEventListener('mousedown', event => event.preventDefault());
  }

  function mountTagEditor(container, conversationId, tagItems, onChange) {
    if (!container) return;
    container.innerHTML = `<div class="vocab-picker" data-kind="tag">
      <div class="vocab-picker-chips">${tagItems.length ? tagItems.map(item => `<span class="vocab-chip">${escapeHtml(item.tag_name)}<button type="button" class="vocab-chip-remove" data-id="${item.tag_id}" aria-label="移除标签 ${escapeHtml(item.tag_name)}">×</button></span>`).join('') : '<span class="muted">还没有标签</span>'}</div>
      <div class="vocab-picker-search"><input class="vocab-picker-input" maxlength="50" autocomplete="off" aria-label="搜索或新建标签" placeholder="搜索已有标签，或输入新标签"><div class="vocab-picker-menu hidden" role="listbox"></div></div>
      <div class="vocab-picker-conflict hidden" role="alert"></div>
      <p class="field-hint">先显示已有标签；同名会复用，相近名称会提示后再决定是否新建。</p>
    </div>`;
    $$('.vocab-chip-remove', container).forEach(button => button.onclick = run(async () => {
      button.disabled = true;
      await api(`/api/conversations/${conversationId}/tags/${button.dataset.id}`, {method: 'DELETE'});
      toast('已移除该标签');
      await refreshTagVocabulary();
      onChange?.();
    }));
    mountPicker(container, {kind: 'tag', conversationId, onDone: () => { refreshTagVocabulary(); onChange?.(); }});
  }

  function mountPointEditor(container, conversationId, courseId, points, onChange) {
    if (!container) return;
    container.innerHTML = `<div class="vocab-picker" data-kind="point" data-course="${courseId || ''}">
      <div class="vocab-picker-search"><input class="vocab-picker-input" maxlength="50" autocomplete="off" aria-label="搜索或新建知识点" placeholder="搜索已有知识点，或输入新知识点"><div class="vocab-picker-menu hidden" role="listbox"></div></div>
      <div class="vocab-picker-conflict hidden" role="alert"></div>
      <p class="field-hint">知识点按本会话所在课程查找；同名会复用，相近名称会提示。上方标签可确认、拒绝或移除。</p>
    </div>`;
    mountPicker(container, {kind: 'point', conversationId, courseId, onDone: () => {
      if (typeof loadPointOptions === 'function') loadPointOptions();
      onChange?.();
    }});
  }

  function focusTag(topic) {
    const picker = $('#tag-editor .vocab-picker');
    if (!picker) return;
    const input = picker.querySelector('.vocab-picker-input');
    if (!input) return;
    input.value = topic;
    input.dispatchEvent(new Event('input', {bubbles: true}));
    input.focus();
    picker.scrollIntoView({block: 'nearest'});
  }

  function wireCurationInput(form) {
    if (!form) return;
    const input = form.elements.tags;
    const hint = $('#curation-tag-hint');
    if (!input || !hint) return;
    let timer;
    input.addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const pieces = input.value.split(/[,，、\n]/).map(value => value.trim()).filter(Boolean);
        const last = pieces[pieces.length - 1] || '';
        if (!last) {
          hint.textContent = '输入时显示已有标签；完全同名会复用，相近名称会提示。';
          return;
        }
        try {
          const data = await api(`/api/tags?q=${encodeURIComponent(last)}&limit=5`);
          const exact = (data.items || []).find(item => item.relation === 'exact');
          const similar = (data.items || []).filter(item => item.relation === 'similar');
          if (exact) hint.textContent = `已有同名标签「${exact.tag_name}」，保存时会直接复用。`;
          else if (similar.length) hint.textContent = `已有相近标签「${similar[0].tag_name}」；建议直接使用已有项，确需新建请勾选下方允许项。`;
          else hint.textContent = '没有相近的已有标签；保存时会新建。';
        } catch (error) {
          hint.textContent = '词库提示暂不可用，保存时仍会检查相近名称。';
        }
      }, 180);
    });
  }

  document.addEventListener('pointerdown', event => {
    $$('.vocab-picker').forEach(picker => {
      if (!picker.contains(event.target)) picker.querySelector('.vocab-picker-menu')?.classList.add('hidden');
    });
  });
  $('#point-notes-back').onclick = run(async () => {
    showMain();
    await loadList();
  });
  $('#point-notes-previous').onclick = run(async () => {
    vState.pointOffset = Math.max(0, vState.pointOffset - 20);
    await loadPointNotes();
  });
  $('#point-notes-next').onclick = run(async () => {
    vState.pointOffset += 20;
    await loadPointNotes();
  });
  $('#point-note-form').onsubmit = run(async event => {
    event.preventDefault();
    const input = $('#point-note-content');
    const content = input.value.trim();
    if (!content) throw new Error('笔记内容不能为空');
    const button = event.currentTarget.querySelector('button');
    button.disabled = true;
    try {
      await post(`/api/knowledge-points/${vState.pointId}/notes`, {content});
      input.value = '';
      toast('独立笔记已保存');
      await loadPointNotes();
      await loadList();
    } finally {
      button.disabled = false;
    }
  });
  refreshTagVocabulary();
  window.vocabulary = {load, selectTab, openPointNotes, refreshTagVocabulary, mountTagEditor, mountPointEditor,
    focusTag, wireCurationInput, termName, normalize: value => String(value || '').trim()};
})();
