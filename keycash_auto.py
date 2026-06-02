import argparse
import base64
import json
import os
import re
import sys
import time
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from playwright.sync_api import BrowserContext, Page, sync_playwright

# Force UTF-8 output so emoji in page text never crashes on Windows cp1252 terminals.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


class StopRequested(Exception):
    """Raised when the user requests automation to stop."""


def should_stop(stop_event: Optional[threading.Event]) -> bool:
    return stop_event is not None and stop_event.is_set()


def check_stop(stop_event: Optional[threading.Event]) -> None:
    if should_stop(stop_event):
        raise StopRequested()


def sleep_or_stop(seconds: float, stop_event: Optional[threading.Event]) -> bool:
    """Sleep up to `seconds`. Returns True if stop was requested."""
    if seconds <= 0:
        check_stop(stop_event)
        return should_stop(stop_event)

    end = time.time() + seconds
    while time.time() < end:
        if should_stop(stop_event):
            return True
        time.sleep(min(0.2, end - time.time()))
    return False


def is_page_closed_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "target page, context or browser has been closed" in message
        or "target closed" in message
        or "browser has been closed" in message
    )


def is_navigation_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "execution context was destroyed" in message
        or "frame was detached" in message
        or "err_aborted" in message
    )


def page_is_usable(page: Page) -> bool:
    try:
        return not page.is_closed()
    except Exception:
        return False


def safe_page_wait(page: Page, ms: int) -> bool:
    """Wait on the page. Returns False if the browser/tab was closed."""
    try:
        page.wait_for_timeout(ms)
        return True
    except Exception as exc:
        if is_page_closed_error(exc):
            return False
        if is_navigation_error(exc):
            return True
        raise

EXPR_PATTERN = re.compile(
    r"\(?\s*[-+\u2212\u2013\u2014]?\d+(?:\.\d+)?(?:\s*[-+*/^×xX÷\u2212\u2013\u2014/]\s*\(?\s*[-+\u2212\u2013\u2014]?\d+(?:\.\d+)?\s*\)?)+\s*\)?"
)

ALLOWED_NAMES = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "pow": pow,
}


def normalize_expression(text: str) -> str:
    text = text.replace("×", "*")
    text = text.replace("÷", "/")
    text = text.replace("X", "*")
    text = text.replace("x", "*")
    text = text.replace("–", "-")
    text = text.replace("—", "-")
    text = text.replace("=", " ")
    text = text.replace("?", " ")
    return text.strip()


def safe_eval(expr: str):
    expr = normalize_expression(expr)
    expr = expr.replace("^", "**")
    code = compile(expr, "<math>", "eval")
    for name in code.co_names:
        if name not in ALLOWED_NAMES:
            raise ValueError(f"Unsafe expression element: {name}")
    return eval(code, {"__builtins__": {}}, ALLOWED_NAMES)


def extract_expressions(text: str) -> List[str]:
    text = normalize_expression(text)
    candidates = []
    for match in EXPR_PATTERN.finditer(text):
        raw = match.group(0).strip()
        if any(ch.isalpha() for ch in raw if ch not in "xX"):
            continue
        candidates.append(raw)
    return candidates


def choose_expression(text: str) -> Optional[str]:
    candidates = extract_expressions(text)
    for expr in candidates:
        try:
            safe_eval(expr)
            return expr
        except Exception:
            continue
    return None


def choose_best_expression(text: str) -> Optional[str]:
    """Pick the most likely math challenge expression from noisy text."""
    candidates = extract_expressions(text)
    if not candidates:
        return None

    scored = []
    for expr in candidates:
        try:
            safe_eval(expr)
        except Exception:
            continue
        score = len(expr)
        if re.search(r"[+\-*×÷]", expr.replace("/", "")):
            score += 20
        if re.search(r"\d+\s*[-+×÷*/]\s*\d+", expr):
            score += 15
        if re.fullmatch(r"\s*\d+\s*/\s*\d+\s*", expr):
            score -= 30
        nums = [int(n) for n in re.findall(r"\d+", expr)]
        if nums and max(nums) <= 3 and "/" in expr:
            score -= 20
        scored.append((score, expr))

    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]
def visible_math_texts(page: Page) -> List[str]:
    texts = page.evaluate(
        """
        () => {
            const results = [];
            const elements = Array.from(document.querySelectorAll('body *'));
            const isVisible = el => {
                const style = window.getComputedStyle(el);
                if (!style || style.visibility === 'hidden' || style.display === 'none' || parseFloat(style.opacity || '1') === 0) {
                    return false;
                }
                const rect = el.getBoundingClientRect();
                return rect.width > 20 && rect.height > 10;
            };
            for (const el of elements) {
                if (!isVisible(el)) continue;
                const text = (el.innerText || '').trim();
                if (!text || text.length > 120) continue;
                if (!/[0-9]/.test(text)) continue;
                // include common operator characters and unicode minus/dashes
                if (!/[×÷+*/\u2212\u2013\u2014xX/^=-]/.test(text)) continue;
                if (/login|sign in|password/i.test(text)) continue;
                results.push(text);
                if (results.length >= 50) break;
            }
            return results;
        }
        """
    )
    return texts if texts else []


def visible_texts(page: Page) -> List[str]:
    texts = page.evaluate(
        """
        () => {
            const results = [];
            const elements = Array.from(document.querySelectorAll('body *'));
            const isVisible = el => {
                const style = window.getComputedStyle(el);
                if (!style || style.visibility === 'hidden' || style.display === 'none' || parseFloat(style.opacity || '1') === 0) {
                    return false;
                }
                const rect = el.getBoundingClientRect();
                return rect.width > 20 && rect.height > 10;
            };
            for (const el of elements) {
                if (!isVisible(el)) continue;
                const text = (el.innerText || '').trim();
                if (!text || text.length > 120) continue;
                if (/login|sign in|password/i.test(text)) continue;
                // include more operator symbols so math-like lines are surfaced
                if (/[0-9]/.test(text) && /[×÷+*/\u2212\u2013\u2014xX/^=-]/.test(text)) {
                    results.push(text);
                } else {
                    results.push(text);
                }
                if (results.length >= 50) break;
            }
            return results;
        }
        """
    )
    return texts if texts else []


def visible_texts(page: Page) -> List[str]:
    texts = page.evaluate(
        """
        () => {
            const results = [];
            const elements = Array.from(document.querySelectorAll('body *'));
            const isVisible = el => {
                const style = window.getComputedStyle(el);
                if (!style || style.visibility === 'hidden' || style.display === 'none' || parseFloat(style.opacity || '1') === 0) {
                    return false;
                }
                const rect = el.getBoundingClientRect();
                return rect.width > 20 && rect.height > 10;
            };
            for (const el of elements) {
                if (!isVisible(el)) continue;
                const text = (el.innerText || '').trim();
                if (!text || text.length > 120) continue;
                if (/login|sign in|password/i.test(text)) continue;
                // include more operator symbols so math-like lines are surfaced
                if (/[0-9]/.test(text) && /[×÷+*/\u2212\u2013\u2014xX/^=-]/.test(text)) {
                    results.push(text);
                } else {
                    results.push(text);
                }
                if (results.length >= 50) break;
            }
            return results;
        }
        """
    )
    return texts if texts else []


def find_question_text(page: Page, selector: Optional[str]) -> str:
    if selector:
        element = page.query_selector(selector)
        if element:
            text = element.text_content()
            if text:
                return text
        raise ValueError(f"No question text found using selector: {selector}")

    candidates = visible_math_texts(page)
    if candidates:
        candidates = sorted(candidates, key=lambda t: (("?" in t or "=" in t), len(t)), reverse=True)
        for text in candidates:
            if extract_expressions(text):
                return text
        return candidates[0]

    body_text = page.text_content("body") or ""
    return body_text


def extract_numeric_value(text: str) -> Optional[float]:
    cleaned = text.strip().replace(",", "")
    cleaned = re.sub(r"[^0-9.\-]", "", cleaned)
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def click_visible_button(page: Page, texts: list[str]) -> bool:
    for text in texts:
        normalized = text.lower().strip()
        button = page.query_selector(f"button:has-text(\"{text}\")")
        if button and button.is_visible():
            button.click()
            return True
        link = page.query_selector(f"a:has-text(\"{text}\")")
        if link and link.is_visible():
            link.click()
            return True
    for element in page.query_selector_all("button, a, [role='button']"):
        if not element.is_visible():
            continue
        label = (element.inner_text() or "").strip().lower()
        if label in [t.lower() for t in texts]:
            element.click()
            return True
    return False


def dismiss_modal(page: Page) -> bool:
    return click_visible_button(page, ["OK", "Okay", "Close", "Got it", "Continue", "Dismiss"])


def navigate_to_game_center(page: Page) -> bool:
    if click_visible_button(page, ["Game Center", "Games", "Play", "Start Game", "Math"]):
        page.wait_for_load_state("networkidle", timeout=15000)
        return True
    return False


def start_math_game(page: Page) -> bool:
    print('Attempting to start math game...')
    math_link = page.query_selector('a[href*="start-game=math"]')
    if math_link and math_link.is_visible():
        print('Found math link; clicking it.')
        math_link.click()
        try:
            page.wait_for_url("**/?c=math", timeout=15000)
        except Exception:
            pass
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        return True

    if click_visible_button(page, ["Play Now", "Start Earning Now", "Math"]):
        print('Clicked a generic play/start button.')
        try:
            page.wait_for_url("**/?c=math", timeout=15000)
        except Exception:
            pass
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        return True

    print('No math start button found.')
    return False


def get_element_text(element):
    text = element.inner_text() or ""
    text = text.strip()
    if not text:
        text = (element.text_content() or "").strip()
    return text


HUMAN_CHECK_CODE_BLOCKLIST = frozenset(
    {
        "STEP",
        "QUICK",
        "HUMAN",
        "CHECK",
        "TEXT",
        "TYPE",
        "EXACT",
        "VERIFY",
        "NEXT",
        "CANCEL",
        "COMPLETE",
        "PREVENT",
        "AUTOMATED",
        "BOTS",
        "STEPS",
        "CONTINUE",
    }
)


def pick_verification_code(text: str) -> Optional[str]:
    """Pick the displayed human-check code, not UI words like STEP."""
    tokens = re.findall(r"\b[A-Z0-9]{4,12}\b", text.upper())
    tokens = [token for token in tokens if token not in HUMAN_CHECK_CODE_BLOCKLIST]
    if not tokens:
        return None

    for token in reversed(tokens):
        if len(token) >= 5 and re.search(r"[A-Z]", token) and re.search(r"[0-9]", token):
            return token

    for token in reversed(tokens):
        if len(token) >= 5:
            return token

    return tokens[-1]


