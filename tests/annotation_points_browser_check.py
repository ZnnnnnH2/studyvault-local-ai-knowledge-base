"""Exercise direct note-to-point links and the knowledge-point note aggregation page."""
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
    output = BASE / 'docs' / 'screenshots-annotation-points'
    output.mkdir(exist_ok=True)
    page = await browser.new_page(viewport={'width': 1440, 'height': 1150})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))

    async def screenshot(name):
        await page.wait_for_timeout(500)
        await expect(page.locator('.toast.show')).to_have_count(0)
        await expect(page.locator('#toast')).to_have_css('opacity', '0')
        await page.wait_for_timeout(100)
        await page.screenshot(path=str(output / name))

    await page.goto(url)
    await page.wait_for_function('window.studyReady === true')
    await page.locator('.nav[data-view="library"]').click()
    await page.locator('.conversation-item').first.click()
    await expect(page.locator('.annotation-form')).to_have_count(2)

    form = page.locator('.annotation-form').first
    await form.locator('select').select_option('NOTE')
    await form.locator('input[aria-label="批注内容"]').fill('只讨论外键：参照完整性把子表关联到父表。')
    await form.locator('.annotation-point-edit summary').click()
    await form.locator('.point-choice', has_text='SQL 与查询').locator('input').check()
    await form.locator('.point-choice', has_text='数据库完整性').locator('input').check()
    await form.locator('button[type="submit"]').click()
    await expect(page.locator('.annotation', has_text='只讨论外键')).to_be_visible()
    annotation = page.locator('.annotation', has_text='只讨论外键')
    await expect(annotation.locator('.annotation-points')).to_contain_text('SQL 与查询')
    await expect(annotation.locator('.annotation-points')).to_contain_text('数据库完整性')
    await screenshot('01-note-point-link.png')

    # Open the point page from the vocabulary list and aggregate both notes.
    await page.locator('.nav[data-view="vocab"]').click()
    await page.locator('[data-vocab-kind="points"]').click()
    await page.locator('#vocab-course').select_option(label='数据库系统概论')
    row = page.locator('#vocab-list .vocab-row').filter(has=page.locator('.vocab-name:text-is("SQL 与查询")'))
    await expect(row).to_contain_text('笔记 1')
    await row.locator('.point-notes-open').click()
    await expect(page.locator('#point-notes-view')).to_be_visible()
    await expect(page.locator('#point-notes-title')).to_have_text('SQL 与查询')
    await expect(page.locator('#point-note-list')).to_contain_text('只讨论外键')
    await expect(page.locator('#point-note-list')).to_contain_text('消息笔记')

    await page.locator('#point-note-content').fill('独立笔记：外键的删除行为要结合业务规则判断。')
    await page.locator('#point-note-form button').click()
    await expect(page.locator('#point-note-list')).to_contain_text('独立笔记：外键的删除行为')
    await expect(page.locator('#point-note-list .point-note-item')).to_have_count(2)

    # Edit the independent note in place.
    independent = page.locator('#point-note-list .point-note-item', has_text='独立笔记：外键的删除行为')
    await independent.locator('.point-note-edit summary').click()
    await independent.locator('.point-note-edit-form textarea').fill('独立笔记（已修订）：删除父表行前先检查子表引用。')
    await independent.locator('.point-note-edit-form button').click()
    await expect(page.locator('#point-note-list')).to_contain_text('删除父表行前先检查')
    await screenshot('02-point-notes.png')

    # The message note jumps back to its exact source message.
    message_note = page.locator('#point-note-list .point-note-item', has_text='消息笔记')
    await message_note.locator('.point-note-jump').click()
    await expect(page.locator('.message.search-hit')).to_have_count(1)
    await expect(page.locator('.message.search-hit .message-content')).to_contain_text('什么是外键')

    assert errors == [], errors
    result = {'passed': True,
              'dataset': '5 controlled fixture conversations; not the bundled historical corpus',
              'javascript_errors': errors,
              'checks': ['message note saves two direct point links',
                         'knowledge point page aggregates a message note and an independent note',
                         'independent note can be edited in place',
                         'message note jumps back to the source message'],
              'screenshots': sorted(path.name for path in output.glob('*.png'))}
    await page.close()
    return result


async def main():
    original = app.DB_PATH
    with tempfile.TemporaryDirectory(prefix='studyvault-annotation-points-') as temporary:
        try:
            root = Path(temporary)
            app.DB_PATH = root / 'test.db'
            app.restore_bundled(*write_bundle(root / 'fixtures', 5))
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(channel='msedge', headless=True)
                try:
                    with serve(app.DB_PATH) as url:
                        result = await check(browser, url)
                    (BASE / 'docs' / 'ANNOTATION_POINTS_EVIDENCE.json').write_text(
                        json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                finally:
                    await browser.close()
        finally:
            app.DB_PATH = original


if __name__ == '__main__':
    asyncio.run(main())
