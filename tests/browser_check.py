"""Real Edge regression checks; --capture-report also records restored-data screenshots."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import socket
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import uvicorn
from playwright.async_api import async_playwright, expect

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
import app
from support import write_bundle


@contextmanager
def serve(database: Path):
    original = app.DB_PATH
    app.DB_PATH = database
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app.app, host='127.0.0.1', port=port, log_level='error'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started:
            if not thread.is_alive() or time.monotonic() >= deadline:
                raise RuntimeError('Test server failed to start')
            threading.Event().wait(.02)
        yield f'http://127.0.0.1:{port}'
    finally:
        server.should_exit = True
        thread.join(15)
        sock.close()
        app.DB_PATH = original


async def regression(browser, base_url: str) -> dict:
    page = await browser.new_page(viewport={'width': 1440, 'height': 1050})
    errors, requests = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: requests.append(request.url) if '/api/conversations' in request.url else None)
    await page.goto(base_url)
    await page.wait_for_function("window.studyReady === true")
    await expect(page.locator('#page-title')).to_have_text('错题本')
    await page.locator('.nav[data-view="overview"]').click()
    await expect(page.locator('[data-count="conversations"] .value')).to_have_text('25')
    await page.locator('.nav[data-view="library"]').click()
    await expect(page.locator('.conversation-item')).to_have_count(20)
    await expect(page.locator('#page-info')).to_contain_text('第 1 / 2 页')
    await page.locator('.conversation-item').first.click()
    await expect(page.locator('.message')).to_have_count(2)
    count = len(requests)
    await page.locator('.annotation-form input').first.fill('保留未提交的输入')
    await page.wait_for_timeout(400)
    assert len(requests) == count, 'opening a conversation still causes repeated requests'
    await expect(page.locator('.annotation-form input').first).to_have_value('保留未提交的输入')
    await page.locator('.annotation-form button').first.click()
    await expect(page.locator('.annotation')).to_contain_text('保留未提交的输入')
    await expect(page.locator('#app-status')).to_have_text('')
    await page.locator('#next-page').click()
    await expect(page.locator('.conversation-item')).to_have_count(5)
    await expect(page.locator('#page-info')).to_contain_text('第 2 / 2 页')
    await expect(page.locator('#detail')).to_contain_text('读全文')
    await page.locator('#query').fill('样例 00')
    await page.locator('#search-button').click()
    await expect(page.locator('.conversation-item')).to_have_count(1)
    await expect(page.locator('#page-info')).to_contain_text('第 1 / 1 页')
    await page.locator('#source-filter').select_option(label='Pi')
    await expect(page.locator('#conversation-list')).to_contain_text('没找到')
    await expect(page.locator('#page-info')).to_contain_text('共 0 条')
    await page.locator('#source-filter').select_option('')
    await page.locator('#query').fill('')
    await page.locator('#search-button').click()
    await expect(page.locator('.conversation-item')).to_have_count(20)

    # Force older detail and list responses to arrive after the newer request.
    first_id = await page.locator('.conversation-item').nth(0).get_attribute('data-id')
    second_title = await page.locator('.conversation-item strong').nth(1).text_content()
    async def slow_detail(route):
        response = await route.fetch()
        if urlsplit(route.request.url).path.endswith('/' + first_id):
            await asyncio.sleep(.4)
        await route.fulfill(response=response)
    await page.route('**/api/conversations/*', slow_detail)
    await page.locator('.conversation-item').nth(0).click()
    await page.locator('.conversation-item').nth(1).click()
    await expect(page.locator('#detail h2')).to_have_text(second_title)
    await page.wait_for_timeout(550)
    await expect(page.locator('#detail h2')).to_have_text(second_title)
    await page.unroute('**/api/conversations/*', slow_detail)

    async def slow_list(route):
        response = await route.fetch()
        if parse_qs(urlsplit(route.request.url).query).get('q') == ['older-no-match']:
            await asyncio.sleep(.4)
        await route.fulfill(response=response)
    await page.route('**/api/conversations?*', slow_list)
    await page.locator('#query').fill('older-no-match')
    await page.locator('#search-button').click()
    await page.locator('#query').fill('样例 00')
    await page.locator('#search-button').click()
    await expect(page.locator('.conversation-item')).to_have_count(1)
    await page.wait_for_timeout(550)
    await expect(page.locator('.conversation-item')).to_have_count(1)
    await page.unroute('**/api/conversations?*', slow_list)

    async def failed_list(route):
        await route.fulfill(status=500, json={'detail': '受控失败：请重试'})
    await page.route('**/api/conversations?*', failed_list)
    await page.locator('#search-button').click()
    await expect(page.locator('#app-status')).to_contain_text('受控失败')
    await page.unroute('**/api/conversations?*', failed_list)
    await page.locator('#search-button').click()
    await expect(page.locator('.conversation-item')).to_have_count(1)
    await expect(page.locator('#app-status')).to_have_text('')
    await page.locator('.conversation-item').first.click()
    await expect(page.locator('.message')).to_have_count(2)

    for index, grade in [(1, 'correct'), (0, 'wrong')]:
        if index == 0:
            await page.locator('.nav[data-view="library"]').click()
            await page.locator('.conversation-item').first.click()
            await expect(page.locator('.message')).to_have_count(2)
        message = page.locator('.message').nth(index)
        await message.locator('.add-review').click()
        await expect(message.locator('[name="answer"]')).not_to_have_value('')
        await message.locator('[name="question"]').fill('外键维护什么完整性？')
        await message.locator('.review-form button').click()
        await expect(message.locator('.added-review')).to_have_text('已加入复习')
        await page.locator('.nav[data-view="review"]').click()
        await expect(page.locator('.review-card')).to_have_count(1)
        await page.locator('.reveal-answer').first.click()
        await page.locator(f'[data-grade="{grade}"]').click()
        await expect(page.locator('#review-list')).to_contain_text('今天没卡要复习')
    await page.reload()
    await page.wait_for_function("window.studyReady === true")
    await page.locator('.nav[data-view="library"]').click()
    await page.locator('#query').fill('样例 00')
    await page.locator('#search-button').click()
    await expect(page.locator('.conversation-item')).to_have_count(1)
    await page.locator('.conversation-item').first.click()
    await expect(page.locator('.added-review')).to_have_count(2)

    async with page.expect_download() as download_info:
        await page.locator('.topbar-actions a[href="/api/export/conversations.json"]').click()
    download = await download_info.value
    data = json.loads(Path(await download.path()).read_text(encoding='utf-8'))
    assert len(data) == 25 and sum(len(x['messages']) for x in data) == 50
    await page.locator('#course-filter').select_option(label='数据库系统概论')
    await expect(page.locator('#course-export')).to_be_visible()
    async with page.expect_download() as download_info:
        await page.locator('#course-export').click()
    markdown = Path(await (await download_info.value).path()).read_text(encoding='utf-8')
    assert markdown.count('## SQL 外键学习') == 25
    await page.locator('.nav[data-view="data"]').click()
    for endpoint, name in [('/api/export/structured.jsonl', 'structured_manifest_final.jsonl'),
                           ('/api/export/manual-annotations.jsonl', 'manual_annotations_all.jsonl')]:
        async with page.expect_download() as download_info:
            await page.locator(f'.snapshot-links a[href="{endpoint}"]').click()
        path = Path(await (await download_info.value).path())
        assert hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256((BASE / 'staging' / name).read_bytes()).digest()
    await page.locator('.nav[data-view="stats"]').click()
    await expect(page.locator('#source-stats')).to_contain_text('Codex')
    assert errors == [], errors
    result = {'passed': True, 'browser': 'Microsoft Edge (headless)', 'javascript_errors': errors,
              'checks': ['20/5 pagination', 'draft stable/no request loop', 'annotation save',
                         'filter resets page', 'empty results', 'late detail ignored', 'late list ignored',
                         'request error and retry', 'create immediately due card', 'correct/wrong review',
                         'reload preserves cards', 'current JSON and course Markdown downloads',
                         'historical snapshot download hashes', 'statistics view']}
    await page.close()
    return result


async def capture_report(browser, base_url: str) -> dict:
    """Screenshots for the report, on the demo semester (app.py --demo) in a temporary database."""
    output = BASE / 'docs' / 'screenshots'
    output.mkdir(parents=True, exist_ok=True)
    for old in output.glob('*.png'):
        old.unlink()
    page = await browser.new_page(viewport={'width': 1440, 'height': 1050}, device_scale_factor=1)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    async def screenshot(name, full=False):
        await expect(page.locator('.toast.show')).to_have_count(0)
        await page.screenshot(path=str(output / name), full_page=full)
    await page.goto(base_url)
    await page.wait_for_function("window.studyReady === true")
    await expect(page.locator('.eb-group').first).to_be_visible()
    await screenshot('01-error-book.png')
    await page.locator('#eb-kind label:has-text("AI 答错")').click()
    await expect(page.locator('.eb-item.kind-correction').first).to_be_visible()
    await screenshot('02-error-book-ai-mistakes.png')
    # Follow an AI mistake back to the exact message it was written on.
    await page.locator('.eb-item.kind-correction .eb-source').first.click()
    await expect(page.locator('.message.search-hit')).to_be_visible()
    await page.locator('.message.search-hit .annotation.type-correction').first.scroll_into_view_if_needed()
    await screenshot('03-source-message.png')
    message = page.locator('.message.user').first
    await message.locator('.annotation-form select').select_option('QUESTION')
    await message.locator('.annotation-form input').fill('换成多对多的例子，这个结论还成立吗？')
    await message.locator('.annotation-form button').click()
    await expect(message.locator('.annotation.type-question')).to_have_count(1)
    await message.locator('.annotation.type-question').scroll_into_view_if_needed()
    await screenshot('04-new-doubt.png')
    await page.locator('.nav[data-view="review"]').click()
    await expect(page.locator('.review-card').first).to_be_visible()
    await page.locator('.reveal-answer').first.click()
    await screenshot('05-review.png')
    await page.locator('.nav[data-view="library"]').click()
    await expect(page.locator('#conversation-list[aria-busy="false"]')).to_be_visible()
    await expect(page.locator('#page-info')).to_contain_text('第 1 /')
    await screenshot('06-library.png')
    await page.locator('.nav[data-view="stats"]').click()
    await expect(page.locator('#course-stats .stat-row').first).to_be_visible()
    await screenshot('07-statistics.png')
    await page.locator('.nav[data-view="ask"]').click()
    await expect(page.locator('#t2s-mode')).not_to_have_text('检测中…')
    await page.locator('#t2s-question').fill('哪门课我还没弄懂的疑问最多？')
    await page.locator('#t2s-form button').click()
    await expect(page.locator('#t2s-result table, #t2s-result .empty-state, #app-status:not(:empty)').first).to_be_visible(timeout=90000)
    await screenshot('08-text2sql.png')
    await page.locator('.nav[data-view="data"]').click()
    from test_chatgpt_export import export_fixture
    export = Path(tempfile.mkdtemp(prefix='studyvault-export-')) / 'conversations.json'
    export.write_text(json.dumps(export_fixture(), ensure_ascii=False), encoding='utf-8')
    await page.locator('#import-files').set_input_files(str(export))
    await page.locator('#import-form button').click()
    await expect(page.locator('#import-preview table')).to_be_visible()
    await screenshot('09-chatgpt-import.png')
    assert errors == [], errors
    result = {'screenshots': sorted(p.name for p in output.glob('*.png')),
              'dataset': 'app.py --demo in a temporary database (synthetic conversations + simulated semester)',
              'demonstration_note': 'All data is synthetic. One doubt was added during capture; the ChatGPT import was previewed, not imported.'}
    await page.close()
    return result


async def main(capture: bool):
    with tempfile.TemporaryDirectory(prefix='studyvault-browser-') as directory:
        root = Path(directory)
        original = app.DB_PATH
        try:
            app.DB_PATH = root / 'controlled.db'
            files = write_bundle(root / 'fixtures', 25)
            app.restore_bundled(*files)
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(channel='msedge', headless=True)
                try:
                    with serve(app.DB_PATH) as base_url:
                        evidence = await regression(browser, base_url)
                    if capture:
                        app.DB_PATH = root / 'demo.db'
                        evidence['restore'] = app.restore_bundled()
                        evidence['activity'] = app.seed_demo_activity()
                        with serve(app.DB_PATH) as base_url:
                            evidence['report'] = await capture_report(browser, base_url)
                        (BASE / 'docs' / 'IMPLEMENTATION_EVIDENCE.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
                    print(json.dumps(evidence, ensure_ascii=False, indent=2))
                finally:
                    await browser.close()
        finally:
            app.DB_PATH = original


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--capture-report', action='store_true')
    asyncio.run(main(parser.parse_args().capture_report))