def format_numeric_answer(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return str(value)


def scrub_step_indicator(text: str) -> str:
    return re.sub(r"STEP\s+\d+\s*/\s*\d+", "", text, flags=re.I)


def extract_human_check_challenge(popup) -> dict:
    """Read the active human-check step from the modal only (not the game behind it)."""
    if popup is None:
        return {}
    try:
        return popup.evaluate(
            """(popup) => {
                const blocked = new Set([
                    'STEP', 'QUICK', 'HUMAN', 'CHECK', 'TEXT', 'TYPE', 'EXACT', 'VERIFY',
                    'NEXT', 'CANCEL', 'COMPLETE', 'PREVENT', 'AUTOMATED', 'BOTS', 'STEPS', 'CONTINUE'
                ]);
                const lines = (popup.innerText || '')
                    .split('\\n')
                    .map(line => line.trim())
                    .filter(Boolean);
                const lowered = lines.join('\\n').toLowerCase();

                let stepType = null;
                if (/type (this|the) text exactly/i.test(lowered)) {
                    stepType = 'code';
                } else if (/solve this equation/i.test(lowered)) {
                    stepType = 'equation';
                } else if (/\\bhow many\\b/i.test(lowered)) {
                    stepType = 'count';
                }

                const leafCodeNodes = Array.from(popup.querySelectorAll('*')).filter(el => {
                    if (el.children.length > 0) return false;
                    const text = (el.textContent || '').trim().toUpperCase();
                    if (!/^[A-Z0-9]{5,8}$/.test(text)) return false;
                    if (blocked.has(text)) return false;
                    return /[A-Z]/.test(text) && /[0-9]/.test(text);
                });
                const code = leafCodeNodes.length
                    ? leafCodeNodes[leafCodeNodes.length - 1].textContent.trim().toUpperCase()
                    : null;

                let equationLine = lines.find(line => /=\\s*\\?/.test(line));
                if (!equationLine) {
                    equationLine = lines.find(line => /\\d+\\s*[-+×÷*/^]\\s*\\d+/.test(line));
                }

                let countLine = lines.find(line => /\\bhow many\\b/i.test(line));

                return { stepType, code, equationLine, countLine, lines };
            }"""
        ) or {}
    except Exception:
        return {}


def derive_human_check_answer(popup_text: str, page: Page, popup=None) -> Optional[str]:
    """Derive the answer for any human-check step using modal content only."""
    challenge = extract_human_check_challenge(popup) if popup is not None else {}
    step_type = challenge.get("stepType")
    text = (popup_text or "").strip()
    lowered = text.lower()

    if step_type == "code" or re.search(r"type (this|the) text exactly", lowered):
        code = challenge.get("code")
        if code:
            picked = pick_verification_code(code)
            return picked or code
        if popup is not None:
            code = find_human_check_code_in_popup(popup)
            if code:
                return code

    if step_type == "count" or re.search(r"\bhow many\b", lowered):
        count_line = challenge.get("countLine")
        if count_line:
            count_answer = count_repeated_items(count_line)
            if count_answer is not None:
                return format_numeric_answer(count_answer)
        for line in challenge.get("lines", []):
            count_answer = count_repeated_items(line)
            if count_answer is not None:
                return format_numeric_answer(count_answer)

    equation_line = challenge.get("equationLine")
    if equation_line:
        expr = choose_best_expression(equation_line)
        if expr:
            try:
                return format_numeric_answer(safe_eval(expr))
            except Exception:
                pass

    if step_type == "equation" or re.search(r"solve this equation", lowered):
        scrubbed = scrub_step_indicator(text)
        for line in scrubbed.splitlines():
            line = line.strip()
            if not line or re.match(r"^STEP\s+\d+", line, re.I):
                continue
            if re.search(r"\bhow many\b", line, re.I):
                continue
            expr = choose_best_expression(line)
            if expr:
                try:
                    return format_numeric_answer(safe_eval(expr))
                except Exception:
                    continue

        expr = choose_best_expression(scrubbed)
        if expr:
            try:
                return format_numeric_answer(safe_eval(expr))
            except Exception:
                pass

    return None


SET_INPUT_VALUE_JS = """
(el, value) => {
    const tag = el.tagName.toLowerCase();
    if (tag === 'input' || tag === 'textarea') {
        const proto = tag === 'textarea'
            ? window.HTMLTextAreaElement.prototype
            : window.HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
        if (setter) {
            setter.call(el, value);
        } else {
            el.value = value;
        }
    } else if (el.isContentEditable) {
        el.textContent = value;
    } else if ('value' in el) {
        el.value = value;
    } else {
        el.textContent = value;
    }
    el.focus();
    el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: value }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
}
"""


def find_text_input_candidates(page: Page):
    selectors = [
        "[contenteditable='true']",
        "[contenteditable]",
        "[role='textbox']",
        "div[role='textbox']",
        "span[role='textbox']",
        "input[type='text']",
        "input[type='search']",
        "input[type='tel']",
        "input[type='number']",
        "input[placeholder]",
        "input:not([type])",
        "textarea",
        "[aria-label]",
        "[data-placeholder]",
        "[tabindex]",
    ]
    candidates = []
    for selector in selectors:
        for element in page.query_selector_all(selector):
            if element and element.is_visible():
                candidates.append(element)

    for element in page.query_selector_all("[contenteditable], input, textarea, [role='textbox'], [placeholder], [aria-label], [data-placeholder], [tabindex]"):
        if not element.is_visible():
            continue
        tag = element.evaluate("el => el.tagName.toLowerCase()")
        if tag == 'input':
            input_type = (element.get_attribute("type") or "text").lower()
            if input_type in ("hidden", "submit", "button", "checkbox", "radio", "file", "password"):
                continue
        if element.get_attribute("contenteditable") is not None or element.get_attribute("role") == "textbox" or tag in ('input', 'textarea') or element.get_attribute('placeholder') or element.get_attribute('aria-label'):
            if element not in candidates:
                candidates.append(element)
    return candidates


def find_text_input(page: Page):
    candidates = find_text_input_candidates(page)
    return candidates[0] if candidates else None


def show_answer_overlay(page: Page, answer_text: str) -> None:
    page.evaluate(
        '''answer => {
            let overlay = document.getElementById('keycash_answer_overlay');
            if (!overlay) {
                overlay = document.createElement('div');
                overlay.id = 'keycash_answer_overlay';
                Object.assign(overlay.style, {
                    position: 'fixed',
                    bottom: '16px',
                    right: '16px',
                    zIndex: '999999',
                    background: 'rgba(0, 0, 0, 0.85)',
                    color: 'white',
                    padding: '12px 16px',
                    borderRadius: '10px',
                    fontSize: '14px',
                    fontFamily: 'Arial, sans-serif',
                    maxWidth: '260px',
                    wordBreak: 'break-word',
                    boxShadow: '0 2px 12px rgba(0, 0, 0, 0.4)',
                });
                document.body.appendChild(overlay);
            }
            overlay.textContent = 'Answer: ' + answer;
        }''',
        answer_text,
    )


def fill_text_input_direct(page: Page, answer_text: str, root_selector: Optional[str] = None) -> bool:
    result = page.evaluate(
        '''({ answer, rootSelector }) => {
            const selectors = [
                'input[placeholder*="TYPE"]',
                'input[placeholder*="EXACT"]',
                'input[placeholder*="verify" i]',
                'input[placeholder*="Enter"]',
                'input[placeholder*="answer"]',
                'input[aria-label*="TYPE"]',
                'input[aria-label*="EXACT"]',
                'input[aria-label*="verify" i]',
                'input[aria-label*="Enter"]',
                'input[aria-label*="answer"]',
                'textarea[placeholder*="TYPE"]',
                'textarea[placeholder*="EXACT"]',
                'textarea[placeholder*="Enter"]',
                'textarea[placeholder*="answer"]',
                'input[type="text"]',
                'input:not([type])',
                'textarea',
                'div[role="textbox"]',
                'span[role="textbox"]',
                '[contenteditable="true"]',
                '[contenteditable]'
            ];
            const isVisible = el => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                if (!style || style.visibility === 'hidden' || style.display === 'none' || parseFloat(style.opacity || '1') === 0) return false;
                const rect = el.getBoundingClientRect();
                return rect.width > 10 && rect.height > 10;
            };
            const setValue = (el, value) => {
                const tag = el.tagName.toLowerCase();
                if (tag === 'input' || tag === 'textarea') {
                    const proto = tag === 'textarea'
                        ? window.HTMLTextAreaElement.prototype
                        : window.HTMLInputElement.prototype;
                    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                    if (setter) {
                        setter.call(el, value);
                    } else {
                        el.value = value;
                    }
                } else if (el.isContentEditable) {
                    el.textContent = value;
                } else if ('value' in el) {
                    el.value = value;
                } else {
                    el.textContent = value;
                }
            };
            const tryFill = el => {
                if (!el || el.disabled || !isVisible(el)) return false;
                try {
                    el.removeAttribute('readonly');
                    el.readOnly = false;
                    el.style.pointerEvents = 'auto';
                    el.focus();
                    setValue(el, answer);
                    el.dispatchEvent(new Event('focus', { bubbles: true }));
                    el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: answer }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    const current = 'value' in el ? el.value : el.innerText || el.textContent || '';
                    return current.trim().toUpperCase() === String(answer).trim().toUpperCase();
                } catch (e) {
                    return false;
                }
            };
            const roots = rootSelector
                ? Array.from(document.querySelectorAll(rootSelector))
                : [document];
            for (const root of roots) {
                for (const sel of selectors) {
                    const elements = Array.from(root.querySelectorAll(sel));
                    for (const el of elements) {
                        if (tryFill(el)) return true;
                    }
                }
            }
            const active = document.activeElement;
            if (active && active !== document.body) {
                return tryFill(active);
            }
            return false;
        }''',
        {"answer": answer_text, "rootSelector": root_selector},
    )
    if result:
        print("Filled answer using direct DOM fallback")
    return bool(result)


def read_input_value(input_el) -> str:
    return (
        input_el.evaluate(
            "el => ('value' in el && el.value !== undefined ? el.value : el.innerText || el.textContent || '')"
        )
        or ""
    ).strip()


def try_type_into_element(
    page: Page,
    input_el,
    answer_text: str,
    press_enter: bool = True,
    fast: bool = False,
) -> bool:
    try:
        try:
            input_el.scroll_into_view_if_needed()
        except Exception:
            pass

        try:
            input_el.evaluate(
                "el => { el.removeAttribute('readonly'); el.readOnly = false; el.style.pointerEvents = 'auto'; }"
            )
        except Exception:
            pass

        try:
            input_el.click(force=True)
        except Exception:
            pass

        try:
            input_el.evaluate("el => el.focus()")
        except Exception:
            pass

        page.wait_for_timeout(60 if fast else 300)

        try:
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            page.wait_for_timeout(30 if fast else 120)
        except Exception:
            pass

        if not fast:
            show_answer_overlay(page, answer_text)
        try:
            tag_name = input_el.evaluate("el => el.tagName.toLowerCase()")
            input_type = input_el.get_attribute("type") or ""
            print(f"Typing into element: tag={tag_name}, type={input_type}")
            input_el.evaluate(SET_INPUT_VALUE_JS, answer_text)

            page.wait_for_timeout(50 if fast else 200)
            current_value = read_input_value(input_el)
            if current_value.upper() != answer_text.strip().upper():
                try:
                    # Faster fallback typing for the typing game.
                    page.keyboard.type(answer_text, delay=10 if fast else 80)
                except Exception:
                    if fast:
                        # Last resort: insert all at once.
                        page.keyboard.insert_text(answer_text)
                    else:
                        for char in answer_text:
                            page.keyboard.insert_text(char)
                            page.wait_for_timeout(60)

            input_el.evaluate(SET_INPUT_VALUE_JS, answer_text)
        except Exception as exc:
            print("Typing helper fill error:", exc)
            try:
                input_el.evaluate(SET_INPUT_VALUE_JS, answer_text)
            except Exception as exc2:
                print("Typing helper fallback JS error:", exc2)

        page.wait_for_timeout(40 if fast else 200)
        current_value = read_input_value(input_el)
        if current_value.upper() != answer_text.strip().upper():
            print(f"Typing verification failed. Expected {answer_text!r}, got {current_value!r}")
            return False

        if press_enter:
            try:
                input_el.press("Enter")
            except Exception:
                page.keyboard.press("Enter")
            page.wait_for_timeout(60 if fast else 200)

        print("Typed:", answer_text)
        return True

    except Exception as e:
        print("TYPE ERROR:", e)
        return False


def type_answer_in_input(page: Page, answer_text: str) -> bool:
    input_el = find_text_input(page)
    if input_el and input_el.is_visible():
        print("Typing into best visible text input candidate")
        if try_type_into_element(page, input_el, answer_text):
            return True

    selectors = [
        'input[placeholder*="Enter"]',
        'input[placeholder]',
        'input[type="text"]',
        'input[type="search"]',
        'input[type="tel"]',
        'input[type="number"]',
        'input',
        'textarea',
        '[contenteditable="true"]',
        '[role="textbox"]',
        'div[role="textbox"]',
        'span[role="textbox"]',
        '[tabindex]'
    ]

    for selector in selectors:
        elements = page.query_selector_all(selector)

        for el in elements:
            try:
                if not el.is_visible():
                    continue
                print(f"Trying generic selector candidate: {selector} -> {el.evaluate('el => el.outerHTML').strip()[:200]}")
                if try_type_into_element(page, el, answer_text):
                    print(f"Typed answer: {answer_text}")
                    return True
            except Exception as e:
                print("Typing failed:", e)

    return False


def type_answer_in_input_fast(page: Page, answer_text: str) -> bool:
    """Fast path for typing-game inputs (less waiting, JS set first)."""
    try:
        # Try direct JS fill first (fastest).
        if fill_text_input_direct(page, answer_text):
            # Enter might still be needed depending on the game.
            input_el = find_text_input(page)
            if input_el:
                try:
                    input_el.press("Enter")
                except Exception:
                    page.keyboard.press("Enter")
            return True
    except Exception:
        pass

    input_el = find_text_input(page)
    if input_el and input_el.is_visible():
        return try_type_into_element(page, input_el, answer_text, fast=True)

    return type_answer_in_input(page, answer_text)


def find_human_check_popup(page: Page):
    """Return the smallest visible modal for Quick Human Check (not a page wrapper)."""
    try:
        handle = page.evaluate_handle(
            """() => {
                const isVisible = el => {
                    const style = getComputedStyle(el);
                    if (!style || style.display === 'none' || style.visibility === 'hidden') return false;
                    const rect = el.getBoundingClientRect();
                    return rect.width > 80 && rect.height > 80;
                };
                const matches = [];
                for (const el of document.querySelectorAll('dialog, [role="dialog"], div, section, article')) {
                    const text = (el.innerText || '').trim();
                    if (!text || !/quick human check/i.test(text)) continue;
                    if (!isVisible(el)) continue;
                    matches.push(el);
                }
                if (!matches.length) return null;
                const dialog = matches.find(
                    el => el.tagName === 'DIALOG' || el.getAttribute('role') === 'dialog'
                );
                if (dialog) return dialog;
                matches.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
                return matches[0];
            }"""
        )
        element = handle.as_element() if handle else None
        if element and element.is_visible():
            return element
    except Exception:
        pass

    for selector in (
        '[role="dialog"]:has-text("Quick Human Check")',
        'dialog:has-text("Quick Human Check")',
    ):
        try:
            popup = page.query_selector(selector)
        except Exception as exc:
            if is_navigation_error(exc) or is_page_closed_error(exc):
                return None
            raise
        if popup and popup.is_visible():
            return popup

    return None


def find_human_check_code_in_popup(popup) -> Optional[str]:
    if popup is None:
        return None
    challenge = extract_human_check_challenge(popup)
    code = challenge.get("code")
    if code:
        picked = pick_verification_code(code)
        return picked or code
    return None


def find_human_check_code(page: Page) -> Optional[str]:
    code = page.evaluate(
        '''() => {
            const blocked = new Set([
                'STEP', 'QUICK', 'HUMAN', 'CHECK', 'TEXT', 'TYPE', 'EXACT', 'VERIFY',
                'NEXT', 'CANCEL', 'COMPLETE', 'PREVENT', 'AUTOMATED', 'BOTS', 'STEPS', 'CONTINUE'
            ]);
            const pickCode = text => {
                const tokens = (text || '').toUpperCase().match(/\\b[A-Z0-9]{4,12}\\b/g) || [];
                const filtered = tokens.filter(token => !blocked.has(token));
                for (let i = filtered.length - 1; i >= 0; i -= 1) {
                    const token = filtered[i];
                    if (token.length >= 5 && /[A-Z]/.test(token) && /[0-9]/.test(token)) {
                        return token;
                    }
                }
                for (let i = filtered.length - 1; i >= 0; i -= 1) {
                    if (filtered[i].length >= 5) {
                        return filtered[i];
                    }
                }
                return filtered.length ? filtered[filtered.length - 1] : null;
            };

            const popups = Array.from(document.querySelectorAll('div, section, article, dialog, [role="dialog"]'));
            const popup = popups.find(el => /type (this|the) text exactly to verify/i.test(el.innerText || ''));
            if (!popup) return null;

            const codeNodes = Array.from(popup.querySelectorAll('*')).filter(el => {
                const text = (el.innerText || '').trim().toUpperCase();
                if (!/^[A-Z0-9]{5,8}$/.test(text)) return false;
                if (blocked.has(text)) return false;
                return /[A-Z]/.test(text) && /[0-9]/.test(text);
            });
            if (codeNodes.length) {
                return codeNodes[codeNodes.length - 1].innerText.trim().toUpperCase();
            }

            return pickCode(popup.innerText);
        }'''
    )
    if not code:
        return None
    picked = pick_verification_code(code)
    return picked.strip() if picked else None


def find_human_check_input(page: Page, popup) -> Optional[object]:
    placeholder_patterns = [
        re.compile(r"TYPE.*EXACT", re.I),
        re.compile(r"exact.*text", re.I),
        re.compile(r"verify", re.I),
    ]
    for pattern in placeholder_patterns:
        try:
            locator = page.get_by_placeholder(pattern)
            if locator.count() > 0 and locator.first.is_visible():
                return locator.first.element_handle()
        except Exception:
            pass

    if popup:
        selectors = [
            'input[placeholder*="TYPE" i]',
            'input[placeholder*="EXACT" i]',
            'input[placeholder*="verify" i]',
            'input[type="text"]',
            'input:not([type])',
            'textarea',
            "[contenteditable='true']",
            "[contenteditable]",
            "[role='textbox']",
        ]
        for selector in selectors:
            for candidate in popup.query_selector_all(selector):
                if candidate and candidate.is_visible():
                    return candidate

    try:
        dialog_inputs = page.locator('[role="dialog"] input, dialog input').all()
        for candidate in dialog_inputs:
            if candidate.is_visible():
                return candidate.element_handle()
    except Exception:
        pass

    return None


def fill_human_check_input(page: Page, popup, answer_text: str) -> bool:
    """Fill the human-check field using Playwright locators first."""
    locator_attempts = [
        page.get_by_placeholder(re.compile(r"TYPE.*EXACT|exact.*text", re.I)),
        page.get_by_placeholder(re.compile(r"verify", re.I)),
        page.locator('div:has-text("Quick Human Check") input'),
        page.locator('div:has-text("Type this text exactly") input'),
        page.locator('div:has-text("Solve this equation") input'),
        page.locator('[role="dialog"] input'),
    ]

    for locator in locator_attempts:
        try:
            count = locator.count()
        except Exception:
            continue
        if count == 0:
            continue
        for index in range(count):
            target = locator.nth(index)
            try:
                if not target.is_visible():
                    continue
                target.click(force=True, timeout=2000)
                target.fill("", timeout=2000)
                target.fill(answer_text, timeout=2000)
                try:
                    current = target.input_value(timeout=1000).strip()
                except Exception:
                    current = target.evaluate(
                        "el => ('value' in el ? el.value : el.innerText || el.textContent || '').trim()"
                    )
                if str(current).upper() == answer_text.strip().upper():
                    print(f"Filled human-check input via locator: {answer_text}")
                    return True
                target.press_sequentially(answer_text, delay=50)
                try:
                    current = target.input_value(timeout=1000).strip()
                except Exception:
                    current = target.evaluate(
                        "el => ('value' in el ? el.value : el.innerText || el.textContent || '').trim()"
                    )
                if str(current).upper() == answer_text.strip().upper():
                    print(f"Filled human-check input via sequential typing: {answer_text}")
                    return True
            except Exception as exc:
                print(f"Locator fill attempt failed: {exc}")

    preferred_input = find_human_check_input(page, popup)
    if preferred_input:
        if try_type_into_element(page, preferred_input, answer_text, press_enter=False):
            return True

    if popup:
        input_candidates = popup.query_selector_all(
            "[contenteditable], [role='textbox'], input[type='text'], input[type='search'], "
            "input[type='tel'], input[type='number'], input:not([type]), textarea"
        )
        for candidate in input_candidates:
            if candidate and candidate.is_visible():
                if try_type_into_element(page, candidate, answer_text, press_enter=False):
                    return True

    if fill_text_input_direct(page, answer_text):
        return True

    return type_answer_in_input(page, answer_text)


def get_human_check_step_number(popup_text: str) -> Optional[int]:
    match = re.search(r"STEP\s+(\d+)\s*/\s*\d+", popup_text, re.I)
    if not match:
        return None
    return int(match.group(1))


def handle_human_check_popup(page: Page) -> bool:
    handled_any = False
    last_step = None

    for step_attempt in range(3):
        popup = find_human_check_popup(page)
        if not popup:
            return handled_any

        popup_text = (popup.inner_text() or "").strip()
        if not popup_text:
            return handled_any

        step_number = get_human_check_step_number(popup_text)
        challenge = extract_human_check_challenge(popup)
        print(
            f"Detected human-check step {step_number or '?'} "
            f"(type={challenge.get('stepType')}, attempt {step_attempt + 1})"
        )
        print(f"Popup snippet: {popup_text[:200]}")

        if step_number is not None and step_number == last_step and step_attempt > 0:
            print("Human-check step did not advance; stopping to avoid wrong answers.")
            break
        last_step = step_number

        answer_text = derive_human_check_answer(popup_text, page, popup)
        if answer_text is None:
            print("Human-check popup detected but no answer could be derived.")
            print(f"Challenge debug: {challenge}")
            return handled_any

        print(f"Human-check answer: {answer_text}")
        show_answer_overlay(page, answer_text)

        typed = fill_human_check_input(page, popup, answer_text)
        if not typed:
            print("Human-check input field not found or could not type into it.")
            return handled_any

        print("Entered human-check answer into popup input.")
        handled_any = True

        next_button = popup.query_selector('button:has-text("Next"), [role="button"]:has-text("Next")')
        if not next_button:
            next_button = page.query_selector('button:has-text("Next"), [role="button"]:has-text("Next")')

        if next_button and next_button.is_visible():
            try:
                next_button.click(force=True)
                page.wait_for_timeout(1500)
                continue
            except Exception:
                pass

        try:
            page.keyboard.press("Enter")
            page.wait_for_timeout(1500)
        except Exception:
            pass

    return handled_any


def find_answer_option_by_text(page: Page, answer_text: str):
    normalized = normalize_answer_text(answer_text)

    def contains_answer_token(text: str, token: str) -> bool:
        return re.search(rf"\b{re.escape(token)}\b", text) is not None

    for element in page.query_selector_all("button, [role='button'], input[type='button'], a"):
        if not element.is_visible():
            continue
        text = get_element_text(element)
        if not text:
            continue
        if normalize_answer_text(text) == normalized:
            return element
        if contains_answer_token(text.lower(), normalized):
            return element
    return None


def prompt_manual_answer() -> Optional[str]:
    try:
        answer = input("Type the answer here (or press Enter to skip): ")
    except EOFError:
        return None
    answer = answer.strip()
    return answer if answer else None


def type_manual_answer(page: Page) -> bool:
    answer_text = prompt_manual_answer()
    if not answer_text:
        return False
    if type_answer_in_input(page, answer_text):
        print(f"Typed manual answer into input: {answer_text}")
        return True
    print("Could not type the manual answer into a visible input.")
    return False


def count_repeated_items(text: str) -> Optional[float]:
    parts = re.findall(r"\S+", text)
    if len(parts) >= 3 and len(set(parts)) == 1:
        return float(len(parts))

    symbols = [ch for ch in text if not ch.isalnum() and not ch.isspace()]
    if len(symbols) >= 3:
        counts = {}
        for ch in symbols:
            counts[ch] = counts.get(ch, 0) + 1
        best_char, best_count = max(counts.items(), key=lambda item: item[1])
        if best_count >= 3:
            return float(best_count)

    return None


def is_count_question(text: str) -> bool:
    normalized = text.lower()
    return bool(re.search(r"\bhow many\b", normalized)) or bool(re.search(r"\bcount\b", normalized))


def extract_count_answer(page: Page) -> Optional[float]:
    body_text = (page.text_content('body') or '')
    if not is_count_question(body_text):
        return None

    candidates = visible_texts(page)
    for candidate in candidates:
        count = count_repeated_items(candidate)
        if count is not None:
            return count

    count = count_repeated_items(body_text)
    if count is not None:
        return count

    return None


def find_answer_option(page: Page, answer: float):
    candidates = page.query_selector_all("button, [role='button'], input[type='button'], a")
    answer_text = str(int(answer)) if answer == int(answer) else str(answer)
    best_match = None

    def contains_answer_token(text: str, token: str) -> bool:
        return re.search(rf"\b{re.escape(token)}\b", text) is not None

    for element in candidates:
        if not element.is_visible():
            continue
        text = get_element_text(element)
        if not text:
            continue
        value = extract_numeric_value(text)
        if value is not None and abs(value - answer) < 1e-9:
            return element
        if answer_text == text:
            return element
        if contains_answer_token(text, answer_text) and best_match is None:
            best_match = element

    return best_match


def is_login_page(page: Page) -> bool:
    current_url = page.url.lower()
    if "login" in current_url or "signin" in current_url:
        return True
    if page.query_selector("input[type=password]") is not None:
        return True
    if page.query_selector("input[name=email], input[name=username], input[name=user]") is not None:
        return True
    return False


def debug_page_state(page: Page, label: str) -> None:
    try:
        url = page.url
    except Exception:
        url = '<unknown>'
    try:
        body = page.text_content('body') or ''
    except Exception:
        body = ''
    print(f"[DEBUG] {label} url={url}")
    print(f"[DEBUG] {label} body snippet={body[:300]!r}")
    try:
        texts = visible_math_texts(page)
        print(f"[DEBUG] {label} visible math-like texts ({len(texts)}):")
        for t in texts[:10]:
            print(f"  - {t}")
    except Exception as exc:
        print(f"[DEBUG] {label} visible_math_texts failed: {exc}")


def is_typing_game_url(url: str) -> bool:
    u = (url or "").lower()
    return "?c=typing" in u or "/?c=typing" in u


def is_typing_game(page: Page) -> bool:
    return is_typing_game_url(page.url)


def is_emoji_game_url(url: str) -> bool:
    u = (url or "").lower()
    return "?c=emoji" in u or "/?c=emoji" in u


def is_emoji_game(page: Page) -> bool:
    return is_emoji_game_url(page.url)


@dataclass
class EmojiAIConfig:
    enabled: bool = False
    api_key: str = ""
    model: str = "gpt-4o-mini"
    auth_failed: bool = False


def get_emoji_ai_api_key(explicit_key: str = "") -> Optional[str]:
    key = (explicit_key or os.environ.get("OPENAI_API_KEY") or "").strip()
    return key or None


def build_emoji_ai_config(args: argparse.Namespace) -> EmojiAIConfig:
    explicit = getattr(args, "emoji_ai", None)
    api_key = get_emoji_ai_api_key(getattr(args, "openai_api_key", "") or "")
    model = (getattr(args, "emoji_ai_model", None) or "gpt-4o-mini").strip()
    if explicit is False:
        return EmojiAIConfig(enabled=False, api_key=api_key or "", model=model)
    if explicit is True:
        return EmojiAIConfig(enabled=bool(api_key), api_key=api_key or "", model=model)
    auto = is_emoji_game_url(getattr(args, "url", "")) and bool(api_key)
    return EmojiAIConfig(enabled=auto, api_key=api_key or "", model=model)


def capture_emoji_game_screenshot(page: Page) -> bytes:
    """Screenshot the game card (center prompt + answer buttons)."""
    viewport = page.viewport_size or {"width": 1280, "height": 900}
    width = int(viewport["width"])
    height = int(viewport["height"])
    clip = {
        "x": max(0, int(width * 0.04)),
        "y": max(0, int(height * 0.16)),
        "width": min(width, int(width * 0.92)),
        "height": min(height - int(height * 0.16), int(height * 0.68)),
    }
    return page.screenshot(type="png", clip=clip)


def match_ai_pick_to_option(raw: str, options: List[str]) -> Optional[str]:
    if not raw or not options:
        return None
    cleaned = raw.strip().strip('"').strip("'").strip("`.")
    cleaned = re.sub(r"^(answer|choice|option)\s*[:.]?\s*", "", cleaned, flags=re.I)
    if not cleaned:
        return None

    norm = normalize_answer_text(cleaned)
    for opt in options:
        if normalize_answer_text(opt) == norm:
            return opt
    for opt in options:
        nopt = normalize_answer_text(opt)
        if norm in nopt or nopt in norm:
            return opt
    for opt in options:
        if cleaned.lower() in opt.lower() or opt.lower() in cleaned.lower():
            return opt
    return match_emoji_label_to_option(cleaned, options)


def call_openai_vision_emoji(
    image_png: bytes,
    options: List[str],
    api_key: str,
    model: str,
    config: Optional[EmojiAIConfig] = None,
) -> Optional[str]:
    """Ask a vision model which button label matches the center emoji/logo."""
    if not options or not api_key:
        return None

    b64 = base64.standard_b64encode(image_png).decode("ascii")
    options_text = "\n".join(f"- {opt}" for opt in options)
    prompt = (
        "You are playing Keycash 'Emoji ID'. A large emoji, icon, or logo is shown in the "
        "center of the screenshot. Four answer buttons are below it.\n\n"
        f"Pick exactly ONE label from this list that best matches the center image:\n"
        f"{options_text}\n\n"
        "Reply with ONLY the exact text of the correct choice, copied character-for-character. "
        "No explanation."
    )
    body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "low"},
                    },
                ],
            }
        ],
        "max_tokens": 48,
        "temperature": 0,
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        if exc.code in (401, 403) and config is not None and not config.auth_failed:
            config.auth_failed = True
            config.enabled = False
            print(
                "[EMOJI-AI] Invalid or unauthorized API key; vision disabled for this run. "
                "Using DOM matching only. Fix OPENAI_API_KEY and restart."
            )
        elif exc.code not in (401, 403):
            print(f"[EMOJI-AI] OpenAI HTTP {exc.code}: {detail}")
        return None
    except Exception as exc:
        print(f"[EMOJI-AI] request failed: {exc}")
        return None

    try:
        raw = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        print(f"[EMOJI-AI] unexpected response: {payload!r}")
        return None

    if not raw:
        return None
    print(f"[EMOJI-AI] model said: {raw.strip()!r}")
    return match_ai_pick_to_option(str(raw), options)


