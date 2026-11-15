"""Exercise existing-vocabulary selection, near-duplicate prompts and merge/rename in real Edge."""
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
    output = BASE / 'docs' / 'screenshots-vocabulary'
    output.mkdir(exist_ok=True)
    page = await browser.new_page(viewport={'width': 1440, 'height': 1100})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))

    async def screenshot(name):
        await expect(page.locator('.toast.show')).to_have_count(0)
        await expect(page.locator('#toast')).to_have_css('opacity', '0')
        await page.wait_for_timeout(100)
        await page.screenshot(path=str(output / name))

    def row_for(name):
        return page.locator('#vocab-list .vocab-row').filter(
            has=page.locator(f'.vocab-name:text-is("{name}")'))

    await page.goto(url)
    await page.wait_for_function("window.studyReady === true")
    await page.locator('.nav[data-view="vocab"]').click()
    await expect(page.locator('#vocab-title')).to_have_text('标签词库')
    await expect(row_for('数据库')).to_have_count(1)

    # Existing vocabulary is shown before creation, and a near-duplicate is held for confirmation.
    await page.locator('#vocab-create-name').fill('外键')
    await page.locator('#vocab-create-form button').click()
    await expect(row_for('外键')).to_have_count(1)
    await page.locator('#vocab-create-name').fill('外键约束')
    await page.locator('#vocab-create-form button').click()
    await expect(page.locator('#vocab-create-conflict')).to_be_visible()
    await expect(page.locator('#vocab-create-conflict')).to_contain_text('已有相近标签')
    await page.locator('#vocab-create-conflict .vocab-create-anyway').click()
    await expect(row_for('外键约束')).to_have_count(1)

    # Merge the near-duplicate into the existing term; the old name becomes an alias.
    row = row_for('外键约束')
    await row.locator('.vocab-merge summary').click()
    await row.locator('.vocab-merge-form select').select_option(label='外键')
    await row.locator('.vocab-merge-form button').click()
    await expect(row_for('外键约束')).to_have_count(0)
    await expect(row_for('外键')).to_contain_text('别名')
    await screenshot('01-vocab-tags.png')

    # Rename keeps the previous spelling as an alias too.
    row = row_for('外键')
    await row.locator('.vocab-rename').click()
    await row.locator('.vocab-rename-form input[name="name"]').fill('参照完整性')
    await row.locator('.vocab-rename-form button[type="submit"]').click()
    await expect(row_for('参照完整性')).to_contain_text('外键')

    # Knowledge points use the same choose-before-create and merge flow.
    await page.locator('[data-vocab-kind="points"]').click()
    await expect(page.locator('#vocab-title')).to_have_text('知识点词库')
    await page.locator('#vocab-course').select_option(label='数据库系统概论')
    await expect(row_for('SQL 与查询')).to_have_count(1)
    await page.locator('#vocab-create-name').fill('SQL查询')
    await page.locator('#vocab-create-form button').click()
    await expect(page.locator('#vocab-create-conflict')).to_be_visible()
    await page.locator('#vocab-create-conflict .vocab-create-anyway').click()
    await expect(row_for('SQL查询')).to_have_count(1)
    row = row_for('SQL查询')
    await row.locator('.vocab-merge summary').click()
    await row.locator('.vocab-merge-form select').select_option(label='SQL 与查询 · 数据库系统概论')
    await row.locator('.vocab-merge-form button').click()
    await expect(row_for('SQL查询')).to_have_count(0)
    await screenshot('02-vocab-points.png')

    # Conversation detail: existing vocab appears in the picker; new terms are created explicitly.
    await page.locator('.nav[data-view="library"]').click()
    await page.locator('.conversation-item').first.click()
    await expect(page.locator('#tag-editor .vocab-picker-input')).to_be_visible()
    tag_input = page.locator('#tag-editor .vocab-picker-input')
    await tag_input.click()
    await expect(page.locator('#tag-editor .vocab-picker-menu .vocab-option').first).to_be_visible()
    await screenshot('04-existing-tag-menu.png')
    await tag_input.fill('错题')
    await tag_input.press('Enter')
    await expect(page.locator('#tag-editor .vocab-chip:has-text("错题")')).to_have_count(1)

    # A second near-duplicate uses the picker's own confirmation panel.
    await tag_input.fill('错题整理')
    await tag_input.press('Enter')
    await expect(page.locator('#tag-editor .vocab-picker-conflict')).to_be_visible()
    await expect(page.locator('#tag-editor .vocab-picker-conflict')).to_contain_text('已有相近标签')
    await page.locator('#tag-editor .vocab-picker-conflict .vocab-use-existing').first.click()
    await expect(page.locator('#tag-editor .vocab-chip:has-text("错题整理")')).to_have_count(0)
    # Linking a tag requests a fresh conversation detail; let the editor remount
    # before typing into the point picker, otherwise fill/press can target two
    # different DOM nodes.
    await page.wait_for_timeout(400)

    # New knowledge point from the detail editor.
    point_input = page.locator('#point-editor .vocab-picker-input')
    await point_input.fill('范式设计')
    await point_input.press('Enter')
    await expect(page.locator('#detail .detail-meta')).to_contain_text('范式设计')

    # The read-only deep-reading topic can be adopted through the same tag picker.
    await page.locator('#detail .adopt-topic').first.click()
    await expect(tag_input).to_have_value('外键')
    await expect(page.locator('#tag-editor .vocab-picker-menu')).to_contain_text('参照完整性')
    await page.locator('#tag-editor .vocab-picker-menu .vocab-option.vocab-default').click()
    await expect(page.locator('#tag-editor .vocab-chip:has-text("参照完整性")')).to_have_count(1)
    await screenshot('03-conversation-picker.png')

    assert errors == [], errors
    result = {'passed': True,
              'dataset': '5 controlled fixture conversations; not the bundled historical corpus',
              'javascript_errors': errors,
              'checks': ['existing vocabulary listed with usage',
                         'near-duplicate tag prompts and can be explicitly created',
                         'tag merge moves links and keeps alias',
                         'tag rename keeps alias',
                         'point near-duplicate prompt and merge',
                         'conversation picker selects existing term',
                         'conversation picker prompts on near-duplicate',
                         'detail creates a new point',
                         'deep-reading topic adopted through the tag picker'],
              'screenshots': sorted(path.name for path in output.glob('*.png'))}
    await page.close()
    return result


async def main():
    original = app.DB_PATH
    with tempfile.TemporaryDirectory(prefix='studyvault-vocabulary-') as temporary:
        try:
            root = Path(temporary)
            app.DB_PATH = root / 'test.db'
            app.restore_bundled(*write_bundle(root / 'fixtures', 5))
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(channel='msedge', headless=True)
                try:
                    with serve(app.DB_PATH) as url:
                        result = await check(browser, url)
                    (BASE / 'docs' / 'VOCABULARY_EVIDENCE.json').write_text(
                        json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                finally:
                    await browser.close()
        finally:
            app.DB_PATH = original


if __name__ == '__main__':
    asyncio.run(main())
