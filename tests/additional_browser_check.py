"""Exercise added features in real Edge, using explicitly labelled controlled data."""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

from playwright.async_api import async_playwright, expect

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
import app
from browser_check import serve
from support import write_bundle


async def check(browser, url):
    output = BASE / 'docs' / 'screenshots-additional'
    output.mkdir(exist_ok=True)
    page = await browser.new_page(viewport={'width': 1440, 'height': 1050})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    async def screenshot(name):
        await expect(page.locator('.toast.show')).to_have_count(0)
        await expect(page.locator('#toast')).to_have_css('opacity', '0')
        await page.screenshot(path=str(output / name))
    await page.goto(url)
    await expect(page.locator('[data-count="conversations"] .value')).to_have_text('25')
    await page.locator('.nav[data-view="library"]').click()
    await page.locator('#query').fill('维护参照完整性')
    await page.locator('#search-button').click()
    await expect(page.locator('.match-snippet')).to_have_count(20)
    await expect(page.locator('.match-snippet mark').first).to_have_text('维护参照完整性')
    await page.locator('.conversation-item').first.click()
    await expect(page.locator('.message.search-hit .speaker')).to_contain_text('#2')
    await expect(page.locator('.message.search-hit mark')).to_have_text('维护参照完整性')
    await screenshot('01-message-search.png')

    await page.locator('.curation-editor summary').click()
    form = page.locator('#curation-form')
    await form.locator('[name="course_id"]').select_option(label='程序设计与工程实践')
    await form.locator('[name="summary"]').fill('人工确认：本条学习资料说明外键如何维护参照完整性。')
    await form.locator('[name="tags"]').fill('已确认、数据库基础')
    await form.locator('button').click()
    await expect(page.locator('.curation-provenance')).to_contain_text('人工整理')
    await expect(page.locator('#detail .detail-summary')).to_contain_text('人工确认')
    await expect(page.locator('#detail .detail-meta')).to_contain_text('已确认')
    await page.locator('.curation-editor summary').click()
    await page.locator('#curation-form').scroll_into_view_if_needed()
    await screenshot('02-manual-curation.png')
    current_id = await page.locator('.conversation-item.active').get_attribute('data-id')
    with app.connect() as db:
        assert db.execute("SELECT content FROM message WHERE conversation_id=? AND seq_no=2", (current_id,)).fetchone()[0] == '外键将子表关联到父表，维护参照完整性。'

    for index, grade in [(1, 'correct'), (0, 'wrong')]:
        if index == 0:
            await page.locator('.nav[data-view="library"]').click()
            await page.locator(f'.conversation-item[data-id="{current_id}"]').click()
        message = page.locator('.message').nth(index)
        await message.locator('.add-review').click()
        await message.locator('[name="question"]').fill('外键如何维护参照完整性？')
        await message.locator('.review-form button').click()
        await expect(message.locator('.added-review')).to_be_visible()
        await page.locator('.nav[data-view="review"]').click()
        await expect(page.locator('.review-card')).to_have_count(1)
        await page.locator('.reveal-answer').first.click()
        await page.locator(f'[data-grade="{grade}"]').click()
        await expect(page.locator('#review-history tbody tr')).to_have_count(1 if index == 1 else 2)
    await screenshot('03-review-history.png')
    await page.reload()
    await page.wait_for_function("window.studyReady === true")
    await page.locator('.nav[data-view="review"]').click()
    await expect(page.locator('#review-history tbody tr')).to_have_count(2)
    await page.locator('.nav[data-view="stats"]').click()
    await expect(page.locator('#review-stats')).to_have_text('累计复习 2 次 · 答对 1 次 · 答错 1 次')
    assert errors == [], errors
    result = {'passed': True, 'dataset': '25 controlled fixture conversations; not the bundled historical corpus',
              'javascript_errors': errors, 'checks': ['matching snippet and safe highlight', 'jump to matched message #2',
              'edit course summary and tags in browser', 'original message unchanged', 'curation provenance visible',
              'correct/wrong review logs visible', 'history survives reload', 'review outcome statistics'],
              'screenshots': sorted(path.name for path in output.glob('*.png'))}
    await page.close()
    return result


async def main():
    original = app.DB_PATH
    with tempfile.TemporaryDirectory(prefix='studyvault-additional-') as temporary:
        try:
            root = Path(temporary)
            app.DB_PATH = root / 'test.db'
            app.restore_bundled(*write_bundle(root / 'fixtures', 25))
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(channel='msedge', headless=True)
                try:
                    with serve(app.DB_PATH) as url:
                        result = await check(browser, url)
                    (BASE / 'docs' / 'ADDITIONAL_FEATURES_EVIDENCE.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                finally:
                    await browser.close()
        finally:
            app.DB_PATH = original


if __name__ == '__main__':
    asyncio.run(main())
