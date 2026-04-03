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


def extract_semantic_data(html_content, url):
    soup = BeautifulSoup(html_content, 'html.parser')

    # Grab title and meta before stripping anything
    title_tag = soup.find('title')
    title_text = title_tag.get_text(strip=True) if title_tag else ''

    meta_desc_tag = soup.find('meta', attrs={'name': 'description'})
    meta_desc_text = meta_desc_tag.get('content', '') if meta_desc_tag else ''

    # Strip noise
    for element in soup(["script", "style", "noscript", "header", "footer", "nav", "iframe"]):
        element.decompose()

    elements_data = []
    markdown_lines = []
    human_lines = []

    # Always include title and meta at the top of output
    if title_text:
        elements_data.append({"type": "TITLE", "text": title_text})
        markdown_lines.append(f"<TITLE>{title_text}</TITLE>")
        human_lines.append(f"[TITLE] {title_text}")

    if meta_desc_text:
        elements_data.append({"type": "META_DESC", "text": meta_desc_text})
        markdown_lines.append(f"<META_DESC>{meta_desc_text}</META_DESC>")
        human_lines.append(f"[META DESCRIPTION] {meta_desc_text}")

    # Prefer main content containers to reduce nav/sidebar noise
    main_content = (
        soup.find('main') or
        soup.find('article') or
        soup.find(attrs={"role": "main"}) or
        soup.find('body') or
        soup
    )

    seen_texts = set()

    for tag in main_content.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'li', 'img', 'a']):
        text = tag.get_text(strip=True)

        # Deduplicate repeated text blocks
        if text and text in seen_texts:
            continue
        if text:
            seen_texts.add(text)

        if tag.name.startswith('h'):
            elements_data.append({"type": tag.name.upper(), "text": text})
            markdown_lines.append(f"<{tag.name.upper()}>{text}</{tag.name.upper()}>")
            human_lines.append(f"[{tag.name.upper()}] {text}")

        elif tag.name == 'p' and text and len(text) > 20:
            elements_data.append({"type": "P", "text": text})
            markdown_lines.append(f"<P>{text}</P>")
            human_lines.append(f"{text}\n")

        elif tag.name == 'li' and text:
            elements_data.append({"type": "LI", "text": text})
            markdown_lines.append(f"<LI>{text}</LI>")
            human_lines.append(f"  • {text}")

        elif tag.name == 'img':
            alt = tag.get('alt', '')
            src = tag.get('src', '')
            title_attr = tag.get('title', '')
            if alt or src:
                elements_data.append({"type": "IMG", "alt": alt, "src": src, "title": title_attr})
                markdown_lines.append(f"<IMG alt='{alt}' title='{title_attr}' src='{src}'/>")
                human_lines.append(f"[IMAGE] Alt: {alt} | Title: {title_attr}")

        elif tag.name == 'a' and text:
            href = tag.get('href', '')
            if href and not href.startswith('javascript:') and not href.startswith('#'):
                elements_data.append({"type": "LINK", "text": text, "href": href})
                markdown_lines.append(f"<A href='{href}'>{text}</A>")
                human_lines.append(f"[LINK: {text}] -> {href}")

    return {
        "elements": elements_data,
        "llm_markdown": "\n".join(markdown_lines),
        "human_readable": "\n".join(human_lines),
        "page_title": title_text,
        "meta_description": meta_desc_text,
    }


def scrape_with_httpx(url):
    """Fast path — no browser needed. Works for most static/SSR sites."""
    with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=30) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.text


def scrape_with_playwright(url):
    """Fallback for JS-heavy sites (SPAs, React, etc.)."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-gpu", "--single-process"]
        )
        context = browser.new_context(user_agent=HEADERS["User-Agent"])
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_selector("h1, p", timeout=15000)
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

    # Be forgiving about missing protocol
    if not url.startswith('http'):
        url = 'https://' + url

    html_content = None
    extraction_method = None

    # Try fast httpx first — saves Render memory and is much quicker
    try:
        html_content = scrape_with_httpx(url)
        extraction_method = "httpx"
    except Exception:
        # Page likely requires JavaScript — spin up Playwright
        try:
            html_content = scrape_with_playwright(url)
            extraction_method = "playwright_headless"
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    extracted = extract_semantic_data(html_content, url)

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
    """Render can ping this to keep the service warm."""
    return jsonify({"status": "ok"})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