def resolve_emoji_answer_with_ai(page: Page, config: EmojiAIConfig) -> Optional[str]:
    if not config.enabled or not config.api_key or config.auth_failed:
        return None

    state = wait_for_emoji_prompt(page, timeout_ms=3500)
    options = filter_emoji_options(list(state.get("options") or []))
    if len(options) < 2:
        options = collect_emoji_options(page)
    if len(options) < 2:
        print("[EMOJI-AI] need at least 2 answer buttons on screen")
        return None

    page.wait_for_timeout(500)
    try:
        image = capture_emoji_game_screenshot(page)
    except Exception as exc:
        print(f"[EMOJI-AI] screenshot failed: {exc}")
        return None

    return call_openai_vision_emoji(image, options, config.api_key, config.model, config)


def is_math_game(page: Page) -> bool:
    url = page.url.lower()
    if "?c=math" in url or "/?c=math" in url:
        return True
    candidates = visible_math_texts(page)
    return len(candidates) > 0


def is_game_page(page: Page) -> bool:
    return is_math_game(page) or is_typing_game(page) or is_emoji_game(page)


def normalize_answer_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def filter_emoji_options(options: List[str]) -> List[str]:
    """Drop UI junk like currency symbols from answer lists."""
    cleaned = []
    for opt in options:
        text = opt.strip()
        if not text or len(text) > 45:
            continue
        if text in {"₱", "$", "€", "£"} or re.fullmatch(r"[^\w\s'-]+", text):
            continue
        if not re.search(r"[a-zA-Z]", text):
            continue
        cleaned.append(text)
    return cleaned


