from playwright.sync_api import sync_playwright
from pathlib import Path

output_dir = Path('inspect_output')
output_dir.mkdir(exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    page = browser.new_page()
    page.goto('https://keycash.pro/?c=math', timeout=60000)
    page.wait_for_timeout(10000)
    print('URL:', page.url)
    print('TITLE:', page.title())
    body = page.text_content('body') or ''
    print('BODY TEXT LENGTH:', len(body))
    print('BODY TEXT SNIPPET:')
    print(body[:1200])
    html = page.content()
    print('HTML LENGTH:', len(html))
    print('HTML SNIPPET:')
    print(html[:2000])
    texts = page.evaluate('''() => {
        const results = [];
        const elements = Array.from(document.querySelectorAll('body *'));
        const isVisible = el => {
            const style = window.getComputedStyle(el);
            if (!style || style.visibility === 'hidden' || style.display === 'none' || parseFloat(style.opacity || '1') === 0) return false;
            const rect = el.getBoundingClientRect();
            return rect.width > 20 && rect.height > 10;
        };
        for (const el of elements) {
            if (!isVisible(el)) continue;
            const text = (el.innerText || '').trim();
            if (!text || text.length > 120) continue;
            results.push(text);
            if (results.length >= 50) break;
        }
        return results;
    }''')
    print('VISIBLE TEXTS:')
    for t in texts:
        print(repr(t))
    screenshot_path = output_dir / 'keycash_live.png'
    page.screenshot(path=str(screenshot_path), full_page=True)
    print('Saved screenshot to', screenshot_path)
    browser.close()
