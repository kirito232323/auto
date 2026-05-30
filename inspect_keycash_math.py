from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch_persistent_context(user_data_dir='./keycash_profile', headless=False, viewport={'width':1280,'height':900})
    page = browser.new_page()
    page.goto('https://keycash.pro/?start-game=math', timeout=60000)
    page.wait_for_timeout(8000)
    print('URL', page.url)
    print('TITLE', page.title())
    body = page.text_content('body') or ''
    print('BODY snippet', body[:1200])
    candidates = page.evaluate('''() => {
        const out=[];
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
            if (/[0-9]/.test(text) && /[×÷+\-*/]/.test(text)) out.push(text);
            if (out.length >= 30) break;
        }
        return out;
    }''')
    print('Question candidates:')
    for c in candidates:
        print(repr(c))
    page.screenshot(path='keycash_math_page.png', full_page=True)
    print('screenshot saved keycash_math_page.png')
    browser.close()