def collect_emoji_options(page: Page) -> List[str]:
    """Collect visible answer buttons in the Emoji ID game."""
    try:
        options = page.evaluate(
            """() => {
                const skip = /^(quit|time|correct|emoji id|keycash|pro)$/i;
                const results = [];
                const seen = new Set();
                for (const btn of document.querySelectorAll('button')) {
                    const style = window.getComputedStyle(btn);
                    if (!style || style.display === 'none' || style.visibility === 'hidden') continue;
                    const rect = btn.getBoundingClientRect();
                    if (rect.top < window.innerHeight * 0.28) continue;
                    if (rect.width < 40 || rect.height < 20) continue;
                    const text = (btn.innerText || '').replace(/\\s+/g, ' ').trim();
                    if (!text || text.length > 45 || skip.test(text) || /^\\d+$/.test(text)) continue;
                    if (!/[a-zA-Z]/.test(text)) continue;
                    const key = text.toLowerCase();
                    if (!seen.has(key)) { seen.add(key); results.push(text); }
                }
                return results;
            }"""
        )
        return filter_emoji_options(list(options) if options else [])
    except Exception:
        return []


def extract_center_emoji_char(page: Page) -> Optional[str]:
    """Read the large centered emoji character from the game card."""
    ch = gather_emoji_state(page).get("centerChar")
    return str(ch).strip() if ch else None


