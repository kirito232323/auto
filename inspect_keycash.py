from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    page = browser.new_page()
    page.goto('https://keycash.pro/?c=math', timeout=60000)
    page.wait_for_timeout(8000)
    print('URL:', page.url)
    print('TITLE:', page.title())
    body_text = page.text_content('body') or ''
    print('BODY TEXT SNIPPET:')
    print(body_text[:1200])
    print('\n--- VISIBLE ELEMENT TEXTS ---')
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
    for t in texts:
        print(repr(t))
    browser.close()
