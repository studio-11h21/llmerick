import httpx
from flask import Flask, request, jsonify
from flask_cors import CORS
from bs4 import BeautifulSoup

app = Flask(__name__)
CORS(app)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# A page is considered "thin" (JS shell) if the total visible text content
# across all real elements is below this character count.
MIN_CONTENT_CHARS = 1000


def get_image_src(tag):
    """
    Returns the best available image URL from a tag.
    Checks real src first, then common lazy-load attributes used by
    Shopify, WordPress, and other CMS platforms.
    """
    for attr in ['src', 'data-src', 'data-lazy-src', 'data-original', 'data-srcset']:
        val = tag.get(attr, '').strip()
        if val and attr == 'data-srcset':
            val = val.split(',')[0].split(' ')[0].strip()
        if val and not val.startswith('data:'):
            return val
    return ''


def extract_semantic_data(html_content, url):
    soup = BeautifulSoup(html_content, 'html.parser')

    # Grab title and meta before stripping anything
    title_tag = soup.find('title')
    title_text = title_tag.get_text(strip=True) if title_tag else ''

    meta_desc_tag = soup.find('meta', attrs={'name': 'description'})
    meta_desc_text = meta_desc_tag.get('content', '') if meta_desc_tag else ''

    # Strip noise — remove these before any content search
    for element in soup(["script", "style", "noscript", "header", "footer", "nav", "iframe"]):
        element.decompose()

    elements_data = []
    markdown_lines = []
    human_lines = []

    if title_text:
        elements_data.append({"type": "TITLE", "text": title_text})
        markdown_lines.append(f"<TITLE>{title_text}</TITLE>")
        human_lines.append(f"[TITLE] {title_text}")

    if meta_desc_text:
        elements_data.append({"type": "META_DESC", "text": meta_desc_text})
        markdown_lines.append(f"<META_DESC>{meta_desc_text}</META_DESC>")
        human_lines.append(f"[META DESCRIPTION] {meta_desc_text}")

    # Use the full body — header/footer/nav are already stripped above.
    # This ensures we capture content in sections, articles, and divs
    # that sit outside of <main>, which is common in CMS-built sites.
    content_root = soup.find('body') or soup

    seen_texts = set()

    for tag in content_root.find_all(
        ['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'li', 'a']
    ):
        text = tag.get_text(strip=True)

        if not text or text in seen_texts:
            continue
        seen_texts.add(text)

        if tag.name.startswith('h'):
            elements_data.append({"type": tag.name.upper(), "text": text})
            markdown_lines.append(f"<{tag.name.upper()}>{text}</{tag.name.upper()}>")
            human_lines.append(f"[{tag.name.upper()}] {text}")

        elif tag.name == 'p' and len(text) > 20:
            elements_data.append({"type": "P", "text": text})
            markdown_lines.append(f"<P>{text}</P>")
            human_lines.append(f"{text}\n")

        elif tag.name == 'li' and text:
            elements_data.append({"type": "LI", "text": text})
            markdown_lines.append(f"<LI>{text}</LI>")
            human_lines.append(f"  • {text}")

        elif tag.name == 'a':
            href = tag.get('href', '')
            if href and not href.startswith('javascript:') and not href.startswith('#'):
                elements_data.append({"type": "LINK", "text": text, "href": href})
                markdown_lines.append(f"<A href='{href}'>{text}</A>")
                human_lines.append(f"[LINK: {text}] -> {href}")

    # ── FAQ: capture questions from buttons and answers from hidden divs ─
    # Many CMS FAQ sections use <button> for questions and hide answers
    # with style="display:none" — BeautifulSoup reads these regardless.
    seen_faq = set()
    for btn in content_root.find_all('button', class_='faqitem'):
        question_tag = btn.find(['h2', 'h3', 'h4', 'span', 'p'])
        question = question_tag.get_text(strip=True) if question_tag else btn.get_text(strip=True)
        if not question or question in seen_faq:
            continue
        seen_faq.add(question)

        # Answer is usually the next sibling div with class "answer"
        answer_div = btn.find_next_sibling('div')
        answer = answer_div.get_text(strip=True) if answer_div else ''

        elements_data.append({"type": "FAQ", "question": question, "answer": answer})
        markdown_lines.append(f"<FAQ><Q>{question}</Q><A>{answer}</A></FAQ>")
        human_lines.append(f"[FAQ] Q: {question}\n      A: {answer}\n")

    # ── Images: scan the whole document ─────────────────────────────────
    seen_imgs = set()
    for tag in soup.find_all('img'):
        alt = tag.get('alt', '').strip()
        title_attr = tag.get('title', '').strip()
        src = get_image_src(tag)

        if not alt and not title_attr:
            continue

        img_key = (alt, src)
        if img_key in seen_imgs:
            continue
        seen_imgs.add(img_key)

        elements_data.append({"type": "IMG", "alt": alt, "src": src, "title": title_attr})
        markdown_lines.append(f"<IMG alt='{alt}' title='{title_attr}' src='{src}'/>")
        human_lines.append(f"[IMAGE] Alt: {alt} | Title: {title_attr} | Src: {src}")

    return {
        "elements": elements_data,
        "llm_markdown": "\n".join(markdown_lines),
        "human_readable": "\n".join(human_lines),
        "page_title": title_text,
        "meta_description": meta_desc_text,
    }


def content_is_thin(extracted):
    """
    Returns True if the page looks like an empty JS shell.
    Measures total character count of headings, paragraphs and list items only.
    """
    substantive_types = {"H1", "H2", "H3", "H4", "H5", "H6", "P", "LI"}
    total_chars = sum(
        len(e.get("text", ""))
        for e in extracted["elements"]
        if e["type"] in substantive_types
    )
    return total_chars < MIN_CONTENT_CHARS


def scrape_with_httpx(url):
    with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=30) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.text


def scrape_with_playwright(url):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-gpu",
                "--single-process",
                "--disable-dev-shm-usage",
                "--disable-setuid-sandbox",
            ]
        )
        context = browser.new_context(user_agent=HEADERS["User-Agent"])
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_selector("h1, p, img", timeout=15000)
        except Exception:
            page.wait_for_timeout(5000)
        page.wait_for_timeout(2000)
        html = page.content()
        browser.close()
        return html


@app.route('/api/scrape', methods=['GET'])
def scrape():
    url = request.args.get('url')
    if not url:
        return jsonify({"status": "error", "message": "No URL provided"}), 400

    if not url.startswith('http'):
        url = 'https://' + url

    extraction_method = None

    try:
        html_content = scrape_with_httpx(url)
        extracted = extract_semantic_data(html_content, url)
        extraction_method = "httpx"

        if content_is_thin(extracted):
            raise ValueError("Thin content detected — retrying with Playwright")

    except Exception:
        try:
            html_content = scrape_with_playwright(url)
            extracted = extract_semantic_data(html_content, url)
            extraction_method = "playwright_headless"
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    return jsonify({
        "status": "success",
        "extraction_method": extraction_method,
        "human_readable": extracted["human_readable"],
        "llm_markdown": extracted["llm_markdown"],
        "json_data": {
            "target_url": url,
            "page_title": extracted["page_title"],
            "meta_description": extracted["meta_description"],
            "extraction_method": extraction_method,
            "elements": extracted["elements"],
        }
    })


@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok"})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
