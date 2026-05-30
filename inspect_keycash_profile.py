from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch_persistent_context(user_data_dir='./keycash_profile', headless=False, viewport={'width':1280,'height':900})
    page = browser.new_page()
    page.goto('https://keycash.pro/?c=math', timeout=60000)
    page.wait_for_timeout(10000)
    print('URL:', page.url)
    print('TITLE:', page.title())
    body = page.text_content('body') or ''
    print('BODY len', len(body))
    print('BODY snippet', body[:1200])
    visible = page.evaluate('''() => {
        const out=[];
        const els=Array.from(document.querySelectorAll('button, a, div, span, [role=button]'));
        const isVisible=el=>{const s=getComputedStyle(el);if(!s||s.display==='none'||s.visibility==='hidden'||parseFloat(s.opacity||'1')===0)return false;const r=el.getBoundingClientRect();return r.width>20&&r.height>10;};
        for(const el of els){if(!isVisible(el))continue;const t=(el.innerText||'').trim();if(t.length===0||t.length>120) continue;out.push({tag:el.tagName,text:t});if(out.length>=100) break;}
        return out;
    }''')
    for item in visible[:60]:
        print(repr(item['text']))
    page.screenshot(path='logged_in_keycash.png', full_page=True)
    print('screenshot saved logged_in_keycash.png')
    browser.close()
