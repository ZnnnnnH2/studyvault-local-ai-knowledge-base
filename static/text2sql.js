// Text2SQL 问答视图：自然语言 → SQL → 只读执行 → 表格展示。
// 复用 app.js 的全局 $ / api / post / escapeHtml / showError。
(function () {
  let schemaLoaded = false;
  let formBound = false;
  let offline = false;

  const METHOD_KEY = 't2s-method';
  const METHOD_HINT = {
    c3: '只喂相关表 + 纠偏提示 + 采样投票',
    baseline: '整库 schema 一次生成，温度 0',
  };

  function currentMethod() {
    const checked = document.querySelector('input[name="t2s-method"]:checked');
    return checked ? checked.value : 'c3';
  }

  function setMethod(method) {
    const radio = document.querySelector(`input[name="t2s-method"][value="${method}"]`);
    if (radio) radio.checked = true;
    $('#t2s-method-hint').textContent = METHOD_HINT[currentMethod()] || '';
  }

  // C3 诊断：召回了哪些表/列、采样几条、几条能执行、各结果组票数。
  function diagnosticsHtml(c3) {
    const tables = c3.linked_tables.map(t => `<code>${escapeHtml(t)}</code>`).join('') || '—';
    const colEntries = Object.entries(c3.linked_columns || {});
    const cols = colEntries.length
      ? `<details><summary>${colEntries.reduce((n, [, v]) => n + v.length, 0)} 列</summary><ul>`
        + colEntries.map(([t, v]) => `<li><code>${escapeHtml(t)}</code>${v.map(escapeHtml).join(', ')}</li>`).join('')
        + '</ul></details>'
      : '<span class="t2s-weak">列召回失败，用了召回表的全部列</span>';
    const tally = c3.tally && c3.tally.length ? c3.tally : [c3.votes];
    const failed = Math.max(0, c3.samples - c3.valid);
    const squares = tally.map((n, i) => `<i class="${i === 0 ? 'win' : ''}"></i>`.repeat(n)).join('')
      + '<i class="fail"></i>'.repeat(failed);
    const split = tally.join(' / ');
    const weak = c3.votes * 2 <= c3.valid
      ? ' <span class="t2s-weak">· 票数没过半，结果不太稳，可以换个问法再试</span>' : '';
    const voteLine = `<span class="t2s-votes" aria-hidden="true">${squares}</span>`
      + `采样 ${c3.samples} 条，${c3.valid} 条能执行，按结果分成 ${tally.length} 组（${split}），取 ${c3.votes} 票那组`
      + (failed ? `；${failed} 条生成或执行失败` : '') + weak;
    return `<dl class="t2s-diag">
      <dt>召回表</dt><dd>${tables}</dd>
      <dt>召回列</dt><dd>${cols}</dd>
      <dt>一致性投票</dt><dd>${voteLine}</dd>
    </dl>`;
  }

  function renderResult(data, elapsedMs) {
    const sqlBox = $('#t2s-sql');
    let badge;
    if (data.source !== 'llm') badge = '<span class="t2s-badge fallback">内置规则模板</span>';
    else if (data.method === 'c3') badge = '<span class="t2s-badge llm">大模型 · C3</span>';
    else badge = '<span class="t2s-badge llm">大模型 · 基线</span>';
    const elapsed = elapsedMs != null ? `<span class="t2s-elapsed">${(elapsedMs / 1000).toFixed(1)} 秒</span>` : '';
    sqlBox.classList.remove('hidden');
    sqlBox.innerHTML = `<div class="meta">${badge}${elapsed}</div><pre>${escapeHtml(data.sql)}</pre>`
      + (data.c3 ? diagnosticsHtml(data.c3) : '')
      + (data.note ? `<p class="t2s-note">${escapeHtml(data.note)}</p>` : '');

    const result = $('#t2s-result');
    if (!data.columns.length) {
      result.innerHTML = '<p class="empty-state">查询没有返回任何列。</p>';
      return;
    }
    const head = data.columns.map(c => `<th>${escapeHtml(c)}</th>`).join('');
    const body = data.rows.length
      ? data.rows.map(row => `<tr>${row.map(cell => `<td>${escapeHtml(cell)}</td>`).join('')}</tr>`).join('')
      : `<tr><td colspan="${data.columns.length}" class="empty-state">没有匹配的数据行。</td></tr>`;
    result.innerHTML = `<div class="table-scroll"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`
      + (data.truncated ? `<p class="t2s-truncated">结果较多，仅显示前 ${data.rows.length} 行。</p>` : '');
  }

  async function ask(question) {
    const text = question.trim();
    if (!text) return;
    $('#app-status').textContent = '';
    $('#t2s-question').value = text;
    const status = $('#t2s-status');
    const method = currentMethod();
    status.textContent = method === 'c3' && !offline
      ? '正在召回相关表、采样候选 SQL 并投票…（要调好几次模型，稍等）'
      : '正在生成 SQL 并查询…';
    $('#t2s-sql').classList.add('hidden');
    $('#t2s-result').innerHTML = '';
    const button = $('#t2s-form').querySelector('button');
    button.disabled = true;
    try {
      const started = performance.now();
      const data = await post('/api/text2sql', { question: text, method });
      status.textContent = `共 ${data.rows.length} 行`;
      renderResult(data, performance.now() - started);
    } catch (error) {
      status.textContent = '';
      showError(error);
    } finally {
      button.disabled = false;
    }
  }

  async function load() {
    if (formBound === false) {
      $('#t2s-form').addEventListener('submit', event => {
        event.preventDefault();
        ask($('#t2s-question').value);
      });
      $$('input[name="t2s-method"]').forEach(radio => radio.addEventListener('change', () => {
        setMethod(radio.value);
        try { localStorage.setItem(METHOD_KEY, radio.value); } catch (_) { /* 无痕模式等 */ }
      }));
      let saved = null;
      try { saved = localStorage.getItem(METHOD_KEY); } catch (_) { /* ignore */ }
      setMethod(saved === 'baseline' ? 'baseline' : 'c3');
      formBound = true;
    }
    if (schemaLoaded) return;
    try {
      const info = await api('/api/text2sql/schema');
      $('#t2s-mode').textContent = info.llm_enabled ? `大模型：${info.model}` : '离线规则模式';
      offline = !info.llm_enabled;
      if (offline) {
        $$('input[name="t2s-method"]').forEach(radio => { radio.disabled = true; });
        $('#t2s-method-hint').textContent = '离线模式下走规则模板，不区分方法';
      } else {
        METHOD_HINT.c3 = `只喂相关表 + 纠偏提示 + 采样 ${info.samples} 条投票`;
        setMethod(currentMethod());
      }
      $('#t2s-examples').innerHTML = info.examples
        .map(q => `<button type="button" class="t2s-chip">${escapeHtml(q)}</button>`).join('');
      $$('#t2s-examples .t2s-chip').forEach(chip => chip.onclick = () => ask(chip.textContent));
      schemaLoaded = true;
    } catch (error) {
      $('#t2s-mode').textContent = '不可用';
      showError(error);
    }
  }

  window.askData = { load, ask };
})();
