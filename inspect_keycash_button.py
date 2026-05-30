from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch_persistent_context(user_data_dir='./keycash_profile', headless=False, viewport={'width':1280,'height':900})
    page = browser.new_page()
    page.goto('https://keycash.pro/', timeout=60000)
    page.wait_for_timeout(8000)
    print('URL', page.url)
    elems = page.query_selector_all('button, a, [role="button"]')
    for el in elems:
        text = (el.inner_text() or '').strip()
        if not text:
            continue
        low = text.lower()
        if 'start earning' in low or 'game' in low or 'math' in low or 'play' in low:
            print('TAG', el.evaluate('el => el.tagName'), 'TEXT', repr(text), 'VISIBLE', el.is_visible())
            outer = el.evaluate('el => el.outerHTML')
            print(outer[:500])
            print('---')
    browser.close()