def wait_for_emoji_prompt(page: Page, timeout_ms: int = 2500) -> dict:
    """Wait until the center emoji or prompt image is visible."""
    deadline = time.time() + (timeout_ms / 1000.0)
    last: dict = {}
    while time.time() < deadline:
        last = gather_emoji_state(page)
        options = last.get("options") or []
        if last.get("centerChar") or last.get("imgSrc") or last.get("imgAlt"):
            return last
        if len(options) >= 2:
            page.wait_for_timeout(280)
            continue
        page.wait_for_timeout(280)
    return last


def emoji_char_to_label(ch: str) -> Optional[str]:
    """Convert an emoji character to a human label like 'ring buoy'."""
    if not ch:
        return None
    try:
        import emoji as emoji_lib

        dem = emoji_lib.demojize(ch)
        parts = re.findall(r":([a-z0-9_]+):", dem, re.I)
        if parts:
            return parts[0].replace("_", " ")
    except ImportError:
        print("[EMOJI] Install the 'emoji' package for best results: pip install emoji")
    except Exception as exc:
        print(f"[EMOJI] demojize failed: {exc}")
    return None


def match_emoji_label_to_option(label: str, options: List[str]) -> Optional[str]:
    if not label or not options:
        return None
    norm_label = normalize_answer_text(label)
    for opt in options:
        if normalize_answer_text(opt) == norm_label:
            return opt
    for opt in options:
        nopt = normalize_answer_text(opt)
        if norm_label in nopt or nopt in norm_label:
            return opt
    label_words = set(norm_label.split())
    best = None
    best_score = 0
    for opt in options:
        opt_words = set(normalize_answer_text(opt).split())
        score = len(label_words & opt_words)
        if score > best_score:
            best_score = score
            best = opt
    return best if best_score > 0 else None


def label_from_emoji_url(url: str) -> Optional[str]:
    if not url:
        return None
    match = re.search(r"([0-9a-f]{4,6}(?:-[0-9a-f]{4,6})?)", url, re.I)
    if match:
        try:
            parts = match.group(1).split("-")
            ch = "".join(chr(int(part, 16)) for part in parts)
            derived = emoji_char_to_label(ch)
            if derived:
                return derived
        except (ValueError, TypeError, OverflowError):
            pass
    cleaned = re.sub(r"\.[a-z0-9]+$", "", url.split("/")[-1], flags=re.I)
    cleaned = re.sub(r"[-_]+", " ", cleaned).strip()
    if cleaned and re.search(r"[a-z]", cleaned, re.I):
        return cleaned
    return None


def gather_emoji_state(page: Page) -> dict:
    """Collect answer buttons + center emoji (img, text glyph, or background)."""
    try:
        state = page.evaluate(
            """() => {
                const skip = /^(quit|time|correct|emoji id|keycash|pro)$/i;
                const normalize = s => (s || '').replace(/\\u00A0/g, ' ').replace(/\\s+/g, ' ').trim();
                const options = [];
                const seen = new Set();
                const centerX = window.innerWidth / 2;
                const emojiRe = /([\\uD83C-\\uDBFF][\\uDC00-\\uDFFF]|[\\u2600-\\u27BF])/g;

                const codeFromUrl = url => {
                    if (!url) return null;
                    const s = String(url);
                    const m = s.match(/([0-9a-f]{4,6}(?:-[0-9a-f]{4,6})?)/i);
                    if (!m) return null;
                    try {
                        const parts = m[1].split('-').map(p => parseInt(p, 16));
                        return String.fromCodePoint(...parts);
                    } catch (e) { return null; }
                };

                const inGameCard = (cy, cx) => {
                    return cy > window.innerHeight * 0.22
                        && cy < window.innerHeight * 0.72
                        && Math.abs(cx - centerX) < window.innerWidth * 0.45;
                };

                for (const btn of document.querySelectorAll('button')) {
                    const style = window.getComputedStyle(btn);
                    if (!style || style.display === 'none' || style.visibility === 'hidden') continue;
                    const rect = btn.getBoundingClientRect();
                    if (rect.top < window.innerHeight * 0.28) continue;
                    if (rect.width < 40 || rect.height < 20) continue;
                    const text = normalize(btn.innerText);
                    if (!text || text.length > 45 || skip.test(text) || /^\\d+$/.test(text)) continue;
                    if (!/[a-zA-Z]/.test(text)) continue;
                    const key = text.toLowerCase();
                    if (!seen.has(key)) { seen.add(key); options.push(text); }
                }

                let centerChar = null;
                let imgSrc = null;
                let imgAlt = null;
                let bestScore = -Infinity;

                const consider = (score, ch, src, alt) => {
                    if (score > bestScore) {
                        bestScore = score;
                        if (ch) centerChar = ch;
                        if (src) imgSrc = src;
                        if (alt) imgAlt = alt;
                    }
                };

                // 1) Center <img> (twemoji etc.)
                for (const img of document.querySelectorAll('img')) {
                    const style = window.getComputedStyle(img);
                    if (!style || style.display === 'none' || style.visibility === 'hidden') continue;
                    const rect = img.getBoundingClientRect();
                    const cx = rect.left + rect.width / 2;
                    const cy = rect.top + rect.height / 2;
                    if (!inGameCard(cy, cx)) continue;
                    if (rect.width < 20 || rect.height < 20) continue;
                    const src = img.currentSrc || img.src || '';
                    if (/logo|favicon|avatar|keycash/i.test(src)) continue;
                    const area = rect.width * rect.height;
                    const ch = codeFromUrl(src);
                    const alt = normalize(img.alt);
                    consider(area, ch, src, alt);
                }

                // 2) Large native emoji text in the card (not an image).
                for (const el of document.querySelectorAll('*')) {
                    if (el.children && el.children.length > 3) continue;
                    const style = window.getComputedStyle(el);
                    if (!style || style.display === 'none' || style.visibility === 'hidden') continue;
                    const rect = el.getBoundingClientRect();
                    if (rect.width < 16 || rect.height < 16) continue;
                    const cx = rect.left + rect.width / 2;
                    const cy = rect.top + rect.height / 2;
                    if (!inGameCard(cy, cx)) continue;
                    const text = normalize(el.innerText || el.textContent);
                    if (!text || text.length > 16) continue;
                    const matches = text.match(emojiRe);
                    if (!matches || !matches[0]) continue;
                    const rest = text.replace(emojiRe, '').trim();
                    if (rest.length > 1) continue;
                    const fontSize = parseFloat(style.fontSize || '0') || 0;
                    const score = fontSize * 500 + rect.width * rect.height;
                    consider(score, matches[0], null, null);
                }

                // 4) Custom logo / asset images (filename matches an option).
                if (!centerChar && !imgSrc) {
                    for (const img of document.querySelectorAll('img')) {
                        const style = window.getComputedStyle(img);
                        if (!style || style.display === 'none' || style.visibility === 'hidden') continue;
                        const rect = img.getBoundingClientRect();
                        const cx = rect.left + rect.width / 2;
                        const cy = rect.top + rect.height / 2;
                        if (!inGameCard(cy, cx)) continue;
                        const src = img.currentSrc || img.src || '';
                        if (!src || /logo|favicon|avatar|keycash/i.test(src)) continue;
                        const area = rect.width * rect.height;
                        const alt = normalize(img.alt);
                        consider(area, null, src, alt);
                    }
                }

                // 3) background-image emoji/sprites.
                for (const el of document.querySelectorAll('div, span')) {
                    const style = window.getComputedStyle(el);
                    if (!style || style.display === 'none') continue;
                    const rect = el.getBoundingClientRect();
                    const cx = rect.left + rect.width / 2;
                    const cy = rect.top + rect.height / 2;
                    if (!inGameCard(cy, cx)) continue;
                    const bg = style.backgroundImage || '';
                    if (!bg || bg === 'none' || !bg.includes('url')) continue;
                    const m = bg.match(/url\\(["']?([^"')]+)["']?\\)/i);
                    if (!m) continue;
                    const src = m[1];
                    const ch = codeFromUrl(src);
                    const area = rect.width * rect.height;
                    consider(area, ch, src, null);
                }

                return { options, centerChar, imgSrc, imgAlt };
            }"""
        )
        if isinstance(state, dict) and state.get("options"):
            state["options"] = filter_emoji_options(state["options"])
        return state if isinstance(state, dict) else {}
    except Exception as exc:
        print(f"[EMOJI] gather_emoji_state error: {exc}")
        return {}


def ensure_emoji_page(page: Page, url: str, timeout: int = 30) -> None:
    """Stay on the emoji game; re-open if the site navigates away."""
    if not page_is_usable(page):
        return
    if is_emoji_game(page):
        return
    print(f"[EMOJI] Left emoji page ({page.url}), navigating back...")
    try:
        navigate_to_game(page, url, timeout)
    except Exception as exc:
        if is_navigation_error(exc):
            safe_page_wait(page, 800)
            return
        raise


def resolve_emoji_answer_from_state(state: dict) -> Optional[str]:
    options = filter_emoji_options(list(state.get("options") or []))

    ch = state.get("centerChar")
    if ch:
        derived = emoji_char_to_label(str(ch))
        if derived:
            match = match_emoji_label_to_option(derived, options)
            if match:
                return match

    img_alt = state.get("imgAlt") or state.get("centerAlt")
    if img_alt:
        match = match_emoji_label_to_option(str(img_alt), options)
        if match:
            return match

    img_src = state.get("imgSrc") or state.get("centerSrc")
    if img_src:
        derived = label_from_emoji_url(str(img_src))
        if derived:
            match = match_emoji_label_to_option(derived, options)
            if match:
                return match

    return None


def resolve_emoji_answer_robust(
    page: Page,
    emoji_ai: Optional[EmojiAIConfig] = None,
) -> Optional[str]:
    """Try AI vision first (if enabled), then DOM/heuristic matching."""
    if emoji_ai and emoji_ai.enabled:
        answer = resolve_emoji_answer_with_ai(page, emoji_ai)
        if answer:
            return answer

    state = wait_for_emoji_prompt(page)
    answer = resolve_emoji_answer_from_state(state)
    if answer:
        return answer

    if emoji_ai and emoji_ai.enabled:
        return None

    options = collect_emoji_options(page)
    if options:
        ch = extract_center_emoji_char(page)
        if ch:
            derived = emoji_char_to_label(ch)
            if derived:
                match = match_emoji_label_to_option(derived, options)
                if match:
                    return match
        label = detect_emoji_prompt_label(page)
        if label:
            match = match_emoji_label_to_option(label, options)
            if match:
                return match
    return None


def resolve_emoji_answer(page: Page) -> Optional[str]:
    """Determine which Emoji ID option text to click."""
    return resolve_emoji_answer_from_state(gather_emoji_state(page))


