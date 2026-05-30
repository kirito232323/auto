from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch_persistent_context(user_data_dir='./keycash_profile', headless=True, viewport={'width':1280,'height':900})
    page = browser.new_page()
    page.goto('https://keycash.pro/?c=games', timeout=60000)
    page.wait_for_timeout(8000)
    print('URL', page.url)
    link = page.query_selector('a[href*=\"start-game=math\"]')
    print('exists', link is not None)
    print('visible', link.is_visible() if link else None)
    print('text', link.inner_text().strip() if link else None)
    if link:
        print('outer', link.evaluate('el => el.outerHTML'))
    browser.close()
