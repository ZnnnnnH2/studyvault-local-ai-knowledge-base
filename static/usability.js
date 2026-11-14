/* Additional local workflows; persistence lives in SQLite, not only this browser. */
(() => {
  let initializing = true, restoring = false, preferenceTimer, progressTimer, activeReading = null, scrollIntent = 0;
  let todoOffset = 0, cardOffset = 0, todoSeq = 0, cardSeq = 0, importToken, restoreToken;
  const patch = (url, payload) => api(url, {method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
  function preferenceValues() {
    return {view: state.view, q: $('#query').value, course_id: Number($('#course-filter').value) || null,
      source_id: Number($('#source-filter').value) || null, offset: state.offset,
      favorites_only: $('#favorites-only').checked, conversation_id: Number(state.selected) || null};
  }
  function savePreferences() {
    if (initializing || restoring) return;
    clearTimeout(preferenceTimer);
    preferenceTimer = setTimeout(() => post('/api/preferences', preferenceValues()).catch(showError), 200);
  }
  function pager(prefix, data) {
    $(`#${prefix}-info`).textContent = `第 ${data.total ? Math.floor(data.offset / 20) + 1 : 0} / ${Math.ceil(data.total / 20)} 页 · 共 ${data.total} 条`;
    $(`#${prefix}-previous`).disabled = data.offset === 0;
    $(`#${prefix}-next`).disabled = data.offset + data.limit >= data.total;
  }
  async function jumpToMessage(cid, mid) {
    await switchView('library'); await openConversation(cid, mid);
  }
  async function loadRecent() {
    const data = await api('/api/library/recent');
    $('#reading-recent').innerHTML = data.length ? data.map(item => `<button class="recent-item reading-item" data-cid="${item.conversation_id}" data-mid="${item.last_message_id || ''}"><span><strong>${escapeHtml(item.title)}</strong><small>上次阅读：${escapeHtml(item.last_opened_at)}</small></span><span class="pill">继续阅读</span></button>`).join('') : '<p class="muted">打开会话后，这里会记录最近阅读位置。</p>';
    $$('.reading-item').forEach(button => button.onclick = run(() => jumpToMessage(button.dataset.cid, button.dataset.mid)));
  }
  function trackConversation(data, target) {
    clearTimeout(progressTimer);
    const cid = data.conversation.conversation_id;
    let favorite = Boolean(data.reading_state?.is_favorite);
    const button = document.createElement('button'); button.type = 'button'; button.className = 'small-button favorite-toggle';
    const update = () => { button.textContent = favorite ? '★ 已收藏' : '☆ 收藏'; button.setAttribute('aria-pressed', String(favorite)); };
    update(); $('#detail .panel-head').append(button);
    button.onclick = run(async () => {
      await post('/api/library/favorite', {conversation_id: cid, is_favorite: !favorite});
      favorite = !favorite; update();
      if ($('#favorites-only').checked) await loadConversations();
    });
    activeReading = {conversation_id: cid, message_id: target || data.messages[0]?.message_id || null};
    post('/api/library/progress', activeReading).then(loadRecent).catch(showError);
    scrollIntent = 0;
    $('#detail').onwheel = $('#detail').ontouchmove = () => { scrollIntent = Date.now(); };
    $('#detail').onpointerdown = event => { if (event.target === $('#detail')) scrollIntent = Date.now(); };
    $('#detail').onscroll = () => {
      if (Date.now() - scrollIntent > 2000) return;
      clearTimeout(progressTimer);
      progressTimer = setTimeout(() => {
        if (String(state.selected) !== String(cid) || restoring) return;
        const viewport = $('#detail').getBoundingClientRect();
        const elements = $$('#detail .message');
        const message = elements.map(item => { const rect = item.getBoundingClientRect(); return {item, visible: Math.max(0, Math.min(rect.bottom, viewport.bottom) - Math.max(rect.top, viewport.top))}; }).sort((a,b) => b.visible-a.visible)[0];
        if (message?.visible) {
          activeReading = {conversation_id: cid, message_id: Number(message.item.dataset.messageId)};
          post('/api/library/progress', activeReading).catch(showError);
        }
      }, 250);
    };
  }
  async function loadTodos() {
    const sequence = ++todoSeq;
    const params = new URLSearchParams({status: $('#todo-status').value, limit: 20, offset: todoOffset});
    if ($('#todo-type').value) params.set('annotation_type', $('#todo-type').value);
    $('#todo-list').textContent = '正在加载待办…';
    const data = await api(`/api/todos?${params}`);
    if (sequence !== todoSeq) return;
    $('#todo-list').innerHTML = data.items.length ? data.items.map(item => `<article class="todo-item"><span class="pill">${{QUESTION:'疑问',CORRECTION:'待纠错',IMPORTANT:'重点 / 待练习'}[item.annotation_type]}</span><h3>${escapeHtml(item.content)}</h3><p class="muted">${escapeHtml(item.course_name)} · ${escapeHtml(item.title)} · 消息 #${item.seq_no}</p><div class="review-actions"><button class="small-button todo-jump" data-cid="${item.conversation_id}" data-mid="${item.message_id}">查看原消息</button><button class="small-button todo-resolve" data-id="${item.annotation_id}" data-resolved="${item.is_resolved}">${item.is_resolved ? '重新打开' : '标记已解决'}</button></div></article>`).join('') : '<p class="empty-state">当前条件下没有学习待办。</p>';
    $$('.todo-jump').forEach(button => button.onclick = run(() => jumpToMessage(button.dataset.cid, button.dataset.mid)));
    $$('.todo-resolve').forEach(button => button.onclick = run(async () => {
      button.disabled = true;
      try { await patch(`/api/annotations/${button.dataset.id}`, {is_resolved: button.dataset.resolved !== '1'}); await Promise.all([loadTodos(), loadSummary()]); }
      catch (error) { button.disabled = false; throw error; }
    }));
    pager('todos', data);
  }
  function localDate(value) {
    if (!value) return '';
    const date = new Date(value.includes('T') ? value : value.replace(' ', 'T') + 'Z');
    if (Number.isNaN(date.getTime())) return '';
    return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  }
  async function loadCards() {
    const sequence = ++cardSeq;
    const params = new URLSearchParams({limit: 20, offset: cardOffset});
    if ($('#card-status').value) params.set('status', $('#card-status').value);
    const data = await api(`/api/reviews/cards?${params}`);
    if (sequence !== cardSeq) return;
    $('#card-manager').innerHTML = data.items.length ? data.items.map(card => `<details class="managed-card"><summary>${escapeHtml(card.question)} <span class="pill">${{ACTIVE:'正常复习',SUSPENDED:'已暂停',COMPLETED:'已完成'}[card.status]}</span></summary><p class="muted">下次复习：${escapeHtml(card.due_at)} UTC · 连续答对 ${card.review_count} 次</p><form class="card-edit-form data-form" data-id="${card.review_card_id}"><label>问题<textarea name="question" required>${escapeHtml(card.question)}</textarea></label><label>答案<textarea name="answer" required>${escapeHtml(card.answer)}</textarea></label><label>状态<select name="status">${['ACTIVE','SUSPENDED','COMPLETED'].map(status => `<option value="${status}" ${card.status === status ? 'selected' : ''}>${{ACTIVE:'正常复习',SUSPENDED:'已暂停',COMPLETED:'已完成'}[status]}</option>`).join('')}</select></label><label>下次复习（本地时间）<input type="datetime-local" name="due_at" value="${localDate(card.due_at)}" required></label><button class="button">保存卡片</button></form></details>`).join('') : '<p class="empty-state">当前条件下没有复习卡。</p>';
    $$('.card-edit-form').forEach(form => form.onsubmit = run(async event => {
      event.preventDefault(); const button = form.querySelector('button'); button.disabled = true;
      try {
        await patch(`/api/reviews/cards/${form.dataset.id}`, {question: form.elements.question.value.trim(), answer: form.elements.answer.value.trim(), status: form.elements.status.value, due_at: new Date(form.elements.due_at.value).toISOString()});
        await Promise.all([loadCards(), loadReviews(), loadSummary()]); toast('卡片已保存，复习历史保持不变');
      } finally { button.disabled = false; }
    }));
    pager('cards', data);
  }
  function describeCounts(counts) {
    const names = {conversation:'会话',message:'消息',annotation:'消息批注',review_card:'复习卡',review_log:'复习记录',conversation_state:'收藏与阅读记录',user_preference:'界面设置'};
    return `<dl class="backup-counts">${Object.entries(counts).map(([key,value]) => `<div><dt>${names[key] || escapeHtml(key)}</dt><dd>${value}</dd></div>`).join('')}</dl>`;
  }
  $('#favorites-only').onchange = run(async () => { state.offset = 0; await loadConversations(); });
  $('#todo-status').onchange = $('#todo-type').onchange = run(async () => { todoOffset = 0; await loadTodos(); });
  $('#todos-previous').onclick = run(async () => { todoOffset = Math.max(0, todoOffset - 20); await loadTodos(); });
  $('#todos-next').onclick = run(async () => { todoOffset += 20; await loadTodos(); });
  $('#card-status').onchange = run(async () => { cardOffset = 0; await loadCards(); });
  $('#cards-previous').onclick = run(async () => { cardOffset = Math.max(0, cardOffset - 20); await loadCards(); });
  $('#cards-next').onclick = run(async () => { cardOffset += 20; await loadCards(); });
  document.addEventListener('keydown', event => {
    if (state.view === 'library' && ['PageDown','PageUp','ArrowDown','ArrowUp','Home','End',' '].includes(event.key) && !event.target.closest('input,textarea,select,button')) scrollIntent = Date.now();
    if (event.repeat || state.view !== 'review' || event.target.closest('input,textarea,select,button,[contenteditable="true"]')) return;
    const card = $('.review-card'); if (!card) return;
    if (event.code === 'Space') { event.preventDefault(); card.querySelector('.reveal-answer:not(.hidden)')?.click(); }
    if (['1','2'].includes(event.key) && !card.querySelector('.review-actions').classList.contains('hidden')) {
      event.preventDefault(); const button = card.querySelector(`[data-grade="${event.key === '1' ? 'wrong' : 'correct'}"]`); if (!button.disabled) button.click();
    }
  });
  const dropzone = $('#import-dropzone');
  dropzone.ondragover = event => { event.preventDefault(); dropzone.classList.add('dragging'); };
  dropzone.ondragleave = () => dropzone.classList.remove('dragging');
  dropzone.ondrop = event => { event.preventDefault(); dropzone.classList.remove('dragging'); $('#import-files').files = event.dataTransfer.files; };
  $('#import-form').onsubmit = run(async event => {
    event.preventDefault(); const button = event.currentTarget.querySelector('button'); button.disabled = true;
    try {
      const body = new FormData(); [...$('#import-files').files].forEach(file => body.append('files', file)); body.append('platform', $('#import-platform').value);
      $('#import-preview').textContent = '正在解析并检查重复记录…';
      const data = await api('/api/import/preview', {method: 'POST', body}); importToken = data.token;
      $('#import-preview').innerHTML = `<p>新增 ${data.counts.NEW} · 重复 ${data.counts.DUPLICATE} · 异常 ${data.counts.INVALID}</p><div class="table-scroll"><table><thead><tr><th>选择</th><th>文件 / 会话</th><th>平台</th><th>消息</th><th>检查结果</th></tr></thead><tbody>${data.candidates.map(item => `<tr><td><input type="checkbox" data-import-index="${item.index}" aria-label="选择 ${escapeHtml(item.file_name)}" ${item.status === 'NEW' ? 'checked' : ''} ${item.status === 'INVALID' ? 'disabled' : ''}></td><td>${escapeHtml(item.file_name)}<br>${escapeHtml(item.title || '')}</td><td>${escapeHtml(item.platform || '未识别')}</td><td>${item.message_count}</td><td>${{NEW:'可新增',DUPLICATE:'已存在，将跳过',INVALID:'无法导入'}[item.status]} ${escapeHtml(item.error || '')}</td></tr>`).join('')}</tbody></table></div><button id="start-import" class="button">导入所选文件</button>`;
      $('#start-import').onclick = run(async () => {
        const selected = $$('[data-import-index]:checked').map(input => Number(input.dataset.importIndex));
        if (!selected.length) throw new Error('请先选择至少一个可导入的文件');
        $('#start-import').disabled = true;
        const task = await post('/api/import/tasks', {preview_token: importToken, selected});
        sessionStorage.setItem('studyvault-import-task', task.task_id); await pollImport(task.task_id);
      });
    } catch (error) { $('#import-preview').textContent = '预览未完成，请检查文件或重试。'; throw error; }
    finally { button.disabled = false; }
  });
  async function pollImport(taskId) {
    for (;;) {
      const task = await api(`/api/import/tasks/${taskId}`);
      const finished = ['completed','cancelled'].includes(task.status);
      $('#import-progress').innerHTML = `<h3>${{queued:'正在等待',running:'正在导入',completed:'导入完成',cancelled:'已停止后续导入'}[task.status]}</h3><progress value="${task.processed}" max="${task.total}"></progress><p>已处理 ${task.processed} / ${task.total} · 新增 ${task.imported} · 跳过 ${task.skipped} · 失败 ${task.failed}</p><p class="muted">${escapeHtml(task.current_file)}</p>${!finished ? '<button id="cancel-import" class="small-button">停止后续文件</button>' : ''}<ul>${task.results.map(item => `<li>${escapeHtml(item.file_name)}：${{SUCCESS:'新增成功',SKIPPED:'重复，已跳过',FAILED:'失败'}[item.status]} ${escapeHtml(item.error || '')}</li>`).join('')}</ul>`;
      if (!finished) $('#cancel-import').onclick = run(async () => { await post(`/api/import/tasks/${taskId}/cancel`); $('#cancel-import').disabled = true; $('#cancel-import').textContent = '当前文件完成后停止'; });
      if (finished) { sessionStorage.removeItem('studyvault-import-task'); await loadSummary(); toast('导入结果已更新'); break; }
      await new Promise(resolve => setTimeout(resolve, 350));
    }
  }
  $('#restore-form').onsubmit = run(async event => {
    event.preventDefault(); const button = event.currentTarget.querySelector('button'); button.disabled = true;
    try {
      const body = new FormData(); body.append('file', $('#restore-file').files[0]);
      $('#restore-preview').textContent = '正在校验备份…';
      const data = await api('/api/backup/preview', {method: 'POST', body}); restoreToken = data.token;
      $('#restore-preview').innerHTML = `<h3>备份内容预览</h3>${describeCounts(data.counts)}<label class="restore-confirm"><input type="checkbox" id="confirm-restore-check"> 我确认用此备份替换当前数据；恢复前会自动保存当前库。</label><button class="button" id="confirm-restore" disabled>确认恢复此备份</button>`;
      $('#confirm-restore-check').onchange = () => $('#confirm-restore').disabled = !$('#confirm-restore-check').checked;
      $('#confirm-restore').onclick = run(async () => {
        if (!$('#confirm-restore-check').checked) return;
        $('#confirm-restore').disabled = true; restoring = true;
        clearTimeout(preferenceTimer); clearTimeout(progressTimer); activeReading = null;
        try {
          const result = await post('/api/backup/restore', {token: restoreToken});
          state.offset = 0; clearDetail(); await Promise.all([loadFilters(), loadSummary()]);
          $('#restore-preview').innerHTML = '';
          $('#restore-result').innerHTML = `<h3>恢复完成</h3>${describeCounts(result.counts)}<a class="button ghost" href="${result.safety_backup_url}">下载恢复前备份</a><p class="muted">重新打开页面可恢复备份中的阅读设置。</p>`;
          toast('完整备份已恢复');
        } catch (error) { $('#confirm-restore').disabled = false; throw error; }
        finally { restoring = false; }
      });
    } catch (error) { $('#restore-preview').textContent = '备份校验失败，当前数据保持不变。'; throw error; }
    finally { button.disabled = false; }
  });
  window.addEventListener('pagehide', () => {
    if (initializing || restoring) return;
    navigator.sendBeacon('/api/preferences', new Blob([JSON.stringify(preferenceValues())], {type:'application/json'}));
    if (activeReading) navigator.sendBeacon('/api/library/progress', new Blob([JSON.stringify(activeReading)], {type:'application/json'}));
  });
  window.usability = {loadTodos, loadCards, loadRecent, trackConversation, savePreferences};
  $$('[data-view]').forEach(button => button.disabled = true);
  initialLoad.then(async () => {
    const preferences = await api('/api/preferences');
    if (Object.keys(preferences).length) {
      $('#query').value = preferences.q || '';
      $('#course-filter').value = preferences.course_id || '';
      $('#source-filter').value = preferences.source_id || '';
      $('#favorites-only').checked = Boolean(preferences.favorites_only);
      state.offset = Number.isInteger(preferences.offset) ? Math.max(0, preferences.offset) : 0;
      await switchView(preferences.view || 'overview');
      if (preferences.view === 'library' && preferences.conversation_id) await openConversation(preferences.conversation_id);
    }
    await loadRecent();
    const taskId = sessionStorage.getItem('studyvault-import-task');
    if (taskId) pollImport(taskId).catch(error => { sessionStorage.removeItem('studyvault-import-task'); showError(error); });
  }).catch(showError).finally(() => { initializing = false; window.studyReady = true; $$('[data-view]').forEach(button => button.disabled = false); });
})();