def click_emoji_option(page: Page, answer: str) -> bool:
    """Click the matching Emoji ID answer button."""
    target = answer.strip()
    if not target:
        return False

    # Playwright locators are the most reliable for this UI.
    try:
        btn = page.get_by_role("button", name=target, exact=True)
        if btn.count() > 0:
            btn.first.click(timeout=3000, force=True)
            return True
    except Exception:
        pass

    try:
        btn = page.locator("button").filter(has_text=re.compile(f"^{re.escape(target)}$", re.I))
        if btn.count() > 0:
            btn.first.click(timeout=3000, force=True)
            return True
    except Exception:
        pass

    try:
        return bool(
            page.evaluate(
                """(answer) => {
                    const target = String(answer).toLowerCase().trim();
                    for (const el of document.querySelectorAll('button, [role="button"]')) {
                        const style = window.getComputedStyle(el);
                        if (!style || style.display === 'none' || style.visibility === 'hidden') continue;
                        const rect = el.getBoundingClientRect();
                        if (rect.top < window.innerHeight * 0.28) continue;
                        const text = (el.innerText || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                        if (text === target) {
                            el.click();
                            return true;
                        }
                    }
                    return false;
                }""",
                target,
            )
        )
    except Exception:
        return False


EMOJI_MIN_PAUSE_SEC = 0.5


def effective_pause(page: Page, pause: float) -> float:
    if is_emoji_game(page):
        return max(pause, EMOJI_MIN_PAUSE_SEC)
    if is_typing_game(page):
        return min(pause, 0.15)
    return pause


def play_emoji_once(
    page: Page,
    pause: float,
    game_url: str = "https://keycash.pro/?c=emoji",
    emoji_ai: Optional[EmojiAIConfig] = None,
    emoji_answer_delay: Optional[float] = None,
) -> bool:
    ensure_emoji_page(page, game_url)
    for attempt in range(3):
        if attempt:
            page.wait_for_timeout(700)
        else:
            page.wait_for_timeout(450)
        answer = resolve_emoji_answer_robust(page, emoji_ai)
        if not answer:
            state = gather_emoji_state(page)
            print(
                f"[EMOJI] no match (attempt {attempt + 1}): "
                f"options={state.get('options')!r} "
                f"center={state.get('centerChar')!r} "
                f"alt={state.get('imgAlt')!r} "
                f"src={str(state.get('imgSrc') or '')[:80]!r}"
            )
            continue
        if click_emoji_option(page, answer):
            print(f"[EMOJI] clicked: {answer!r}")
            # Use explicit emoji_answer_delay if provided, otherwise fall back to effective_pause.
            delay = emoji_answer_delay if emoji_answer_delay is not None else effective_pause(page, pause)
            if delay > 0:
                time.sleep(delay)
            return True
        print(f"[EMOJI] found answer {answer!r} but click failed (attempt {attempt + 1})")
    return False


def detect_emoji_prompt_label(page: Page) -> Optional[str]:
    """Try to read the emoji's label/name from alt/aria/title in the game card."""
    try:
        res = page.evaluate(
            """() => {
                const normalize = s => (s || '').replace(/\\u00A0/g, ' ').replace(/\\s+/g, ' ').trim();
                const isVisible = el => {
                    const style = window.getComputedStyle(el);
                    if (!style || style.visibility === 'hidden' || style.display === 'none' || parseFloat(style.opacity || '1') === 0) return false;
                    const rect = el.getBoundingClientRect();
                    return rect.width > 10 && rect.height > 10;
                };

                const labelFromUrl = url => {
                    const raw = normalize(url);
                    if (!raw) return null;
                    try {
                        const cleaned = raw.split('?')[0].split('#')[0];
                        const last = cleaned.split('/').filter(Boolean).pop() || '';
                        const base = last.replace(/\\.[a-z0-9]+$/i, '');
                        const decoded = decodeURIComponent(base);
                        const normalizedName = decoded
                            .replace(/[-_]+/g, ' ')
                            .replace(/\\s+/g, ' ')
                            .trim();
                        if (!normalizedName) return null;
                        if (normalizedName.length < 2 || normalizedName.length > 60) return null;
                        const filtered = normalizedName.replace(/[^a-z0-9\\s'\\-]/gi, '').trim();
                        return filtered || null;
                    } catch (e) {
                        return null;
                    }
                };

                // Prefer explicit a11y labels first.
                const candidates = [];
                const nodes = Array.from(
                    document.querySelectorAll(
                        '[aria-label], img[alt], svg title, svg[aria-label], [role=\"img\"], img, svg, [style*=\"background-image\"]'
                    )
                );
                for (const node of nodes) {
                    let el = node;
                    // svg title selector yields <title>, normalize to parent svg
                    if (el && el.tagName && el.tagName.toLowerCase() === 'title') {
                        el = el.parentElement;
                    }
                    if (!el || !isVisible(el)) continue;

                    const rect = el.getBoundingClientRect();
                    const cx = rect.left + rect.width / 2;
                    const cy = rect.top + rect.height / 2;

                    // Emoji is usually centered in the game card.
                    if (cy < window.innerHeight * 0.15 || cy > window.innerHeight * 0.75) continue;
                    if (cx < window.innerWidth * 0.2 || cx > window.innerWidth * 0.8) continue;

                    const aria = normalize(el.getAttribute('aria-label'));
                    const alt = normalize(el.getAttribute('alt'));
                    const titleAttr = normalize(el.getAttribute('title'));
                    let titleEl = '';
                    try {
                        const t = el.querySelector && el.querySelector('title');
                        titleEl = normalize(t ? t.textContent : '');
                    } catch (e) {}

                    let label = aria || alt || titleEl || titleAttr;

                    if (!label) {
                        const tag = (el.tagName || '').toLowerCase();
                        if (tag === 'img') {
                            label = labelFromUrl(el.getAttribute('src')) || labelFromUrl(el.currentSrc);
                        } else if (tag === 'svg') {
                            const use = el.querySelector && el.querySelector('use');
                            const href = use ? (use.getAttribute('href') || use.getAttribute('xlink:href')) : null;
                            label = labelFromUrl(href);
                        }
                    }

                    if (!label) {
                        try {
                            const style = window.getComputedStyle(el);
                            const bg = style && style.backgroundImage ? style.backgroundImage : '';
                            const match = bg && bg.match(/url\\([\"']?(.*?)[\"']?\\)/i);
                            label = match ? labelFromUrl(match[1]) : null;
                        } catch (e) {}
                    }
                    if (!label) continue;
                    // Labels like "carpentry saw" (option text)
                    if (label.length < 3 || label.length > 60) continue;
                    if (!/[a-z]/i.test(label)) continue;

                    // Score: center-ish + shorter labels win.
                    const centerDist = Math.abs(cx - window.innerWidth/2) + Math.abs(cy - window.innerHeight/2);
                    const score = -centerDist - label.length;
                    candidates.push({ label, score });
                }

                candidates.sort((a,b)=>b.score-a.score);
                return candidates.length ? candidates[0].label : null;
            }"""
        )
        if not res:
            return None
        label = str(res).strip()
        return label if label else None
    except Exception:
        return None


def detect_typing_prompt_text(page: Page) -> Optional[str]:
    """Detect the text prompt shown in the center area of the typing game."""
    try:
        res = page.evaluate(
            """() => {
                const centerX = window.innerWidth / 2;
                const centerY = window.innerHeight / 2;

                const normalize = s => (s || '').replace(/\\u00A0/g, ' ').replace(/\\s+/g, ' ').trim();
                const isVisible = el => {
                    const style = window.getComputedStyle(el);
                    if (!style) return false;
                    if (style.visibility === 'hidden') return false;
                    if (style.display === 'none') return false;
                    if (parseFloat(style.opacity || '1') === 0) return false;
                    const rect = el.getBoundingClientRect();
                    if (!rect) return false;
                    if (rect.width < 40 || rect.height < 20) return false;
                    if (rect.bottom < 0 || rect.top > window.innerHeight || rect.right < 0 || rect.left > window.innerWidth) return false;
                    return true;
                };

                const skip = [/type here/i, /enter/i, /score/i, /time/i, /quit/i, /decentralized/i];
                const hasInput = !!document.querySelector('input, textarea, [contenteditable="true"], [role="textbox"]');
                if (!hasInput) return { best: null, top: [] };

                const candidates = [];
                for (const el of Array.from(document.querySelectorAll('body *'))) {
                    if (!isVisible(el)) continue;
                    const tag = (el.tagName || '').toLowerCase();
                    if (tag === 'input' || tag === 'textarea' || tag === 'button' || tag === 'select') continue;
                    const style = window.getComputedStyle(el);
                    const fontSize = parseFloat(style.fontSize || '0') || 0;
                    if (fontSize < 18) continue; // typing prompt is the big word

                    const raw = normalize(el.innerText);
                    if (!raw) continue;
                    if (skip.some(re => re.test(raw))) continue;

                    // Typing prompt is usually one word (lowercase) like "inconvenience"
                    if (!/^[A-Za-z][A-Za-z\\-']{2,30}$/.test(raw)) continue;

                    const rect = el.getBoundingClientRect();
                    const cx = rect.left + rect.width / 2;
                    const cy = rect.top + rect.height / 2;

                    const dx = Math.abs(cx - centerX);
                    const dy = Math.abs(cy - centerY);

                    // Exclude the input area (usually lower on the screen).
                    if (cy > window.innerHeight * 0.62) continue;

                    if (dx > window.innerWidth * 0.30) continue;
                    if (dy > window.innerHeight * 0.30) continue;

                    // Score: prefer larger font + closer to center.
                    const dist = Math.sqrt(dx * dx + dy * dy);
                    const score = fontSize * 100 - dist / 2 + raw.length * 3;
                    candidates.push({ text: raw, score, fontSize, dx, dy, cy });
                }

                if (!candidates.length) return { best: null, top: [] };

                // Determinism: pick only among the largest-font candidates.
                // This avoids selecting smaller labels near the prompt.
                let maxFont = candidates.reduce((m, c) => Math.max(m, c.fontSize), 0);
                const topFontCandidates = candidates.filter(c => c.fontSize >= maxFont * 0.9);

                topFontCandidates.sort((a, b) => (b.score - a.score) || (b.fontSize - a.fontSize));
                const best = topFontCandidates.length ? topFontCandidates[0].text : candidates[0].text;

                const top = candidates.slice(0, 6).map(c => ({ text: c.text, score: c.score, fontSize: c.fontSize, cy: c.cy }));
                // Sort top for better debugging display.
                top.sort((a,b)=>b.score-a.score);
                return { best, top: top.slice(0,6) };
            }"""
        )
        if not res:
            return None
        best = res.get("best")
        if best:
            # Helpful when the bot picks the wrong element.
            top = res.get("top") or []
            top_preview = ", ".join([t.get("text", "") for t in top[:3] if t.get("text")])
            print(f"[TYPING-DETECT] best={best!r} top={top_preview!r}")
            return best
        # If we couldn't detect, also print what it saw.
        top = res.get("top") or []
        if top:
            print(f"[TYPING-DETECT] no best found. top={[t.get('text') for t in top[:6]]}")
        return None
    except Exception:
        return None


def is_human_check_page(page: Page) -> bool:
    return False


