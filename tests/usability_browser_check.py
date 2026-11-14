"""Seven usability workflows in real Edge, with controlled and reversible test data."""
import asyncio
import json
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

from playwright.async_api import async_playwright, expect

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
import app
from browser_check import serve
from support import write_bundle, dump_jsonl


def upload_bytes(identity):
    return '\n'.join(json.dumps(row) for row in [
        {'type': 'session_meta', 'payload': {'id': identity}},
        {'type': 'response_item', 'payload': {'role': 'user', 'content': [{'type': 'input_text', 'text': 'SQL question'}]}},
        {'type': 'response_item', 'payload': {'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'SQL answer'}]}},
    ]).encode()


async def check(browser, url, root):
    output = BASE / 'docs' / 'screenshots-usability'; output.mkdir(exist_ok=True)
    context = await browser.new_context(viewport={'width': 1440, 'height': 1100}, permissions=['clipboard-read', 'clipboard-write'])
    page = await context.new_page()
    errors, external = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: external.append(request.url) if not request.url.startswith(url) else None)
    async def screenshot(name):
        await expect(page.locator('.toast.show')).to_have_count(0)
        await expect(page.locator('#toast')).to_have_css('opacity', '0')
        await page.screenshot(path=str(output / name))
    await page.goto(url)
    await page.wait_for_function('window.studyReady === true')
    await page.locator('.nav[data-view="library"]').click()
    await page.locator('#query').fill('hello')
    await page.locator('#search-button').click()
    await expect(page.locator('.conversation-item')).to_have_count(1)
    cid = await page.locator('.conversation-item').get_attribute('data-id')
    await page.locator('.conversation-item').click()
    await expect(page.locator('#detail .message-content h1')).to_have_text('外键学习笔记')
    await expect(page.locator('#detail .message-content .katex')).to_have_count(2)
    await expect(page.locator('#detail .message-content table')).to_have_count(1)
    await expect(page.locator('#detail img,#detail script')).to_have_count(0)
    assert not await page.evaluate('window.XSS || false')
    await page.locator('.copy-code').click()
    await expect(page.locator('.copy-code')).to_have_text('已复制')
    assert 'hello' in await page.evaluate('navigator.clipboard.readText()')
    await screenshot('01-markdown-math-code.png')
    await page.locator('.favorite-toggle').click()
    await expect(page.locator('.favorite-toggle')).to_have_attribute('aria-pressed', 'true')
    await page.locator('#favorites-only').check()
    await expect(page.locator('.conversation-item')).to_have_count(1)
    await page.wait_for_timeout(500)
    await page.reload()
    await page.wait_for_function('window.studyReady === true')
    await expect(page.locator('#query')).to_have_value('hello')
    await expect(page.locator('#favorites-only')).to_be_checked()
    await expect(page.locator('.message.search-hit .speaker')).to_contain_text('#2')
    await expect(page.locator('.favorite-toggle')).to_have_attribute('aria-pressed', 'true')
    await screenshot('02-favorite-reading.png')

    message = page.locator('.message').nth(1)
    await message.locator('.annotation-form select').select_option('QUESTION')
    await message.locator('.annotation-form input').fill('需要再验证外键删除时的行为。')
    await message.locator('.annotation-form button').click()
    await expect(page.locator('.annotation')).to_have_count(1)
    await page.locator('.nav[data-view="todos"]').click()
    await expect(page.locator('.todo-item')).to_have_count(1)
    await screenshot('03-learning-todos.png')
    await page.locator('.todo-resolve').click()
    await expect(page.locator('#todo-list')).to_contain_text('没有学习待办')
    await page.locator('#todo-status').select_option('resolved')
    await expect(page.locator('.todo-item')).to_have_count(1)
    await page.locator('.todo-resolve').click()
    await page.locator('#todo-status').select_option('open')
    await expect(page.locator('.todo-item')).to_have_count(1)
    await page.locator('.todo-jump').click()
    await expect(page.locator('.message.search-hit .speaker')).to_contain_text('#2')
    message = page.locator('.message').nth(1)
    await message.locator('.add-review').click()
    await message.locator('[name="question"]').fill('外键的作用是什么？')
    await message.locator('[name="answer"]').fill('外键用于维护参照完整性。')
    await message.locator('.review-form button').click()
    await expect(message.locator('.added-review')).to_be_visible()
    await page.locator('.nav[data-view="review"]').click()
    await expect(page.locator('.review-card')).to_have_count(1)
    await expect(page.locator('.review-answer')).to_be_hidden()
    await expect(page.locator('.review-card .review-actions')).to_be_hidden()
    await screenshot('04-recall-before-answer.png')
    await page.locator('#page-title').click()
    await page.keyboard.press('Space')
    await expect(page.locator('.review-answer')).to_contain_text('参照完整性')
    await page.keyboard.press('2')
    await expect(page.locator('#review-list')).to_contain_text('今天没有到期复习卡')
    await expect(page.locator('#review-history tbody tr')).to_have_count(1)
    await page.locator('.managed-card summary').click()
    form = page.locator('.card-edit-form')
    await form.locator('[name="question"]').fill('编辑后的问题：外键保护什么？')
    await form.locator('[name="status"]').select_option('SUSPENDED')
    await form.locator('button').click()
    await expect(page.locator('.managed-card summary')).to_contain_text('已暂停')
    await page.locator('.managed-card summary').click()
    await form.scroll_into_view_if_needed()
    await screenshot('05-card-management.png')
    await form.locator('[name="status"]').select_option('ACTIVE')
    await form.locator('[name="due_at"]').fill('2020-01-01T09:00')
    await form.locator('button').click()
    await expect(page.locator('.review-card')).to_have_count(1)
    await page.locator('.reveal-answer').click()
    await page.locator('[data-grade="wrong"]').click()
    await expect(page.locator('#review-history tbody tr')).to_have_count(2)

    await page.locator('.nav[data-view="data"]').click()
    await page.wait_for_timeout(300)
    async with page.expect_download() as download_info:
        await page.locator('#download-backup').click()
    backup = root / 'complete.sqlite3'; await (await download_info.value).save_as(backup)
    with closing(sqlite3.connect(backup)) as db:
        assert db.execute('SELECT COUNT(*) FROM review_log').fetchone()[0] == 2
        assert db.execute('SELECT COUNT(*) FROM annotation').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM conversation_state WHERE is_favorite=1').fetchone()[0] == 1
    files = [root/'new.jsonl', root/'duplicate.jsonl', root/'broken.jsonl']
    for path, content in zip(files, [upload_bytes('browser-new'), upload_bytes('fixture-00'), b'{bad']): path.write_bytes(content)
    await page.locator('#import-files').set_input_files(files)
    await page.locator('#import-form button').click()
    await expect(page.locator('#import-preview')).to_contain_text('新增 1 · 重复 1 · 异常 1')
    await screenshot('06-import-preview.png')
    await page.locator('#start-import').click()
    await expect(page.locator('#import-progress')).to_contain_text('导入完成')
    await expect(page.locator('#import-progress')).to_contain_text('新增 1')
    assert (await (await page.request.get(url+'/api/summary')).json())['counts']['conversations'] == 26
    await screenshot('07-import-progress.png')
    await page.locator('#restore-file').set_input_files(backup)
    await page.locator('#restore-form button').click()
    await expect(page.locator('#restore-preview h3')).to_have_text('备份内容预览')
    await expect(page.locator('#confirm-restore')).to_be_disabled()
    assert (await (await page.request.get(url+'/api/summary')).json())['counts']['conversations'] == 26
    await page.locator('#confirm-restore-check').check()
    await page.locator('#confirm-restore').click()
    await expect(page.locator('#restore-result h3')).to_have_text('恢复完成')
    assert (await (await page.request.get(url+'/api/summary')).json())['counts']['conversations'] == 25
    async with page.expect_download() as download_info:
        await page.locator('#restore-result a').click()
    safety = root / 'before-restore.sqlite3'; await (await download_info.value).save_as(safety)
    with closing(sqlite3.connect(safety)) as db:
        assert db.execute('SELECT COUNT(*) FROM conversation').fetchone()[0] == 26
    await page.locator('#restore-result').scroll_into_view_if_needed()
    await screenshot('08-complete-backup-restore.png')
    assert errors == [], errors
    assert external == [], external
    result = {'passed': True, 'dataset': '25 controlled conversations, one temporary upload and round-trip backup restore',
              'browser': 'Microsoft Edge headless', 'javascript_errors': errors, 'external_page_requests': external,
              'checks': ['Markdown headings tables math', 'safe rendering of script/image input', 'copy code',
                         'favorites filter', 'restore filters selected conversation and message position after reload',
                         'todo resolve reopen and source jump', 'answer hidden until reveal', 'Space and 2 keyboard review',
                         'edit suspend resume card without losing history', 'upload NEW DUPLICATE INVALID preview',
                         'background import progress', 'complete SQLite download includes learning state',
                         'restore preview does not write data', 'confirmed restore removes later import', 'download pre-restore safety snapshot'],
              'screenshots': sorted(path.name for path in output.glob('*.png'))}
    await context.close()
    return result


async def main():
    original = app.DB_PATH
    with tempfile.TemporaryDirectory(prefix='studyvault-usability-browser-') as directory:
        try:
            root = Path(directory); app.DB_PATH = root/'test.db'
            manifest, notes = write_bundle(root/'fixtures', 25)
            rows = [json.loads(line) for line in manifest.read_text(encoding='utf-8').splitlines()]
            rows[-1]['messages'][1]['content'] = '# 外键学习笔记\n\n这是 **重点**，公式 $x^2+y^2=z^2$。\n\n$$\n\\sum_{i=1}^{n} i=\\frac{n(n+1)}{2}\n$$\n\n| 概念 | 作用 |\n| --- | --- |\n| 外键 | 完整性 |\n\n```python\nprint("hello")\n```\n\n<script>window.XSS=true</script>\n<img src="https://invalid.example/tracker" onerror="window.XSS=true">'
            dump_jsonl(manifest, rows); app.restore_bundled(manifest, notes)
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(channel='msedge', headless=True)
                try:
                    with serve(app.DB_PATH) as url: result = await check(browser, url, root)
                    (BASE/'docs/USABILITY_EVIDENCE.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                finally:
                    await browser.close()
        finally:
            app.DB_PATH = original


if __name__ == '__main__':
    asyncio.run(main())
