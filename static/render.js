/* Offline Markdown and math rendering. Raw message strings remain in SQLite. */
(() => {
  const escape = value => String(value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const parser = new marked.Marked({gfm: true, breaks: true});
  parser.use({extensions: [
    {name: 'blockMath', level: 'block', start: source => source.search(/\$\$|\\\[/),
      tokenizer(source) { const match = /^(?:\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\])(?:\n|$)/.exec(source); if (match) return {type: 'blockMath', raw: match[0], text: match[1] || match[2]}; },
      renderer(token) { return `<div class="math-placeholder" data-tex="${encodeURIComponent(token.text)}" data-display="true"></div>`; }},
    {name: 'inlineMath', level: 'inline', start: source => source.search(/\$|\\\(/),
      tokenizer(source) { const match = /^(?:\$([^$\n]+?)\$|\\\(([\s\S]+?)\\\))/.exec(source); if (match) return {type: 'inlineMath', raw: match[0], text: match[1] || match[2]}; },
      renderer(token) { return `<span class="math-placeholder" data-tex="${encodeURIComponent(token.text)}"></span>`; }}
  ]});
  function highlight(node, query) {
    if (!query) return;
    const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
    const nodes = []; while (walker.nextNode()) nodes.push(walker.currentNode);
    for (const text of nodes) {
      if (text.parentElement.closest('.katex,button,textarea,script,style')) continue;
      const source = text.textContent, needle = query.toLowerCase();
      let cursor = 0, index, found = false;
      const fragment = document.createDocumentFragment();
      while ((index = source.toLowerCase().indexOf(needle, cursor)) >= 0) {
        found = true; fragment.append(source.slice(cursor, index));
        const mark = document.createElement('mark'); mark.textContent = source.slice(index, index + query.length);
        fragment.append(mark); cursor = index + query.length;
      }
      if (found) { fragment.append(source.slice(cursor)); text.replaceWith(fragment); }
    }
  }
  async function copyText(text) {
    if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(text);
    const field = document.createElement('textarea'); field.value = text; document.body.append(field); field.select();
    const success = document.execCommand('copy'); field.remove(); if (!success) throw new Error('浏览器未允许复制，请手动选择代码');
  }
  function render(node, source, query = '') {
    node.classList.add('rendered-content');
    try {
      node.innerHTML = DOMPurify.sanitize(parser.parse(String(source)), {USE_PROFILES: {html: true}, FORBID_TAGS: ['img', 'iframe', 'video', 'audio', 'style', 'form', 'input'], FORBID_ATTR: ['style']});
      node.querySelectorAll('.math-placeholder').forEach(element => {
        katex.render(decodeURIComponent(element.dataset.tex), element, {displayMode: element.dataset.display === 'true', throwOnError: false, trust: false, maxExpand: 1000});
      });
      node.querySelectorAll('a').forEach(link => { link.target = '_blank'; link.rel = 'noopener noreferrer'; });
      highlight(node, query);
      node.querySelectorAll('pre').forEach(block => {
        const code = block.querySelector('code');
        if (!code) return;
        const button = document.createElement('button'); button.type = 'button'; button.className = 'copy-code small-button'; button.textContent = '复制代码';
        button.onclick = async () => { try { await copyText(code.textContent); button.textContent = '已复制'; } catch (error) { button.textContent = error.message; } };
        block.prepend(button);
      });
    } catch { node.innerHTML = escape(source); }
  }
  window.StudyRender = {render, copyText};
})();