def wait_for_login(
    page: Page,
    url: str,
    timeout: int,
    stop_event: Optional[threading.Event] = None,
) -> bool:
    print("Login appears to be required. Please log in using the opened browser window.")
    print("The script will continue automatically once the page leaves the login screen.")

    end = time.time() + timeout
    while time.time() < end:
        check_stop(stop_event)
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        try:
            if is_game_page(page):
                print(f"Detected game page while waiting for login. Current URL: {page.url}")
                return True
            if not is_login_page(page):
                print(f"Login page is gone. Current URL: {page.url}")
                return True
        except Exception as exc:
            print(f"Login detection failed while checking page state: {exc}")
            return False
        print("Still on the login page. Waiting for the game to load...")
        debug_page_state(page, "wait_for_login")
        if sleep_or_stop(1, stop_event):
            raise StopRequested()

    print("Login did not complete or the session was not preserved. Current URL:", page.url)
    print("Page body snippet:")
    body_text = page.text_content('body') or ''
    print(body_text[:400])
    return False


def wait_for_game_ready(
    page: Page,
    timeout: int,
    stop_event: Optional[threading.Event] = None,
    game_url: str = "",
) -> bool:
    end = time.time() + timeout
    iteration = 0
    candidates: List[str] = []
    emoji_mode = is_emoji_game_url(game_url)
    typing_mode = is_typing_game_url(game_url)

    def refresh_modes() -> None:
        nonlocal fast_mode, poll_ms
        fast_mode = emoji_mode or is_emoji_game(page) or typing_mode or is_typing_game(page)
        poll_ms = 120 if (emoji_mode or is_emoji_game(page)) else (200 if (typing_mode or is_typing_game(page)) else 1000)

    fast_mode = emoji_mode or is_emoji_game(page) or is_typing_game(page)
    poll_ms = 120 if (emoji_mode or is_emoji_game(page)) else (200 if is_typing_game(page) else 1000)

    while time.time() < end:
        check_stop(stop_event)
        if not page_is_usable(page):
            print("Browser tab was closed.")
            return False

        iteration += 1
        refresh_modes()

        try:
            if emoji_mode and not is_emoji_game(page):
                print(f"[EMOJI] Not on emoji page ({page.url}), navigating back...")
                ensure_emoji_page(page, game_url, timeout)
                safe_page_wait(page, 600)
                continue

            if not fast_mode:
                print(f"wait_for_game_ready iteration {iteration}, url={page.url}")
                if iteration % 3 == 1:
                    debug_page_state(page, f"wait_for_game_ready-{iteration}")

            if not safe_page_wait(page, poll_ms):
                return False
            check_stop(stop_event)

            body_text = ""
            if not fast_mode:
                try:
                    body_text = page.text_content("body") or ""
                except Exception as exc:
                    if is_navigation_error(exc) or is_page_closed_error(exc):
                        continue
                    raise

            if handle_human_check_popup(page):
                print("Handled human-check popup by typing the verification code.")
                safe_page_wait(page, 2000)
                continue

            if body_text and (
                "please start the game from the game center" in body_text.lower()
                or "oops!" in body_text.lower()
            ):
                print("Detected Keycash startup popup or game-center requirement. Not dismissing anything automatically.")
                print("Current page URL:", page.url)
                print("Body snippet:", body_text[:300])
                safe_page_wait(page, 2000)
                continue

            if find_human_check_popup(page):
                safe_page_wait(page, 1000)
                continue

            if is_emoji_game(page) or emoji_mode:
                state = gather_emoji_state(page)
                if len(state.get("options") or []) >= 2:
                    return True
            elif is_typing_game(page):
                prompt = detect_typing_prompt_text(page)
                if prompt:
                    print("Typing game appears ready.")
                    return True
            else:
                candidates = visible_math_texts(page)
                for text in candidates:
                    if "quick human check" in text.lower():
                        continue
                    if choose_expression(text):
                        print(f"Math candidate detected: {text}")
                        return True

                count_answer = extract_count_answer(page)
                if count_answer is not None:
                    print(f"Count question detected, answer: {count_answer}")
                    return True

                if (
                    page.url.lower().startswith("https://keycash.pro/?c=games")
                    or "start-game=math" in page.url.lower()
                    or "play now" in body_text.lower()
                ):
                    print("Attempting to start the math game from games page")
                    if start_math_game(page):
                        continue

                if not page.url.lower().startswith("https://keycash.pro/?c=math") and navigate_to_game_center(page):
                    print("Navigated to game center, trying to start math game")
                    if start_math_game(page):
                        continue

        except Exception as exc:
            if is_page_closed_error(exc):
                print("Browser was closed during wait.")
                return False
            if is_navigation_error(exc):
                safe_page_wait(page, 300)
                continue
            raise

    try:
        current_url = page.url
        snippet = (page.text_content("body") or "")[:500]
    except Exception:
        current_url = "(unavailable)"
        snippet = ""
    print("Game did not become ready. Current URL:", current_url)
    print("Body snippet:")
    print(snippet)
    print("Visible text candidates:")
    for candidate in (candidates or [])[:12]:
        print(f"  - {candidate}")
    return False


def play_once(
    page: Page,
    question_selector: Optional[str],
    pause: float,
    manual_answer: bool,
    type_answer: bool,
    game_url: str = "",
    emoji_ai: Optional[EmojiAIConfig] = None,
    emoji_answer_delay: Optional[float] = None,
) -> bool:
    if not page_is_usable(page):
        return False

    if is_emoji_game_url(game_url) and not is_emoji_game(page):
        ensure_emoji_page(page, game_url)

    try:
        popup = find_human_check_popup(page)
    except Exception as exc:
        if is_page_closed_error(exc):
            return False
        if is_navigation_error(exc):
            popup = None
        else:
            raise
    if popup:
        if handle_human_check_popup(page):
            print("Handled human-check popup before solving the question.")
            safe_page_wait(page, 1000)
        try:
            still = find_human_check_popup(page)
        except Exception:
            still = None
        if still:
            print("Human-check popup still visible; skipping game question for now.")
            return True

    print(f"[DEBUG] play_once current url={page.url}")

    # Emoji ID game: fast path (minimal waits, batched DOM reads).
    emoji_url = game_url if is_emoji_game_url(game_url) else page.url
    if is_emoji_game(page) or is_emoji_game_url(game_url):
        if play_emoji_once(page, pause, emoji_url, emoji_ai, emoji_answer_delay):
            return True
        print("Could not answer emoji question.")
        return False

    # Typing game: read the central word and type it into the input.
    if is_typing_game(page):
        word = detect_typing_prompt_text(page)
        if not word:
            print("Could not detect typing word on the page.")
            return False

        print(f"Detected typing challenge word: {word!r}")
        if not type_answer_in_input_fast(page, word):
            print("Failed to type the challenge word into an input.")
            return False

        fast_pause = effective_pause(page, pause)
        if fast_pause > 0:
            time.sleep(fast_pause)
        return True
    question_text = find_question_text(page, question_selector)
    expr = choose_expression(question_text)
    if not expr:
        print("Could not detect a math expression on the page.")
        candidates = visible_math_texts(page)
        if candidates:
            print("Detected text candidates:")
            for candidate in candidates[:8]:
                print(f"  - {candidate}")
        else:
            print("No visible math-like text candidates were found.")

        if is_count_question(question_text):
            count_answer = count_repeated_items(question_text) or extract_count_answer(page)
            if count_answer is not None:
                answer_text = str(int(count_answer)) if count_answer == int(count_answer) else str(count_answer)
                if type_answer_in_input(page, answer_text):
                    print(f"Typed count question answer into input: {answer_text}")
                    if pause > 0:
                        time.sleep(pause)
                    return True
                print(f"Detected count question answer {answer_text} but could not type into a visible input.")
                if manual_answer and type_manual_answer(page):
                    if pause > 0:
                        time.sleep(pause)
                    return True
                return False

        if type_answer:
            count_answer = extract_count_answer(page)
            if count_answer is not None:
                answer_text = str(int(count_answer)) if count_answer == int(count_answer) else str(count_answer)
                if type_answer_in_input(page, answer_text):
                    print(f"Typed count-based answer into input: {answer_text}")
                    if pause > 0:
                        time.sleep(pause)
                    return True
                print(f"Detected count answer {answer_text} but could not type into a visible input.")
                if manual_answer and type_manual_answer(page):
                    if pause > 0:
                        time.sleep(pause)
                    return True

        if manual_answer:
            answer_text = prompt_manual_answer()
            if answer_text:
                option = find_answer_option_by_text(page, answer_text)
                if option:
                    try:
                        option.click()
                    except Exception:
                        option.evaluate("el => el.click()")
                    print(f"Clicked manual answer option: {answer_text}")
                    if pause > 0:
                        time.sleep(pause)
                    return True
                if type_answer and type_answer_in_input(page, answer_text):
                    print(f"Typed manual answer into input: {answer_text}")
                    if pause > 0:
                        time.sleep(pause)
                    return True
                print("No answer button matched the pasted text.")
        return False

    answer = safe_eval(expr)
    print(f"Detected question: {question_text.strip()}")
    print(f"Expression: {expr}")
    print(f"Computed answer: {answer}")

    option = find_answer_option(page, answer)
    if not option and type_answer:
        answer_text = str(int(answer)) if answer == int(answer) else str(answer)
        if type_answer_in_input(page, answer_text):
            print(f"Typed computed answer into input: {answer_text}")
            if pause > 0:
                time.sleep(pause)
            return True

    if not option and manual_answer:
        print("Could not find a matching answer button on the page. Please paste the human-check answer if available.")
        answer_text = prompt_manual_answer()
        if answer_text:
            option = find_answer_option_by_text(page, answer_text)
            if not option and type_answer and type_answer_in_input(page, answer_text):
                print(f"Typed manual answer into input: {answer_text}")
                if pause > 0:
                    time.sleep(pause)
                return True

    if not option:
        print("Could not find a matching answer button on the page.")
        print("Visible answer candidates:")
        for element in page.query_selector_all("button, [role='button'], div, span, a"):
            if not element.is_visible():
                continue
            text = get_element_text(element)
            if not text:
                continue
            value = extract_numeric_value(text)
            if value is not None:
                print(f"  - '{text}' (numeric {value})")
            else:
                print(f"  - '{text}'")
        return False

    try:
        option.click()
    except Exception:
        option.evaluate("el => el.click()")
    print(f"Clicked answer option: {answer}")
    if pause > 0:
        time.sleep(pause)
    return True


def extract_coin_balance(page: Page) -> Optional[int]:
    """Read the in-game coin balance from the Keycash page."""
    try:
        coins = page.evaluate(
            """() => {
                const body = (document.body && document.body.innerText) || '';
                const patterns = [
                    /COINS\\s*\\n\\s*([\\d,]+)/i,
                    /COINS\\s+([\\d,]+)/i,
                ];
                for (const pattern of patterns) {
                    const match = body.match(pattern);
                    if (match) {
                        const value = parseInt(match[1].replace(/,/g, ''), 10);
                        if (!Number.isNaN(value)) return value;
                    }
                }

                const labels = Array.from(document.querySelectorAll('body *')).filter(el => {
                    const text = (el.innerText || '').trim();
                    return /^COINS$/i.test(text) && el.children.length === 0;
                });
                for (const label of labels) {
                    let sibling = label.nextElementSibling;
                    if (sibling) {
                        const value = parseInt((sibling.textContent || '').replace(/,/g, ''), 10);
                        if (!Number.isNaN(value)) return value;
                    }
                    const parentText = (label.parentElement && label.parentElement.innerText) || '';
                    const match = parentText.match(/COINS\\s*\\n\\s*([\\d,]+)/i);
                    if (match) {
                        const value = parseInt(match[1].replace(/,/g, ''), 10);
                        if (!Number.isNaN(value)) return value;
                    }
                }
                return null;
            }"""
        )
        if coins is None:
            return None
        return int(coins)
    except Exception:
        return None


def report_automation_status(
    page: Page,
    question_count: int,
    status_callback: Optional[Callable[[Dict[str, Optional[int]]], None]],
    baseline_coins: Optional[int],
) -> Optional[int]:
    """Notify GUI listeners and return the baseline coin count when first seen."""
    coins = extract_coin_balance(page)
    if baseline_coins is None and coins is not None:
        baseline_coins = coins

    gained = None
    if coins is not None and baseline_coins is not None:
        gained = coins - baseline_coins

    # Emit a structured status line so the GUI subprocess reader can parse it.
    print(
        f"[STATUS] coins={coins if coins is not None else ''} "
        f"gained={gained if gained is not None else ''} "
        f"questions={question_count}",
        flush=True,
    )

    if status_callback is not None:
        status_callback(
            {
                "coins": coins,
                "questions_answered": question_count,
                "coins_gained": gained,
                "baseline_coins": baseline_coins,
            }
        )

    return baseline_coins


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Automate the Keycash math game by selecting the correct answer.")
    parser.add_argument("--url", default="https://keycash.pro/?c=math", help="The Keycash math game URL.")
    parser.add_argument("--user-data-dir", default="./keycash_profile", help="Directory to store browser session data.")
    parser.add_argument("--question-selector", help="Optional CSS selector for the math question text.")
    parser.add_argument("--iterations", type=int, default=0, help="Number of questions to answer. 0 means keep answering until failure or stop.")
    parser.add_argument("--pause", type=float, default=1.0, help="Seconds to wait after selecting an answer.")
    parser.add_argument("--timeout", type=int, default=30, help="Navigation timeout in seconds.")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode.")
    parser.add_argument("--login", action="store_true", help="Pause and allow manual login if needed.")
    parser.add_argument("--keep-open", action="store_true", help="Keep the browser open until you close it manually.")
    parser.add_argument("--close", action="store_true", help="Close the browser automatically after the automation finishes.")
    parser.add_argument("--manual-answer", action="store_true", help="Prompt to paste a human-check answer when auto-detection fails.")
    parser.add_argument(
        "--type-answer",
        action="store_true",
        default=True,
        help="Type the computed or pasted answer into a visible text input if no button is found (default: on).",
    )
    parser.add_argument(
        "--no-type-answer",
        action="store_false",
        dest="type_answer",
        help="Do not type answers into text inputs.",
    )
    parser.add_argument(
        "--emoji-ai",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Use OpenAI vision for Emoji ID (default: on for ?c=emoji when OPENAI_API_KEY is set).",
    )
    parser.add_argument(
        "--openai-api-key",
        default="",
        help="OpenAI API key (or set OPENAI_API_KEY in the environment).",
    )
    parser.add_argument(
        "--emoji-ai-model",
        default="gpt-4o-mini",
        help="OpenAI vision model for Emoji ID (default: gpt-4o-mini).",
    )
    parser.add_argument(
        "--emoji-answer-delay",
        type=float,
        default=None,
        help="Seconds to wait after clicking an Emoji ID answer (overrides --pause for emoji game).",
    )
    return parser


def wait_for_game_ready_with_retry(
    page: Page,
    timeout: int,
    stop_event: Optional[threading.Event],
    game_url: str,
    max_retries: int = 5,
) -> bool:
    """
    Like wait_for_game_ready but navigates back to game_url and retries
    up to max_retries times before giving up.  This prevents the automation
    from stopping just because the page was briefly slow or redirected.
    """
    for attempt in range(1, max_retries + 1):
        check_stop(stop_event)
        if not page_is_usable(page):
            return False

        if wait_for_game_ready(page, timeout, stop_event, game_url):
            return True

        check_stop(stop_event)
        if not page_is_usable(page):
            return False

        print(
            f"Game not ready (attempt {attempt}/{max_retries}). "
            f"Navigating back to {game_url} and retrying..."
        )
        try:
            navigate_to_game(page, game_url, max(timeout, 45))
        except Exception as exc:
            if is_page_closed_error(exc):
                return False
            print(f"Re-navigation error: {exc}")

        # Brief pause before next attempt so the page can settle.
        if sleep_or_stop(2.0, stop_event):
            return False

    print(f"Game did not become ready after {max_retries} retries. Giving up.")
    return False


def navigate_to_game(page: Page, url: str, timeout: int) -> None:
    """Open the game URL, tolerating redirects and aborted loads on Keycash."""
    last_error: Optional[Exception] = None
    for attempt in range(3):
        try:
            page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass
            try:
                page.wait_for_load_state("networkidle", timeout=min(10000, timeout * 1000))
            except Exception:
                pass
            return
        except Exception as exc:
            last_error = exc
            message = str(exc).lower()
            if "err_aborted" in message or "detached" in message or "interrupted" in message:
                print(f"Navigation interrupted (attempt {attempt + 1}/3), retrying...")
                try:
                    page.wait_for_timeout(1200)
                except Exception:
                    pass
                continue
            raise
    if last_error is not None:
        raise last_error


def warn_emoji_ai_setup(args: argparse.Namespace, emoji_ai: EmojiAIConfig) -> None:
    wants_ai = getattr(args, "emoji_ai", None)
    if wants_ai is None:
        wants_ai = is_emoji_game_url(getattr(args, "url", ""))
    if not wants_ai or not is_emoji_game_url(getattr(args, "url", "")):
        return
    if emoji_ai.enabled:
        print(f"[EMOJI-AI] Vision enabled (model={emoji_ai.model})")
        return
    if not get_emoji_ai_api_key(getattr(args, "openai_api_key", "") or ""):
        print(
            "[EMOJI-AI] Emoji URL detected but no API key. Set OPENAI_API_KEY or pass "
            "--openai-api-key. Falling back to DOM matching only."
        )


def run_automation(
    args: argparse.Namespace,
    stop_event: Optional[threading.Event] = None,
    status_callback: Optional[Callable[[Dict[str, Optional[int]]], None]] = None,
) -> int:
    """Run the Keycash automation loop. Returns the number of answered questions."""
    question_count = 0
    browser: Optional[BrowserContext] = None
    baseline_coins: Optional[int] = None
    emoji_ai = build_emoji_ai_config(args)
    warn_emoji_ai_setup(args, emoji_ai)

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch_persistent_context(
                user_data_dir=args.user_data_dir,
                headless=args.headless,
                viewport={"width": 1280, "height": 900},
            )
            page = browser.new_page()
            try:
                navigate_to_game(page, args.url, args.timeout)
            except Exception as exc:
                print(f"Navigation failed: {exc}")
                print("Retrying with a fresh browser tab...")
                page = browser.new_page()
                navigate_to_game(page, args.url, max(args.timeout, 45))
            baseline_coins = report_automation_status(page, question_count, status_callback, baseline_coins)

            startup_failed = False
            if is_login_page(page):
                if not args.login:
                    print("Login is required to access the game. Run with --login or use a logged-in profile.")
                    startup_failed = True
                elif not wait_for_login(page, args.url, args.timeout, stop_event):
                    print("Login failed or the game page did not appear after login.")
                    startup_failed = True

            if not startup_failed and not wait_for_game_ready(
                page, args.timeout, stop_event, args.url
            ):
                hint = (
                    "Make sure the emoji game is visible."
                    if is_emoji_game_url(args.url)
                    else "Make sure the game is visible."
                )
                print(f"Game page did not become ready after login. {hint}")
                startup_failed = True

            baseline_coins = report_automation_status(page, question_count, status_callback, baseline_coins)

            if not startup_failed:
                if args.iterations <= 0:
                    while True:
                        check_stop(stop_event)
                        baseline_coins = report_automation_status(
                            page, question_count, status_callback, baseline_coins
                        )
                        if not wait_for_game_ready_with_retry(
                            page, args.timeout, stop_event, args.url
                        ):
                            print("Stopping automation because the next question did not appear.")
                            break
                        if not page_is_usable(page):
                            print("Browser was closed.")
                            break
                        success = play_once(
                            page,
                            args.question_selector,
                            args.pause,
                            args.manual_answer,
                            args.type_answer,
                            args.url,
                            emoji_ai,
                            getattr(args, "emoji_answer_delay", None),
                        )
                        if not success:
                            print("Could not answer this question. Retrying after pause.")
                            if sleep_or_stop(effective_pause(page, args.pause), stop_event):
                                break
                            continue
                        question_count += 1
                        baseline_coins = report_automation_status(
                            page, question_count, status_callback, baseline_coins
                        )
                else:
                    while question_count < args.iterations:
                        check_stop(stop_event)
                        baseline_coins = report_automation_status(
                            page, question_count, status_callback, baseline_coins
                        )
                        if not wait_for_game_ready_with_retry(
                            page, args.timeout, stop_event, args.url
                        ):
                            print("Stopping automation because the next question did not appear.")
                            break
                        if not page_is_usable(page):
                            print("Browser was closed.")
                            break
                        success = play_once(
                            page,
                            args.question_selector,
                            args.pause,
                            args.manual_answer,
                            args.type_answer,
                            args.url,
                            emoji_ai,
                            getattr(args, "emoji_answer_delay", None),
                        )
                        if not success:
                            print("Could not answer this question. Retrying after pause.")
                            if sleep_or_stop(effective_pause(page, args.pause), stop_event):
                                break
                            continue
                        question_count += 1
                        baseline_coins = report_automation_status(
                            page, question_count, status_callback, baseline_coins
                        )
                        if question_count < args.iterations and not is_emoji_game_url(args.url):
                            print("Waiting for next question...")
                            if sleep_or_stop(effective_pause(page, args.pause), stop_event):
                                break
            else:
                print("Automation did not start due to a startup problem. Browser will remain open for inspection.")

            baseline_coins = report_automation_status(page, question_count, status_callback, baseline_coins)
            print(f"Automation finished after answering {question_count} question(s).")
            keep_open = args.keep_open or (not args.close and not should_stop(stop_event))
            if keep_open and not should_stop(stop_event):
                print("Browser will remain open. Press Ctrl+C to exit and close it.")
                try:
                    while True:
                        if sleep_or_stop(3600, stop_event):
                            break
                except KeyboardInterrupt:
                    print("Exiting and closing browser...")
            else:
                try:
                    browser.close()
                except Exception as exc:
                    print("Browser context was already closed or could not be closed:", exc)
    except StopRequested:
        print("Automation stopped by user.")
        if browser is not None:
            try:
                browser.close()
            except Exception as exc:
                print("Browser context was already closed or could not be closed:", exc)

    return question_count


def main():
    parser = build_arg_parser()
    args = parser.parse_args()
    run_automation(args)


if __name__ == "__main__":
    main()