"""
Zotero <-> reMarkable Sync (combined)
======================================
Three-way sync: Zotero <-> reMarkable Cloud, OneDrive -> Zotero

Folders on the reMarkable:
  /From Zotero     -- PDFs sent from the Zotero "Send Remarkable" collection land
                      here, named "Author - Year - Title [ZOTEROKEY]".
  /Zotero Upload   -- inbox. Anything moved in here is pulled into Zotero with its
                      annotations rendered into the PDF and handwriting transcribed:
                        * name ends in [ZOTEROKEY] -> annotated PDF + transcription
                          note are attached to that ORIGINAL Zotero item, and the
                          item's older PDF goes to Zotero's trash (one PDF per item;
                          a PDF with Zotero-reader highlights is kept). The item also
                          gets 3 topical tags (from your existing tags where possible)
                          and Related links to works it cites and works in the same
                          conversation that are in your library.
                        * no ID, but an "Author - Year - Title" name that matches exactly
                          one item in your library -> treated the same way (documents
                          sent to the tablet before IDs were added to names)
                        * otherwise -> a new item in the import collection
                      the document is moved to /Zotero Upload/Imported.
Safe to run on more than one computer: each piece of work is claimed first with an
operation only one machine can win (see "claiming work" below).

New features:
  - Zotero->reMarkable: filenames built from Author - Year - Title
  - OneDrive->Zotero: auto DOI extraction + Crossref metadata lookup

Run:   python zotero_remarkable_sync.py
Setup: python zotero_remarkable_sync.py --setup
Once:  python zotero_remarkable_sync.py --once
"""

import os, sys, json, time, shutil, argparse, logging, subprocess, re, zipfile, base64, io, posixpath
from datetime import datetime, date, timezone
from pathlib import Path
from threading import Thread

try:
    from pyzotero import zotero
except ImportError:
    print("ERROR: pyzotero not installed. Run:  pip install pyzotero")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("ERROR: requests not installed. Run:  pip install requests")
    sys.exit(1)

try:
    from pypdf import PdfReader
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False

# ============================================================
# PATHS / CONFIG / STATE
# ============================================================
SCRIPT_DIR  = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "sync_config.json"
STATE_FILE  = SCRIPT_DIR / "sync_state.json"
STAGING_DIR = SCRIPT_DIR / "staging"

def setup_logging(log_file="sync.log"):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(SCRIPT_DIR / log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )

log = logging.getLogger(__name__)

def load_config():
    if not CONFIG_FILE.exists(): return None
    with open(CONFIG_FILE, encoding="utf-8") as f: return json.load(f)

def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f: json.dump(cfg, f, indent=2)

def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, encoding="utf-8") as f: return json.load(f)
    return {"synced_to_zotero": [], "synced_from_zotero": {}}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f: json.dump(state, f, indent=2)

# ============================================================
# RMAPI
# ============================================================
def find_rmapi():
    for c in [SCRIPT_DIR / "rmapi.exe", SCRIPT_DIR / "rmapi"]:
        if c.is_file(): return str(c)
    import shutil as sh; return sh.which("rmapi")

def rmapi_run(rmapi_path, args, timeout=120):
    try:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        r = subprocess.run([rmapi_path]+args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           cwd=str(SCRIPT_DIR), creationflags=flags)
        return r.returncode==0, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired: return False, "", "Timeout"
    except Exception as e: return False, "", str(e)

def rmapi_ls(rmapi_path, folder):
    ok, stdout, stderr = rmapi_run(rmapi_path, ["ls", folder])
    if not ok: log.error(f"rmapi ls failed: {stderr}"); return []
    entries = []
    for line in stdout.splitlines():
        m = re.match(r'^\[([df])\]\s+(.+)$', line.strip())
        if m: entries.append((m.group(1), m.group(2)))
    return entries

def rmapi_get(rmapi_path, remote_path, local_dir):
    try:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        # Use 'geta' (get-with-annotations) instead of plain 'get': plain 'get'
        # on an .rmdoc only extracts the clean, pre-annotation original PDF --
        # any highlights/handwritten notes/inserted notes pages are stored
        # separately as .rm stroke files and never make it into that PDF.
        # 'geta --a' asks reMarkable's cloud to render a PDF that actually
        # includes the annotations and all pages (including inserted ones).
        r = subprocess.run([rmapi_path,"geta","--a",remote_path], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120,
                           cwd=str(local_dir), creationflags=flags)
        return r.returncode==0, r.stdout.strip(), r.stderr.strip()
    except Exception as e: return False, "", str(e)

def rmapi_put(rmapi_path, local_file, remote_folder):
    """Upload a local file to reMarkable. Run from file's directory so rmapi uses just the filename."""
    local_file = Path(local_file)
    try:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        r = subprocess.run(
            [rmapi_path, "put", local_file.name, remote_folder],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=120, cwd=str(local_file.parent),
            creationflags=flags,
        )
        return r.returncode==0, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return False, "", str(e)

# ============================================================
# FILENAME HELPERS
# ============================================================
def sanitize(name):
    for ch in '<>:"/\\|?*': name = name.replace(ch, "_")
    return name.strip(". ")[:200]

def build_filename(item_data, item_key=None):
    """Build 'Author - Year - Title [ZOTEROKEY].pdf' from Zotero item metadata. The
    key lets an annotated copy find its way back to the original Zotero item."""
    # Author
    creators = item_data.get("creators", [])
    author = ""
    for c in creators:
        if c.get("creatorType") in ("author", "editor"):
            author = c.get("lastName") or c.get("name", "")
            break
    if not author:
        author = "Unknown"

    # Year
    date = item_data.get("date", "")
    year_match = re.search(r'\b(\d{4})\b', date)
    year = year_match.group(1) if year_match else "n.d."

    # Title (truncated)
    title = item_data.get("title", "Untitled")
    if len(title) > 80:
        title = title[:80].rsplit(" ", 1)[0]

    base = sanitize(f"{author} - {year} - {title}")
    if item_key:
        base = f"{base} [{item_key}]"
    return f"{base}.pdf"

# ============================================================
# DOI + CROSSREF METADATA
# ============================================================
def extract_doi_from_pdf(pdf_path):
    """Try to extract a DOI from a PDF's text or metadata."""
    if not HAS_PYPDF:
        return None
    doi_pattern = re.compile(r'\b10\.\d{4,9}/[^\s"\'<>]+', re.IGNORECASE)
    try:
        reader = PdfReader(str(pdf_path))
        # Check metadata first
        if reader.metadata:
            for v in reader.metadata.values():
                if isinstance(v, str):
                    m = doi_pattern.search(v)
                    if m: return m.group(0).rstrip(".,;)")
        # Check first two pages of text
        for i, page in enumerate(reader.pages[:2]):
            text = page.extract_text() or ""
            m = doi_pattern.search(text)
            if m: return m.group(0).rstrip(".,;)")
    except Exception as e:
        log.debug(f"DOI extraction failed: {e}")
    return None

def lookup_crossref(doi):
    """Fetch citation metadata from Crossref for a given DOI."""
    try:
        url = f"https://api.crossref.org/works/{requests.utils.quote(doi, safe='')}"
        headers = {"User-Agent": "ZoteroRemarkableSync/1.0 (mailto:sync@local)"}
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            return None
        msg = r.json().get("message", {})

        # Authors
        authors = []
        for a in msg.get("author", []):
            name = a.get("family", "")
            if a.get("given"): name = f"{a['given']} {name}"
            if name: authors.append(name.strip())

        # Year
        date_parts = msg.get("published", msg.get("published-print", msg.get("issued", {})))
        parts = date_parts.get("date-parts", [[None]])[0]
        year = str(parts[0]) if parts and parts[0] else ""

        # Journal
        container = ""
        if msg.get("container-title"):
            container = msg["container-title"][0]

        return {
            "title":     (msg.get("title") or [""])[0],
            "authors":   authors,
            "year":      year,
            "journal":   container,
            "doi":       doi,
            "volume":    msg.get("volume", ""),
            "issue":     msg.get("issue", ""),
            "pages":     msg.get("page", ""),
            "publisher": msg.get("publisher", ""),
        }
    except Exception as e:
        log.warning(f"Crossref lookup failed for {doi}: {e}")
        return None

def search_crossref_by_title(title):
    """Search Crossref by title if no DOI found."""
    try:
        url = "https://api.crossref.org/works"
        headers = {"User-Agent": "ZoteroRemarkableSync/1.0 (mailto:sync@local)"}
        r = requests.get(url, params={"query.title": title, "rows": 1}, headers=headers, timeout=10)
        if r.status_code != 200: return None
        items = r.json().get("message", {}).get("items", [])
        if not items: return None
        # Only use if title similarity is reasonable
        best = items[0]
        best_title = (best.get("title") or [""])[0].lower()
        if not any(w in best_title for w in title.lower().split()[:3]):
            return None
        doi = best.get("DOI", "")
        return lookup_crossref(doi) if doi else None
    except Exception as e:
        log.warning(f"Crossref title search failed: {e}")
        return None

def attach_pdf_verified(zot, parent_key, pdf_path, retries=1):
    """Attach pdf_path to Zotero item parent_key and verify it actually took.

    zot.attachment_simple() can report success (or raise nothing) even when
    no attachment ends up on the item -- this was silently producing Zotero
    entries with no PDF. This checks the file before upload (size + %PDF
    header) and the item's children afterward, retrying once, and always
    logs clearly instead of assuming success. Returns True only if a PDF
    attachment is confirmed present on the item afterward.
    """
    pdf_path = Path(pdf_path)
    try:
        size = pdf_path.stat().st_size
    except OSError:
        size = 0
    if size < 100:
        log.error(f"  FAIL: attachment skipped, file is empty/too small ({size} bytes): {pdf_path.name}")
        return False
    try:
        with open(pdf_path, "rb") as fh:
            header = fh.read(5)
    except OSError as e:
        log.error(f"  FAIL: could not read file to verify before attaching: {e}")
        return False
    if not header.startswith(b"%PDF"):
        log.error(f"  FAIL: attachment skipped, not a valid PDF (bad header {header!r}): {pdf_path.name}")
        return False

    def _pdf_child_keys():
        return {c["key"] for c in zot.children(parent_key)
                if c["data"].get("contentType") == "application/pdf"
                or c["data"].get("filename", "").lower().endswith(".pdf")}
    # Compare against what was already there: when attaching to an ORIGINAL item
    # that already has its clean PDF, "has a PDF" would be true even if this
    # upload failed.
    try:
        before = _pdf_child_keys()
    except Exception as e:
        log.error(f"  FAIL: could not read existing attachments before upload: {e}")
        before = set()

    for attempt in range(1, retries + 2):
        try:
            resp = zot.attachment_simple([str(pdf_path)], parent_key)
        except Exception as e:
            log.error(f"  FAIL: attachment_simple raised on attempt {attempt}: {e}")
            resp = None

        ok = False
        try:
            ok = bool(_pdf_child_keys() - before)
        except Exception as e:
            log.error(f"  FAIL: could not verify attachment via children(): {e}")

        if ok:
            return True
        log.warning(f"  Attachment attempt {attempt} for item {parent_key} did not stick (resp={resp})")

    log.error(f"  FAIL: could not attach {pdf_path.name} to Zotero item {parent_key} after {retries + 1} attempt(s) -- item exists with NO PDF, needs a manual fix.")
    return False

def create_zotero_item_from_metadata(zot, collection_key, pdf_path, meta):
    """Create a proper Zotero journal article item with full metadata."""
    pdf_path = Path(pdf_path)
    try:
        item_type = "journalArticle" if meta.get("journal") else "document"
        template = zot.item_template(item_type)

        template["title"] = meta.get("title") or pdf_path.stem
        template["collections"] = [collection_key]
        template["tags"] = [{"tag": "reMarkable-annotated"}]

        if meta.get("doi"):
            template["DOI"] = meta["doi"]
        if meta.get("year"):
            template["date"] = meta["year"]
        if meta.get("journal"):
            template["publicationTitle"] = meta["journal"]
        if meta.get("volume"):
            template["volume"] = meta["volume"]
        if meta.get("issue"):
            template["issue"] = meta["issue"]
        if meta.get("pages"):
            template["pages"] = meta["pages"]
        if meta.get("publisher"):
            template["publisher"] = meta["publisher"]

        # Authors
        creators = []
        for author in meta.get("authors", []):
            parts = author.rsplit(" ", 1)
            if len(parts) == 2:
                creators.append({"creatorType": "author", "firstName": parts[0], "lastName": parts[1]})
            else:
                creators.append({"creatorType": "author", "name": author})
        if creators:
            template["creators"] = creators

        resp = zot.create_items([template])
        if "successful" not in resp or "0" not in resp["successful"]:
            log.error(f"Failed to create item: {resp}")
            return False

        parent_key = resp["successful"]["0"]["key"]
        if attach_pdf_verified(zot, parent_key, pdf_path):
            log.info(f"✓ Imported with metadata: '{template['title'][:60]}' → {parent_key}")
        else:
            log.error(f"✗ Imported metadata-only (NO ATTACHMENT) for '{template['title'][:60]}' → {parent_key} — check this item in Zotero")
        return True

    except Exception as e:
        log.error(f"Error creating item with metadata: {e}")
        return False

# ============================================================
# BASIC ZOTERO IMPORT (fallback, no metadata)
# ============================================================
def import_pdf_to_zotero(pdf_path, zot, collection_key, tag="reMarkable-annotated", title=None):
    """Returns the new item's key on success (truthy), or None on failure.
    (Was previously bool; callers that only did `if import_pdf_to_zotero(...):`
    keep working unchanged since a key string is truthy and None is falsy --
    this just lets sync_remarkable_to_zotero grab the key for the transcription step.)
    title: defaults to the PDF's filename stem. The reMarkable path passes the
    document's reMarkable name instead, so items are titled 'Author - Title'
    rather than 'Author - Title-annotations' (geta's output filename)."""
    pdf_path = Path(pdf_path)
    log.info(f"Importing to Zotero: {pdf_path.name}")
    try:
        template = zot.item_template("document")
        template["title"] = title or pdf_path.stem
        template["collections"] = [collection_key]
        template["tags"] = [{"tag": tag}]
        resp = zot.create_items([template])
        if "successful" not in resp or "0" not in resp["successful"]:
            log.error(f"Failed to create item: {resp}"); return None
        parent_key = resp["successful"]["0"]["key"]
        if attach_pdf_verified(zot, parent_key, pdf_path):
            log.info(f"✓ Imported '{template['title']}' → {parent_key}")
        else:
            log.error(f"✗ Imported '{template['title']}' → {parent_key} but NO ATTACHMENT — check this item in Zotero")
        return parent_key
    except Exception as e:
        log.error(f"Error importing {pdf_path.name}: {e}"); return None

# ============================================================
# HANDWRITING TRANSCRIPTION + TAGS/CONNECTIONS (Claude)
# ============================================================
CLAUDE_MODEL      = "claude-sonnet-5-5"  # handwriting, and judging which works are in the same conversation
CLAUDE_FAST_MODEL = "claude-haiku-5-5"   # ~20x cheaper: picking tags from your list, reading reference lists

TRANSCRIBE_PROMPT = (
    "This image shows handwritten annotations cropped from one page of an annotated document "
    "(the printed original text has been stripped out -- only handwritten ink remains). "
    "Transcribe the handwritten content faithfully as clean plain text. Preserve line breaks "
    "and any numbered/bulleted/circled structure using plain characters (e.g. '1.' for a "
    "circled 1). If a word is genuinely ambiguous, give your best guess in square brackets "
    "with a question mark, e.g. [resilience?], rather than silently guessing or omitting it. "
    "If the page truly has no legible handwriting, respond with exactly: (no legible content). "
    "Output only the transcription itself -- no preamble, no commentary."
)

def crop_to_ink(png_bytes, ink_threshold=180, min_ink_pixels=80, pad=40):
    """Local, free: crop an annotations-only page image to the handwriting on it (plus a
    margin), so the vision model is sent a small strip instead of a mostly blank page.
    Returns PNG bytes, or None if the page has no handwriting. Pixels lighter than
    ink_threshold don't count as ink -- that covers blank paper and highlighter colours,
    so pages with only highlights are skipped rather than sent. Affects only what is sent
    for transcription; the annotated PDF attached in Zotero is a separate file."""
    try:
        from PIL import Image
    except ImportError:
        return png_bytes  # can't inspect locally -- send the whole page
    img = Image.open(io.BytesIO(png_bytes))
    mask = img.convert("L").point(lambda p: 255 if p < ink_threshold else 0)
    if mask.histogram()[255] < min_ink_pixels:
        return None
    l, t, r, b = mask.getbbox()
    box = (max(0, l - pad), max(0, t - pad), min(img.width, r + pad), min(img.height, b + pad))
    out = io.BytesIO()
    img.crop(box).save(out, "PNG")
    return out.getvalue()

def _import_fitz():
    try:
        import pymupdf as fitz  # PyMuPDF's current module name (1.24+)
    except ImportError:
        import fitz  # older PyMuPDF
    return fitz

def _esc(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

def first_pages_text(pdf_path, pages=2, limit=2500):
    """Printed text of the first pages (for papers whose Zotero record has no abstract). '' on failure."""
    try:
        fitz = _import_fitz()
        with fitz.open(str(pdf_path)) as doc:
            text = " ".join(doc[i].get_text() for i in range(min(pages, len(doc))))
        return re.sub(r"\s+", " ", text).strip()[:limit]
    except Exception:
        return ""

def transcribe_annotations(rmapi_path, remote_path, config, staging_dir):
    """Downloads an annotations-only render of remote_path and transcribes the pages that
    actually have handwriting. Returns [(page_number, text), ...] -- possibly empty.
    Never raises and writes nothing to Zotero."""
    api_key = config.get("anthropic_api_key")
    if not api_key:
        return []
    try:
        fitz = _import_fitz()
        import anthropic
    except ImportError as e:
        log.warning(f"  [transcribe] Missing dependency ({e}) -- run: pip install pymupdf pillow anthropic")
        return []

    staging_dir = Path(staging_dir)
    staging_dir.mkdir(exist_ok=True)
    for f in staging_dir.iterdir():
        try: f.unlink()
        except OSError: pass

    transcripts = []
    try:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        r = subprocess.run([rmapi_path, "geta", "--a", "--n", remote_path], cwd=str(staging_dir),
                            capture_output=True, text=True, creationflags=flags, timeout=120)
        if r.returncode != 0:
            log.info(f"  [transcribe] No annotations-only render available (likely no annotations yet): {r.stderr.strip() or r.stdout.strip()}")
            return []
        pdfs = [f for f in staging_dir.iterdir() if f.suffix.lower() == ".pdf"]
        if not pdfs:
            log.info("  [transcribe] geta --n produced no PDF -- skipping.")
            return []

        client = anthropic.Anthropic(api_key=api_key)
        with fitz.open(str(pdfs[0])) as doc:
            for i, page in enumerate(doc, start=1):
                png_bytes = crop_to_ink(page.get_pixmap(dpi=150).tobytes("png"))
                if png_bytes is None:
                    continue  # no handwriting on this page -- no API call, no cost
                try:
                    b64 = base64.b64encode(png_bytes).decode("ascii")
                    msg = client.messages.create(
                        model=CLAUDE_MODEL,
                        max_tokens=1024,
                        messages=[{"role": "user", "content": [
                            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                            {"type": "text", "text": TRANSCRIBE_PROMPT},
                        ]}],
                    )
                    text = "".join(getattr(b, "text", "") for b in msg.content).strip()
                except Exception as e:
                    log.warning(f"  [transcribe] API call failed on page {i}: {e}")
                    continue
                if text and text != "(no legible content)":
                    transcripts.append((i, text))
        if not transcripts:
            log.info("  [transcribe] No legible handwriting found.")
    except Exception as e:
        log.warning(f"  [transcribe] Unexpected error, skipping transcription for this item: {e}")
    finally:
        for f in staging_dir.iterdir():
            try: f.unlink()
            except OSError: pass
    return transcripts

# ---- tags + connections (only for papers returning to an existing item) ----
# Tags that describe what you're DOING with a paper rather than what it's ABOUT --
# never offered to the model and never added.
WORKFLOW_TAG_RE = re.compile(
    r"^(phil-|wildcard|buzz$|needs-|remarkable|ai-|_|#)|^(read|unread|to ?read|toread|reading|done|todo|starred)$"
    r"|\d{4}-\d{2}-\d{2}", re.I)
N_TAGS       = 3           # topical tags added to a returning paper
MAX_NEW_TAGS = 2           # of those, at most this many may be tags you've never used
                           # (after checking they aren't near-duplicates of tags you have)
N_CONVO      = 3           # "same conversation" Related links (works it cites are all linked)
_vocab_cache = {"at": 0.0, "tags": []}

def claude_text(config, prompt, max_tokens=1500, model=None):
    """One text request to Claude (Sonnet unless a model is given). If a cheaper model
    fails -- e.g. it has been retired -- retries once with Sonnet rather than skipping."""
    import anthropic
    client = anthropic.Anthropic(api_key=config["anthropic_api_key"])
    model = model or CLAUDE_MODEL
    try:
        msg = client.messages.create(model=model, max_tokens=max_tokens,
                                     messages=[{"role": "user", "content": prompt}])
    except Exception as e:
        if model == CLAUDE_MODEL:
            raise
        log.warning(f"  [enrich] {model} request failed ({e}); retrying with {CLAUDE_MODEL}")
        msg = client.messages.create(model=CLAUDE_MODEL, max_tokens=max_tokens,
                                     messages=[{"role": "user", "content": prompt}])
    return "".join(getattr(b, "text", "") for b in msg.content).strip()

def reply_lines(text):
    """Model replies are asked for one item per line (not JSON: a quotation mark inside a
    paper title broke a JSON reply once). Strips bullets/numbering and blank lines."""
    out = []
    for line in (text or "").splitlines():
        line = re.sub(r"^\s*(?:[-*\u2022]|\d+[.)])\s*", "", line).strip()
        if line and line.upper() != "NONE":
            out.append(line)
    return out

def _zotero_api(config, method, path, **kw):
    headers = {"Zotero-API-Key": config["zotero_api_key"], "Zotero-API-Version": "3", **kw.pop("headers", {})}
    timeout = kw.pop("timeout", 30)
    return requests.request(method, f"https://api.zotero.org/users/{config['zotero_library_id']}{path}",
                            headers=headers, timeout=timeout, **kw)

class ZoteroUnavailable(Exception):
    """Zotero couldn't be asked (timeout, outage) -- different from 'asked, and the answer is no'."""

# Zotero's quick search is usually fast (~2 s) but on this library has taken over 30 s,
# pyzotero's fixed limit. Searches that decide WHERE a document goes use this instead.
SEARCH_STOPWORDS = {"the", "and", "for", "with", "from", "into", "onto", "upon", "about", "what",
                    "when", "why", "how", "its", "their", "this", "that", "there", "case", "some"}

def search_words(words, n=5):
    """The distinctive words of a title (no 'a', 'of', 'the'...): faster and just as precise."""
    picked = [w for w in words if len(w) > 3 and w not in SEARCH_STOPWORDS][:n]
    return picked if len(picked) >= 2 else words[:6]

def zotero_quick_search(config, q, limit=15, attempts=2, timeout=60, qmode=None):
    """Top-level items matching a quick search. Raises ZoteroUnavailable if Zotero can't
    answer after the retries."""
    last = None
    params = {"q": q, "limit": limit, "format": "json", **({"qmode": qmode} if qmode else {})}
    for i in range(attempts):
        try:
            r = _zotero_api(config, "GET", "/items/top", params=params, timeout=timeout)
            if r.status_code == 200:
                backoff = r.headers.get("Backoff")          # Zotero asking clients to ease off
                if backoff and backoff.isdigit():
                    time.sleep(min(int(backoff), 30))
                return r.json()
            last = f"HTTP {r.status_code}"
            if r.status_code in (429, 503):                 # too many requests / busy: wait as told
                wait = r.headers.get("Retry-After", "5")
                time.sleep(min(int(wait) if wait.isdigit() else 5, 30))
                continue
        except Exception as e:
            last = e
        time.sleep(3 * (i + 1))
    raise ZoteroUnavailable(f"search '{q}' failed: {last}")

def zotero_get_item(zot, key):
    """The item, or None if it doesn't exist. Any other failure (timeout, outage) raises
    ZoteroUnavailable rather than being mistaken for 'doesn't exist'."""
    try:
        return zot.item(key)
    except Exception as e:
        if "404" in str(e) or "notfound" in type(e).__name__.lower():
            return None
        raise ZoteroUnavailable(str(e))

def tag_key(tag):
    """Comparison key for spotting near-duplicate tags: ignores case, accents,
    punctuation, simple plurals, little words (of/the/and...) and word order -- so
    'Epistemic Trust', 'trust, epistemic', 'epistemic-trust' and 'epistemic trusts'
    all collide, as do 'Theories of Truth' and 'truth theory'."""
    import unicodedata
    t = unicodedata.normalize("NFKD", tag).encode("ascii", "ignore").decode().lower()
    words = []
    for w in re.findall(r"[a-z0-9]+", t):
        if w in ("of", "the", "a", "an", "and", "in", "on", "for", "to"):
            continue
        if len(w) > 4 and w.endswith("ies"):
            w = w[:-3] + "y"
        elif len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "is", "us")):
            w = w[:-1]
        words.append(w)
    return " ".join(sorted(words))

TAG_CACHE_FILE = SCRIPT_DIR / "tag_cache.json"
TAG_FULL_REFRESH_DAYS = 7

def _fetch_tag_pages(config, since=None):
    """All tag rows (or only those changed since a library version), plus the library
    version they reflect."""
    rows, start, version = [], 0, None
    while True:
        params = {"limit": 100, "start": start, "format": "json", **({"since": since} if since else {})}
        r = _zotero_api(config, "GET", "/tags", params=params, timeout=60)
        r.raise_for_status()
        if version is None and str(r.headers.get("Last-Modified-Version", "")).isdigit():
            version = int(r.headers["Last-Modified-Version"])
        page = r.json()
        rows += page
        if len(page) < 100:
            break
        start += 100
    return rows, version

def _tag_row(t):
    meta = t.get("meta", {})
    return {"tag": t["tag"], "meta": {"type": meta.get("type", 0), "numItems": meta.get("numItems", 0)}}

def library_tag_rows(config):
    """Every tag in the library, from a copy saved on disk (tag_cache.json) and kept current
    by asking Zotero only for what changed since last time -- usually a single 'nothing
    changed' reply in well under a second, instead of downloading thousands of tags.
    A full download happens once a week (to refresh the how-often-used counts), when
    there's no saved copy, or if the update fails."""
    now = time.time()
    try:
        cache = json.loads(TAG_CACHE_FILE.read_text(encoding="utf-8"))
        assert isinstance(cache.get("rows"), list) and cache.get("version")
    except Exception:
        cache = None
    if cache and now - cache.get("full_at", 0) < TAG_FULL_REFRESH_DAYS * 86400:
        try:
            v = cache["version"]
            r = _zotero_api(config, "GET", "/tags", params={"limit": 1, "format": "json"},
                            headers={"If-Modified-Since-Version": str(v)}, timeout=30)
            if r.status_code == 304:
                return cache["rows"]                       # library unchanged since last time
            changed, new_v = _fetch_tag_pages(config, since=v)
            d = _zotero_api(config, "GET", "/deleted", params={"since": v, "format": "json"}, timeout=30)
            d.raise_for_status()
            gone = set(d.json().get("tags", []))
            rows = {(x["tag"], x["meta"]["type"]): x for x in cache["rows"] if x["tag"] not in gone}
            for t in changed:
                row = _tag_row(t)
                rows[(row["tag"], row["meta"]["type"])] = row
            cache.update(rows=list(rows.values()), version=new_v or v)
            _write_tag_cache(cache)
            log.info(f"  [tags] Saved tag list updated ({len(changed)} changed, {len(gone)} removed)")
            return cache["rows"]
        except Exception as e:
            log.warning(f"  [tags] Couldn't update the saved tag list ({e}); downloading it in full")
    rows, v = _fetch_tag_pages(config)
    cache = {"version": v, "full_at": now, "rows": [_tag_row(t) for t in rows]}
    if v:
        _write_tag_cache(cache)
    log.info(f"  [tags] Downloaded the full tag list ({len(rows)} tags)")
    return cache["rows"]

def _write_tag_cache(cache):
    try:
        tmp = TAG_CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        os.replace(tmp, TAG_CACHE_FILE)
    except Exception as e:
        log.warning(f"  [tags] Couldn't save the tag list ({e}); it will be downloaded again next time")

def library_tag_vocabulary(config, cap=5000):
    """ALL of your own (manually added) topical tags, most-used first. The cap only guards
    against an enormous list pushing the request into Haiku's higher price tier.
    Automatic (imported-keyword) tags are used only if you have very few manual ones."""
    if _vocab_cache["tags"] and time.time() - _vocab_cache["at"] < 300:
        return _vocab_cache["tags"]
    rows = library_tag_rows(config)
    def usable(t): return t["tag"] and len(t["tag"]) <= 60 and not WORKFLOW_TAG_RE.search(t["tag"])
    manual = [t for t in rows if t.get("meta", {}).get("type", 0) == 0 and usable(t)]
    if len(manual) < 30:
        manual += [t for t in rows if t.get("meta", {}).get("type") == 1 and usable(t)
                   and t.get("meta", {}).get("numItems", 0) >= 3]
    seen, vocab = set(), []
    for t in sorted(manual, key=lambda t: -t.get("meta", {}).get("numItems", 0)):
        if t["tag"].lower() not in seen:
            seen.add(t["tag"].lower()); vocab.append(t["tag"])
    _vocab_cache.update(at=time.time(), tags=vocab[:cap])
    return _vocab_cache["tags"]

def _describe(data, abstract_chars=300):
    names = [c.get("lastName") or c.get("name", "") for c in data.get("creators", [])][:3]
    year = (re.search(r"\b(\d{4})\b", data.get("date", "")) or [None, "n.d."])[1]
    s = f"{', '.join(n for n in names if n) or 'Unknown'} ({year}). {data.get('title', 'Untitled')}"
    if data.get("publicationTitle"):
        s += f". {data['publicationTitle']}"
    if abstract_chars and data.get("abstractNote"):
        s += f". Abstract: {data['abstractNote'][:abstract_chars]}"
    return s

def references_text(pdf_path, last_pages=12, limit=20000):
    """The paper's reference list: printed text from the last pages, cut at the last
    'References'/'Bibliography' heading if there is one. '' on failure."""
    try:
        fitz = _import_fitz()
        with fitz.open(str(pdf_path)) as doc:
            text = "\n".join(doc[i].get_text() for i in range(max(0, len(doc) - last_pages), len(doc)))
        heads = list(re.finditer(r"^\s*(references|bibliography|works cited|literature cited|notes and references)\s*$",
                                 text, re.I | re.M))
        if heads:
            text = text[heads[-1].start():]
        return text[:limit]
    except Exception:
        return ""

def _norm(s):
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]+", " ", s).split()

CITATION_LOOKUP_WORKERS = 4

def find_cited_in_library(zot, config, refs, item_key, max_lookups=60):
    """Ask Claude to read the reference list, then look each cited work up in the library.
    A match needs the first author's surname AND a close title match (and the same year
    when both have one). Returns {key: data}."""
    import difflib
    if not refs.strip():
        return {}
    reply = claude_text(config, (
        "Below is the reference list (or the last pages) of an academic paper. List every work "
        "it cites, one per line, in exactly this form and nothing else:\n"
        "Surname | Year | Title\n"
        "(first author's surname; leave Year empty if there is none). If there is no reference "
        "list, reply NONE.\n\n" + refs), max_tokens=6000, model=CLAUDE_FAST_MODEL)
    found = {}
    cited = []
    for line in reply_lines(reply):
        parts = [x.strip() for x in line.split("|")]
        if len(parts) >= 3:
            cited.append((parts[0], parts[1], " | ".join(parts[2:])))
    def lookup(c):
        c_surname, c_year, c_title = c
        surname, title = " ".join(_norm(c_surname)), _norm(c_title)
        year = (re.search(r"\d{4}", c_year) or [""])[0]
        if not surname or len(title) < 2:
            return None
        try:
            hits = zotero_quick_search(config, " ".join(search_words(title)), limit=8, attempts=2, timeout=30)
        except Exception:
            return None
        for h in hits:
            d = h["data"]
            if h["key"] == item_key or d.get("itemType") in ("note", "attachment", "annotation"):
                continue
            names = {" ".join(_norm(cr.get("lastName") or cr.get("name", ""))) for cr in d.get("creators", [])}
            hy = (re.search(r"\b(\d{4})\b", d.get("date", "")) or [None, ""])[1]
            sim = difflib.SequenceMatcher(None, " ".join(title), " ".join(_norm(d.get("title", "")))).ratio()
            if (any(surname == n or surname in n.split() for n in names) and sim >= 0.75
                    and (not year or not hy or year == hy)):
                return h["key"], d
        return None

    # Four lookups at a time: ~4x faster, still gentle on Zotero (and it backs off if asked).
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=CITATION_LOOKUP_WORKERS) as pool:
        for res in pool.map(lookup, cited[:max_lookups]):   # results come back in citation order
            if res:
                found.setdefault(res[0], res[1])
    return found

# Tablet documents sent before item IDs were added to names look like
# "Author - Year - Title" (or "Author - Title"); a " (2)" may have been added in Imported.
LEGACY_NAME_RE = re.compile(r"^(?P<author>[^-]+?)\s+-\s+(?:(?P<year>\d{4}|n\.d\.)\s+-\s+)?(?P<title>.+?)(?:\s\(\d+\))?$")

def find_original_by_name(zot, name, search=None):
    """For a returned document WITHOUT an [ITEMKEY] in its name: find the original Zotero
    item from an 'Author - Year - Title' name. Only a confident, unique match counts --
    first author's surname, same year (if the name has one), and the title matching
    word for word (names were sometimes cut short and had ?:/ etc. replaced). Items this
    script itself created from reMarkable are never treated as originals. Returns the
    item key, or None (then the document comes in as a new item, as before).
    Raises if the library couldn't be searched."""
    import difflib
    m = LEGACY_NAME_RE.match(name.strip())
    if not m:
        return None
    author = " ".join(_norm(m.group("author")))
    year = m.group("year") if m.group("year") and m.group("year") != "n.d." else ""
    title = _norm(m.group("title"))
    if not author or author == "unknown" or not title:
        return None
    # A failed search raises (ZoteroUnavailable): the caller retries later rather than
    # treating "couldn't look" as "no match" and creating a new item.
    search = search or (lambda q: zot.top(q=q, limit=15))
    hits = search(" ".join(search_words(title)))
    matches = []
    for h in hits:
        d = h["data"]
        if d.get("itemType") in ("note", "attachment", "annotation") or d.get("deleted"):
            continue
        if any(t.get("tag") == "reMarkable-import" for t in d.get("tags", [])):
            continue
        names = {" ".join(_norm(c.get("lastName") or c.get("name", ""))) for c in d.get("creators", [])}
        if not any(author == n or author in n.split() for n in names):
            continue
        hy = (re.search(r"\b(\d{4})\b", d.get("date", "")) or [None, ""])[1]
        if year and hy and year != hy:
            continue
        ct = _norm(d.get("title", ""))
        head = ct[:len(title)]
        if len(title) < 3 and ct != title:   # very short titles ("Abilities") must match in full
            continue
        if head == title or (len(title) >= 4 and difflib.SequenceMatcher(None, " ".join(title), " ".join(head)).ratio() >= 0.92):
            matches.append(h["key"])
    return matches[0] if len(matches) == 1 else None

def add_related(zot, config, src_key, dst_keys):
    """Add Zotero 'Related' links from src_key to each of dst_keys (one edit of src_key).
    Returns the keys actually linked. Retries once if the item changed underneath us."""
    lib = f"http://zotero.org/users/{config['zotero_library_id']}/items/"
    for attempt in range(2):
        fresh = zot.item(src_key)
        rels = dict(fresh["data"].get("relations") or {})
        cur = rels.get("dc:relation", [])
        cur = [cur] if isinstance(cur, str) else list(cur)
        todo = [k for k in dst_keys if lib + k not in cur]
        if not todo:
            return list(dst_keys)
        rels["dc:relation"] = cur + [lib + k for k in todo]
        r = _zotero_api(config, "PATCH", f"/items/{src_key}", json={"relations": rels},
                        headers={"If-Unmodified-Since-Version": str(fresh["version"])})
        if r.status_code in (200, 204):
            return list(dst_keys)
        if r.status_code != 412:
            break
    log.warning(f"  [enrich] Could not add Related links on {src_key}")
    return []

def enrich_returned_item(zot, config, item_key, transcripts, printed_text="", refs=""):
    """For a paper returning to an existing item:
      * links it as Related, both ways, to (a) works in your library that it cites and
        (b) up to 3 works in the same conversation -- including ones your notes discuss;
      * adds 3 topical tags, chosen from ALL your own tags, with the tags you put on the
        works it cites as a hint; up to 2 may be new, after a near-duplicate check.
    Cost-conscious: the mechanical steps (reading the reference list, choosing tags) use
    Haiku; only the judgment of "same conversation" uses Sonnet.
    Returns a summary for the note: {"added", "new", "linked": [(key, label, why)]}, or
    None. Never raises."""
    if not config.get("anthropic_api_key"):
        return None
    try:
        item = zot.item(item_key)
        data = item["data"]
        vocab = library_tag_vocabulary(config)
        vocab_by_key = {}
        for t in vocab:                      # most-used spelling wins
            vocab_by_key.setdefault(tag_key(t), t)
        notes = "\n".join(f"[p.{p}] {t}" for p, t in transcripts)[:3000]
        paper = _describe(data, abstract_chars=1500)

        # 1) works it cites that are in your library (a failure here doesn't stop the rest)
        try:
            cited = find_cited_in_library(zot, config, refs, item_key) if refs else {}
        except Exception as e:
            log.warning(f"  [enrich] Couldn't read the reference list ({e}); continuing without cited works")
            cited = {}

        # 2) how you tagged those works -- the best evidence of your own practice
        neighbour = {}
        for d in cited.values():
            for t in d.get("tags", []):
                if t.get("type", 0) == 0 and not WORKFLOW_TAG_RE.search(t["tag"]):
                    neighbour[t["tag"]] = neighbour.get(t["tag"], 0) + 1
        neighbour_lines = [f"{t} ({n})" for t, n in sorted(neighbour.items(), key=lambda x: -x[1])[:25]]

        # 3) tags + search queries
        try:
            reply = reply_lines(claude_text(config, (
                "You are tagging a paper in a philosopher's Zotero library.\n\n"
                f"PAPER: {paper}\n\n"
                + (f"FIRST PAGES (printed text): {printed_text}\n\n" if printed_text else "")
                + (f"THE READER'S HANDWRITTEN NOTES ON IT:\n{notes}\n\n" if notes else "")
                + f"TAGS ALREADY ON THE ITEM: {', '.join(t['tag'] for t in data.get('tags', [])) or '(none)'}\n\n"
                + ("TAGS THIS READER PUT ON WORKS THIS PAPER CITES (tag (how many of those works)):\n"
                   + "\n".join(neighbour_lines) + "\n\n" if neighbour_lines else "")
                + "THE READER'S FULL TAG VOCABULARY (most used first):\n" + "\n".join(vocab) + "\n\n"
                f"Choose the {N_TAGS} best topical tags for this paper, best first, about what it is about "
                "(debates, concepts, figures, methods), not about the reader's workflow. Prefer tags from "
                "the vocabulary, spelled exactly as listed -- look through the whole list, including rarely "
                "used tags, and treat the tags on cited works as a guide to how this reader tags. Propose a "
                f"new tag only where no existing tag fits (at most {MAX_NEW_TAGS}), in the same style as "
                "the vocabulary. Weight what the reader's notes focus on. Also give 4 short library search "
                "queries (2-4 words each) for finding works this paper is in conversation with.\n"
                "Reply in exactly this form and nothing else -- one line per tag, best first, then one "
                "line per query:\nTAG: <tag>\nQUERY: <search query>"), model=CLAUDE_FAST_MODEL))
        except Exception as e:
            log.warning(f"  [enrich] Couldn't choose tags ({e}); continuing without new tags")
            reply = []
        proposed = [l.split(":", 1)[1].strip().strip('"') for l in reply if l.upper().startswith("TAG:")]
        queries = [l.split(":", 1)[1].strip().strip('"') for l in reply if l.upper().startswith("QUERY:")]

        existing_keys = {tag_key(t["tag"]) for t in data.get("tags", [])}
        chosen, new, chosen_keys = [], [], set()
        for t in proposed[:N_TAGS + 3]:
            if len(chosen) == N_TAGS:
                break
            t = str(t).strip()
            k = tag_key(t)
            if not t or not k or WORKFLOW_TAG_RE.search(t) or k in existing_keys or k in chosen_keys:
                continue
            if k in vocab_by_key:                 # existing tag, or a near-duplicate of one
                chosen.append(vocab_by_key[k])
            elif len(new) < MAX_NEW_TAGS:
                chosen.append(t); new.append(t)
            else:
                continue
            chosen_keys.add(k)

        if chosen:
            for attempt in range(2):
                fresh = zot.item(item_key)  # latest version, in case it was just edited
                have = {tag_key(x["tag"]) for x in fresh["data"].get("tags", [])}
                tags = fresh["data"].get("tags", []) + [{"tag": t} for t in chosen if tag_key(t) not in have]
                r = _zotero_api(config, "PATCH", f"/items/{item_key}", json={"tags": tags},
                                headers={"If-Unmodified-Since-Version": str(fresh["version"])})
                if r.status_code in (200, 204):
                    log.info(f"  [enrich] Tagged: {', '.join(chosen)}" + (f"  (new: {', '.join(new)})" if new else ""))
                    break
                if r.status_code != 412 or attempt:
                    log.warning(f"  [enrich] Could not add tags (HTTP {r.status_code})"); chosen, new = [], []
                    break

        # 4) same conversation: candidates sharing the chosen tags, or matching searches
        #    across everything (incl. your notes and full text), ranked by Claude
        cands = {}
        def collect(found):
            for it in found:
                if (it["key"] != item_key and it["key"] not in cited
                        and it["data"].get("itemType") not in ("note", "attachment", "annotation")):
                    cands.setdefault(it["key"], it["data"])
        for t in chosen:
            if t not in new:
                try: collect(zot.top(tag=t, limit=20))
                except Exception: pass
        for q in queries[:4]:
            try: collect(zotero_quick_search(config, q, limit=15, attempts=1, qmode="everything"))
            except Exception: pass
        convo = []
        if cands:
            listing = "\n".join(f"{k}: {_describe(d)}" for k, d in list(cands.items())[:60])
            try:
                pick = reply_lines(claude_text(config, (
                f"PAPER: {paper}\n\n"
                + (f"THE READER'S HANDWRITTEN NOTES ON IT:\n{notes}\n\n" if notes else "")
                + "CANDIDATES FROM THE READER'S LIBRARY (key: description):\n" + listing + "\n\n"
                f"Pick up to {N_CONVO} candidates -- the strongest -- that are in the same conversation as the paper: the same "
                "debate, a position it argues for or against, an author it engages, or a direct "
                "application -- weighting what the reader's notes focus on. Sharing a word is not "
                "enough; return fewer, or none, if fewer are really connected. For each, one "
                "specific sentence on the connection.\n"
                "Reply with one line per pick in exactly this form and nothing else (or NONE):\n"
                "KEY | one sentence on the connection"), max_tokens=1200))
            except Exception as e:
                log.warning(f"  [enrich] Couldn't rank same-conversation works ({e}); skipping those links")
                pick = []
            for line in pick:
                k, _, reason = line.partition("|")
                k = k.strip().strip("[]`*")
                if k in cands and k not in dict(convo) and len(convo) < N_CONVO:
                    convo.append((k, reason.strip()))

        # 5) link them in Zotero, both directions
        targets = list(cited) + [k for k, _ in convo]
        linked = []
        if targets and add_related(zot, config, item_key, targets):
            for k in targets:
                add_related(zot, config, k, [item_key])   # reciprocal link on the other item
            reasons = dict(convo)
            for k in targets:
                d = cited.get(k) or cands.get(k)
                linked.append((k, _describe(d, abstract_chars=0), "cited in this paper" if k in cited else reasons[k]))
            log.info(f"  [enrich] Linked as Related: {len(cited)} cited, {len(convo)} same-conversation")
        return {"added": chosen, "new": new, "linked": linked}
    except Exception as e:
        log.warning(f"  [enrich] Skipped tags/links for {item_key}: {e}")
        return None

def build_note_html(transcripts, extra, heading=None):
    has_extra = bool(extra and (extra["added"] or extra["linked"]))
    html = (f"<p><strong>{heading or 'From reMarkable'}, {date.today().isoformat()}"
            f" (AI-generated, verify before citing)</strong></p>")
    if transcripts:
        html += "<p><strong>Handwritten notes</strong></p>"
        for page_num, text in transcripts:
            html += f"<p><em>Page {page_num}:</em><br/>{_esc(text).replace(chr(10), '<br/>')}</p>"
    if has_extra and extra["added"]:
        labels = [_esc(t) + (" (new tag)" if t in extra["new"] else "") for t in extra["added"]]
        html += f"<p><strong>Tags added</strong>: {', '.join(labels)}</p>"
    if has_extra and extra["linked"]:
        html += "<p><strong>Linked as Related</strong> (remove a link in the item's Related pane if it's wrong)</p><ul>"
        for k, label, why in extra["linked"]:
            html += (f'<li><a href="zotero://select/library/items/{k}">{_esc(label)}</a>'
                     f" -- {_esc(why)}</li>")
        html += "</ul>"
    return html

def transcribe_annotations_to_note(zot, rmapi_path, remote_path, parent_key, config, staging_dir,
                                   enrich=False, printed_text="", refs=""):
    """Transcribes the handwriting, optionally adds tags + possible connections (papers
    returning to an existing item), and writes one child note summarizing it all.
    Never raises -- a failure here never breaks the import itself."""
    transcripts = transcribe_annotations(rmapi_path, remote_path, config, staging_dir)
    extra = enrich_returned_item(zot, config, parent_key, transcripts, printed_text, refs) if enrich else None
    has_extra = bool(extra and (extra["added"] or extra["linked"]))
    if not transcripts and not has_extra:
        return
    try:
        zot.create_items([{"itemType": "note", "note": build_note_html(transcripts, extra), "parentItem": parent_key,
                           "tags": [{"tag": "AI-transcribed"}]}])
        log.info(f"  [note] ✓ Added note ({len(transcripts)} page(s) of handwriting"
                 + (f", {len(extra['added'])} tag(s), {len(extra['linked'])} Related link(s)" if has_extra else "")
                 + ")")
    except Exception as e:
        log.warning(f"  [note] Could not create note on {parent_key}: {e}")

def transcripts_from_notes(zot, item_key):
    """Recover [(page, text)] from the item's existing AI-transcribed notes."""
    import html as htmllib
    out = []
    for c in zot.children(item_key):
        d = c["data"]
        if d.get("itemType") != "note" or "AI-transcribed" not in [t["tag"] for t in d.get("tags", [])]:
            continue
        for m in re.finditer(r"<em>Page (\d+):</em><br/>(.*?)</p>", d.get("note", ""), re.S):
            out.append((int(m.group(1)), htmllib.unescape(m.group(2).replace("<br/>", "\n"))))
    return out

def enrich_existing_item(zot, config, item_key):
    """Run just the tags + Related-links step for an item already in Zotero, using its
    (newest annotated) PDF and its existing transcription notes. Command line:
        python zotero_remarkable_sync.py --enrich ITEMKEY
    Safe to run while the watcher is running (uses its own temporary folder)."""
    import tempfile
    item = zotero_get_item(zot, item_key)
    if not item:
        log.error(f"No Zotero item {item_key}"); return False
    pdfs = [c for c in zot.children(item_key) if _is_pdf_attachment(c)]
    pdfs.sort(key=lambda c: ("(annotated" in (c["data"].get("filename") or c["data"].get("title", "")),
                             c["data"].get("dateAdded", "")), reverse=True)
    printed, refs = "", ""
    if pdfs:
        tmp = Path(tempfile.mkdtemp(prefix="zrs_enrich_"))
        try:
            pdf = tmp / "paper.pdf"
            pdf.write_bytes(zot.file(pdfs[0]["key"]))
            printed, refs = first_pages_text(pdf), references_text(pdf)
        except Exception as e:
            log.warning(f"Couldn't read the PDF ({e}); tagging from the item's details only")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    transcripts = transcripts_from_notes(zot, item_key)
    log.info(f"Enriching {item_key}: PDF {'found' if pdfs else 'not found'}, reference list "
             f"{'found' if refs.strip() else 'not found'}, {len(transcripts)} page(s) of your notes")
    extra = enrich_returned_item(zot, config, item_key, transcripts, printed, refs)
    if extra and (extra["added"] or extra["linked"]):
        zot.create_items([{"itemType": "note", "parentItem": item_key, "tags": [{"tag": "AI-transcribed"}],
                           "note": build_note_html([], extra, heading="Tags and links")}])
        log.info(f"✓ Done: {len(extra['added'])} tag(s), {len(extra['linked'])} Related link(s); see the new note on the item")
        return True
    log.warning("Nothing was added (see messages above)")
    return False

def smart_import_pdf(pdf_path, zot, collection_key):
    """Import PDF with automatic DOI/metadata lookup."""
    pdf_path = Path(pdf_path)
    log.info(f"[OneDrive] Attempting metadata lookup for: {pdf_path.name}")

    # Try DOI from PDF content
    doi = extract_doi_from_pdf(pdf_path)
    if doi:
        log.info(f"[OneDrive] Found DOI: {doi}")
        meta = lookup_crossref(doi)
        if meta:
            log.info(f"[OneDrive] Crossref match: {meta['title'][:60]}")
            return create_zotero_item_from_metadata(zot, collection_key, pdf_path, meta)

    # Try title search
    title = pdf_path.stem
    # Clean up common filename patterns like "Author - Year - Title"
    title_clean = re.sub(r'^[^-]+-\s*\d{4}\s*-\s*', '', title).strip()
    if len(title_clean) > 10:
        log.info(f"[OneDrive] No DOI found, trying title search: {title_clean[:50]}")
        meta = search_crossref_by_title(title_clean)
        if meta:
            log.info(f"[OneDrive] Crossref title match: {meta['title'][:60]}")
            return create_zotero_item_from_metadata(zot, collection_key, pdf_path, meta)

    # Fallback: import without metadata
    log.info(f"[OneDrive] No metadata found, importing as plain document")
    return import_pdf_to_zotero(pdf_path, zot, collection_key, tag="reMarkable-annotated")

# ============================================================
# RMDOC EXTRACTION
# ============================================================
def extract_pdf_from_rmdoc(rmdoc_path, output_pdf_path):
    try:
        with zipfile.ZipFile(rmdoc_path, "r") as z:
            pdf_names = [n for n in z.namelist() if n.endswith(".pdf")]
            if not pdf_names: return False
            with open(output_pdf_path, "wb") as f:
                f.write(z.read(pdf_names[0]))
        return True
    except Exception as e:
        log.error(f"Failed to extract PDF from rmdoc: {e}"); return False

# ============================================================
# reMarkable FOLDERS + claiming work across two machines
# ============================================================
# Two folders on the tablet, one per direction (see module docstring). When two
# computers run this script, each piece of work is CLAIMED before it is done, using
# an operation only one machine can win:
#   * reMarkable -> Zotero: the document is moved out of the inbox into Imported
#     first; the machine whose move succeeds processes it, the other finds it gone.
#   * Zotero -> reMarkable: the item is removed from Send Remarkable first; Zotero
#     accepts that from only one machine (the request carries the item's version),
#     the other is refused and skips it.
# If processing then fails, the claim is undone so the work is retried.
IMPORTED_SUBFOLDER  = "Imported"
DEFAULT_SEND_FOLDER = "/From Zotero"
# "... [7XK2PQ8M]", also allowing the " (2)" a name collision in Imported adds
KEY_SUFFIX_RE       = re.compile(r"\s\[([A-Z0-9]{8})\](?:\s\(\d+\))?$")

def ensure_rm_folder(rmapi_path, path, parent_entries=None):
    """Make sure folder 'path' exists on reMarkable. parent_entries (a listing of its
    parent we already have) saves an API call when supplied."""
    parent, name = posixpath.split(path.rstrip("/"))
    parent = parent or "/"
    entries = parent_entries if parent_entries is not None else rmapi_ls(rmapi_path, parent)
    if any(t == "d" and n == name for t, n in entries):
        return True
    ok, out, err = rmapi_run(rmapi_path, ["mkdir", path])
    if ok:
        log.info(f"[reMarkable] Created folder {path}")
        return True
    # mkdir may fail simply because it already exists (e.g. the other machine just made it)
    if any(t == "d" and n == name for t, n in rmapi_ls(rmapi_path, parent)):
        return True
    log.warning(f"[reMarkable] Could not create {path}: {err or out}")
    return False

def _rm_glob_safe(name):
    """rmapi's mv treats its SOURCE argument as a glob pattern, so the '[ABCD2345]' in
    our filenames was read as a character class and matched nothing. Replace every
    glob metacharacter with '?', which matches exactly one character -- including the
    literal bracket -- under any glob dialect, without relying on escape syntax."""
    return re.sub(r"[\[\]*?\\]", "?", name)

def move_rm(rmapi_path, src_folder, filename, dest_folder, quiet_if_gone=False):
    """Move '<src_folder>/<filename>' into dest_folder (adding ' (2)', ' (3)'... if that
    name is taken there). In the usual case the destination is just the folder, so no
    bracketed name has to be passed as a destination; only on a name collision is a
    full renamed path given. Verifies the document actually left src_folder. Returns
    the new remote path, or None if it didn't move. Never raises.
    quiet_if_gone: don't warn when the move fails because the document is no longer
    there (used for claims, where that just means the other machine took it)."""
    try:
        taken = {n for t, n in rmapi_ls(rmapi_path, dest_folder) if t == "f"}
        target, i = filename, 2
        while target in taken:
            target = f"{filename} ({i})"; i += 1
        dest_arg = dest_folder if target == filename else f"{dest_folder}/{target}"
        ok, out, err = rmapi_run(rmapi_path, ["mv", f"{src_folder}/{_rm_glob_safe(filename)}", dest_arg])
        still_there = any(t == "f" and n == filename for t, n in rmapi_ls(rmapi_path, src_folder))
        if not ok:
            if not (quiet_if_gone and not still_there):
                log.warning(f"  [reMarkable] Could not move '{filename}' to {dest_folder}: {err or out}")
            return None
        if still_there:
            log.warning(f"  [reMarkable] mv reported success but '{filename}' is still in {src_folder}")
            return None
        log.info(f"  [reMarkable] Moved to {dest_folder}: {target}")
        return f"{dest_folder}/{target}"
    except Exception as e:
        log.warning(f"  [reMarkable] Unexpected error moving '{filename}': {e}")
        return None

def _download_annotated_pdf(rmapi_path, remote_path, filename):
    """geta --a into STAGING_DIR; returns the local PDF path or None."""
    ok, _, stderr = rmapi_get(rmapi_path, remote_path, STAGING_DIR)
    if not ok:
        log.error(f"[reMarkable→Zotero] Download failed for '{filename}': {stderr}")
        return None
    local_file = rmdoc_file = None
    for f in STAGING_DIR.iterdir():
        if f.suffix.lower() == ".pdf": local_file = f; break
        elif f.suffix.lower() in (".rmdoc", ".zip"): rmdoc_file = f
    if not local_file and rmdoc_file:
        pdf_out = rmdoc_file.with_suffix(".pdf")
        if extract_pdf_from_rmdoc(rmdoc_file, pdf_out): local_file = pdf_out
    if not local_file or not local_file.exists():
        log.warning(f"[reMarkable→Zotero] No PDF produced for: {filename}")
        return None
    return local_file

def _clear_staging():
    for f in STAGING_DIR.iterdir():
        try: f.unlink()
        except OSError: pass

def _is_pdf_attachment(child):
    d = child["data"]
    return d.get("itemType") == "attachment" and (
        d.get("contentType") == "application/pdf" or d.get("filename", "").lower().endswith(".pdf"))

def trash_superseded_pdfs(zot, config, item_key, keep_key):
    """After the annotated copy is attached, move the item's OTHER PDF attachments to
    Zotero's trash, leaving one authoritative PDF. The trash keeps them for 30 days, so
    this is recoverable. A PDF carrying highlights or notes made in Zotero's own reader
    is kept, because trashing the PDF would take that work with it. Never raises."""
    try:
        if keep_key is None:
            log.warning(f"  [cleanup] Couldn't identify the new annotated PDF on {item_key}; not trashing anything")
            return
        for c in zot.children(item_key):
            if c["key"] == keep_key or not _is_pdf_attachment(c):
                continue
            name = c["data"].get("filename") or c["data"].get("title") or c["key"]
            try:
                has_zotero_notes = any(a["data"].get("itemType") == "annotation" for a in zot.children(c["key"]))
            except Exception:
                has_zotero_notes = True  # can't tell -> keep it, to be safe
            if has_zotero_notes:
                log.info(f"  [cleanup] Kept '{name}': it has highlights/notes made in Zotero's reader")
                continue
            r = requests.patch(
                f"https://api.zotero.org/users/{config['zotero_library_id']}/items/{c['key']}",
                json={"deleted": 1}, timeout=20,
                headers={"Zotero-API-Key": config["zotero_api_key"], "Zotero-API-Version": "3",
                         "If-Unmodified-Since-Version": str(c["version"])})
            if r.status_code in (200, 204):
                log.info(f"  [cleanup] Moved superseded PDF to Zotero trash: {name}")
            else:
                log.warning(f"  [cleanup] Could not trash '{name}' (HTTP {r.status_code}); left in place")
    except Exception as e:
        log.warning(f"  [cleanup] Skipped trashing old PDFs on {item_key}: {e}")

def attach_annotated_to_original(zot, config, item_key, local_file, display):
    """Attach the annotated PDF to the original item and trash the PDFs it supersedes.
    Returns True on success."""
    named = local_file.with_name(sanitize(f"{display} (annotated {date.today().isoformat()})") + ".pdf")
    if named != local_file:
        local_file.replace(named)
    try:
        before = {c["key"] for c in zot.children(item_key) if _is_pdf_attachment(c)}
    except Exception:
        before = None
    if not attach_pdf_verified(zot, item_key, named):
        return False
    log.info(f"✓ Attached annotated PDF to original item '{display}' → {item_key}")
    new_key = None
    if before is not None:
        added = [c["key"] for c in zot.children(item_key) if _is_pdf_attachment(c) and c["key"] not in before]
        new_key = added[0] if len(added) == 1 else None
    trash_superseded_pdfs(zot, config, item_key, new_key)
    return True

def _migrate_state_v2(rmapi_path, state, files, rm_folder, archive, can_archive, send_folder):
    """One-time move from the old single-folder layout, where Zotero sent PDFs into
    the inbox and a per-machine list of names kept them from being imported back.
    Old reading copies still in the inbox go to the send folder; anything already
    imported goes to the archive. After this, synced_to_zotero only holds documents
    that were imported but could not be archived (so they're retried, not re-imported)."""
    sent = {Path(v).stem for v in state["synced_from_zotero"].values() if v and v != "(no PDF)"}
    pending = []
    for filename in list(files):
        if filename not in state["synced_to_zotero"]:
            continue
        files.remove(filename)
        if filename in sent:
            if ensure_rm_folder(rmapi_path, send_folder) and move_rm(rmapi_path, rm_folder, filename, send_folder):
                continue
        elif can_archive and move_rm(rmapi_path, rm_folder, filename, archive):
            continue
        pending.append(filename)
    state["synced_to_zotero"] = pending
    state["state_version"] = 2
    log.info("[reMarkable→Zotero] Migrated to the two-folder layout"
             + (f" ({len(pending)} document(s) still to archive)" if pending else ""))

# ============================================================
# SYNC: reMarkable -> Zotero
# ============================================================
def sync_remarkable_to_zotero(zot, config, state, rmapi_path):
    rm_folder      = config["remarkable_folder"]
    send_folder    = config.get("remarkable_send_folder", DEFAULT_SEND_FOLDER)
    collection_key = config["zotero_import_collection"]
    archive        = f"{rm_folder}/{IMPORTED_SUBFOLDER}"
    entries        = rmapi_ls(rmapi_path, rm_folder)
    files          = [name for (ftype, name) in entries if ftype == "f"]
    can_archive    = ensure_rm_folder(rmapi_path, archive, entries) if files else True

    if state.get("state_version", 1) < 2:
        _migrate_state_v2(rmapi_path, state, files, rm_folder, archive, can_archive, send_folder)

    if not files:
        log.info("[reMarkable→Zotero] No files in reMarkable folder.")
        return
    if not can_archive:
        log.warning(f"[reMarkable→Zotero] {archive} unavailable; will retry next cycle"); return

    new_count = 0
    STAGING_DIR.mkdir(exist_ok=True)

    for filename in files:
        # Brought in earlier but the archive move failed back then -- just finish the move.
        if filename in state["synced_to_zotero"]:
            if move_rm(rmapi_path, rm_folder, filename, archive):
                state["synced_to_zotero"].remove(filename)
            continue

        # Claim it by moving it to Imported first. If the other machine got there
        # first the document is simply gone and we skip it.
        claimed = move_rm(rmapi_path, rm_folder, filename, archive, quiet_if_gone=True)
        if not claimed:
            if not any(t == "f" and n == filename for t, n in rmapi_ls(rmapi_path, rm_folder)):
                log.info(f"[reMarkable→Zotero] Being handled by the other machine: {filename}")
            continue
        claimed_name = posixpath.basename(claimed)

        m = KEY_SUFFIX_RE.search(filename)
        display = KEY_SUFFIX_RE.sub("", filename) if m else filename
        original_key = None
        try:
            if m:
                orig = zotero_get_item(zot, m.group(1))
                if orig and not orig["data"].get("deleted"):
                    original_key = m.group(1)
                else:
                    log.warning(f"[reMarkable→Zotero] '{filename}' names Zotero item {m.group(1)}, which "
                                f"doesn't exist or is in the trash -- bringing it in as a new item instead.")
            else:
                # No item ID in the name (sent to the tablet before IDs were added): try to
                # recognise the original from an 'Author - Year - Title' name.
                original_key = find_original_by_name(zot, filename,
                                                     search=lambda q: zotero_quick_search(config, q))
                if original_key:
                    display = re.sub(r"\s\(\d+\)$", "", filename)
                    log.info(f"[reMarkable→Zotero] No item ID in the name, but it matches existing item {original_key}")
                else:
                    log.info(f"[reMarkable→Zotero] No item ID and no single matching item in the library -- new item")
        except Exception as e:
            # Couldn't ask Zotero which item this belongs to. Don't guess "new item":
            # hand the document back and try again next cycle.
            log.warning(f"[reMarkable→Zotero] Couldn't check Zotero for the original of '{filename}' ({e}); "
                        f"putting it back to retry next cycle")
            if not move_rm(rmapi_path, archive, claimed_name, rm_folder):
                log.error(f"[reMarkable→Zotero] Could not put '{claimed_name}' back -- move it from "
                          f"{archive} to {rm_folder} on the tablet to retry")
            continue

        log.info(f"[reMarkable→Zotero] Found: {filename}"
                 + (f"  (returning to original item {original_key})" if original_key else ""))
        try:
            target_key = None
            printed, refs = "", ""
            local_file = _download_annotated_pdf(rmapi_path, claimed, filename)
            if local_file:
                if original_key:
                    printed, refs = first_pages_text(local_file), references_text(local_file)
                    if attach_annotated_to_original(zot, config, original_key, local_file, display):
                        target_key = original_key
                    else:
                        log.error(f"✗ Could not attach annotated PDF to {original_key}")
                else:
                    target_key = import_pdf_to_zotero(local_file, zot, collection_key,
                                                      tag="reMarkable-import", title=display)
            if not target_key:
                # Undo the claim so it is retried next cycle.
                if move_rm(rmapi_path, archive, claimed_name, rm_folder):
                    log.warning(f"[reMarkable→Zotero] Put '{filename}' back in {rm_folder} to retry")
                else:
                    log.error(f"[reMarkable→Zotero] '{claimed_name}' was not brought in and could not be "
                              f"put back -- move it from {archive} to {rm_folder} on the tablet to retry")
                continue
            new_count += 1
            # Papers returning to an existing item also get tags + Related links.
            transcribe_annotations_to_note(zot, rmapi_path, claimed, target_key, config, STAGING_DIR,
                                           enrich=bool(original_key), printed_text=printed, refs=refs)
        finally:
            _clear_staging()

    if new_count:
        log.info(f"[reMarkable→Zotero] Brought in {new_count} document(s)")

# ============================================================
# WORD -> PDF (for items in Send Remarkable that only have a Word file)
# ============================================================
WORD_EXTENSIONS = (".docx", ".doc", ".docm", ".rtf", ".odt")
WORD_CONTENT_TYPES = ("application/msword",
                      "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                      "application/vnd.ms-word.document.macroenabled.12",
                      "application/rtf", "text/rtf", "application/vnd.oasis.opendocument.text")
WORD_TIMEOUT_SECONDS = 180

# Runs in PowerShell (built into Windows), which drives Microsoft Word: no extra Python
# packages needed. Word runs hidden as its own separate copy, never shows dialogs,
# never runs macros, and only reads the document.
_WORD_TO_PDF_PS = r"""
$ErrorActionPreference = 'Stop'
$src = $env:ZRS_WORD_SRC; $dst = $env:ZRS_WORD_DST
$word = New-Object -ComObject Word.Application
$ours = ($word.Documents.Count -eq 0)   # if this is a Word you already have open, leave it running
$prevAlerts = $word.DisplayAlerts; $prevSecurity = $word.AutomationSecurity
$doc = $null
try {
    if ($ours) { $word.Visible = $false }
    $word.DisplayAlerts = 0             # no pop-up dialogs
    $word.AutomationSecurity = 3        # never run macros in the document
    $doc = $word.Documents.Open($src, $false, $true, $false)   # read-only, not added to recent files
    $doc.ExportAsFixedFormat($dst, 17)  # 17 = PDF
} finally {
    if ($doc) { $doc.Close(0) }
    $word.DisplayAlerts = $prevAlerts; $word.AutomationSecurity = $prevSecurity
    if ($ours) { $word.Quit() }
    [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($word)
}
"""

def _is_word_attachment(child):
    d = child["data"]
    return d.get("itemType") == "attachment" and (
        (d.get("contentType") or "").lower() in WORD_CONTENT_TYPES
        or (d.get("filename") or "").lower().endswith(WORD_EXTENSIONS))

def word_to_pdf(src, dst):
    """Convert src to a PDF at dst with Microsoft Word. Returns (ok, reason)."""
    if sys.platform != "win32":
        return False, "Word conversion only works on Windows"
    encoded = base64.b64encode(_WORD_TO_PDF_PS.encode("utf-16-le")).decode("ascii")
    env = {**os.environ, "ZRS_WORD_SRC": str(Path(src).resolve()), "ZRS_WORD_DST": str(Path(dst).resolve())}
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                            "-EncodedCommand", encoded], env=env, capture_output=True, text=True,
                           timeout=WORD_TIMEOUT_SECONDS, creationflags=subprocess.CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return False, (f"Word didn't finish within {WORD_TIMEOUT_SECONDS}s (a hidden Word may be "
                       f"left running; it closes when you sign out)")
    except Exception as e:
        return False, str(e)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip().splitlines()
        return False, (msg[0] if msg else f"PowerShell exit code {r.returncode}")
    if not Path(dst).exists() or Path(dst).stat().st_size < 100:
        return False, "Word reported success but produced no PDF"
    return True, ""

class DownloadFailed(Exception):
    """Fetching a file from Zotero's storage failed -- usually a passing server hiccup,
    so it's retried on the next check (unlike a failed Word conversion)."""

def convert_word_attachment(zot, att, dst):
    """Download a Word attachment from Zotero and convert it to a PDF at dst.
    Returns (dst, "") on success or (None, reason) if Word couldn't convert it.
    Raises DownloadFailed if the file couldn't be fetched from Zotero."""
    ext = Path(att["data"].get("filename") or "x.docx").suffix.lower() or ".docx"
    src = STAGING_DIR / f"word_source{ext}"
    tmp = STAGING_DIR / "word_converted.pdf"
    try:
        src.write_bytes(zot.file(att["key"]))
    except Exception as e:
        first = str(e).strip().splitlines()
        raise DownloadFailed(" ".join(first[:2])[:160] if first else type(e).__name__)
    ok, reason = word_to_pdf(src, tmp)
    if not ok:
        return None, reason
    tmp.replace(dst)
    return dst, ""

# ============================================================
# BARE FILES IN SEND REMARKABLE -> give them a parent item
# ============================================================
# A PDF or Word file dragged straight into a collection is a "standalone" attachment
# with no item around it. It can't be sent as-is (the annotated copy has to come back
# TO something, and Zotero doesn't let a file hold other files), so the script does
# what Zotero's "Create Parent Item" does -- with the details filled in as well as the
# file allows -- and the parent is then sent on the next check like any other item.
PLACEHOLDER_AUTHOR_RE = re.compile(
    r"^(microsoft office user|office user|windows user|owner|user|admin(istrator)?|unknown|author|"
    r"guest|default|staff|lenovo|dell|hp|asus|acer)$", re.I)

def _iso_date(s):
    """'2026-11-10T15:32:00Z' or PDF 'D:20261110153200' -> '2026-11-10' ('' if none)."""
    m = re.search(r"(\d{4})-?(\d{2})-?(\d{2})", s or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""

def _real_person(name):
    name = (name or "").strip()
    return bool(name) and " " in name.replace(",", " ").strip() and not PLACEHOLDER_AUTHOR_RE.match(name)

def document_facts(path):
    """What the file says about itself: its stored properties (title, author, created,
    last edited) and the text of its first pages."""
    import html as htmllib
    path = Path(path)
    ext = path.suffix.lower()
    props, text = {}, ""
    try:
        if ext in (".docx", ".docm"):
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
                core = z.read("docProps/core.xml").decode("utf-8", "ignore") if "docProps/core.xml" in names else ""
                body = z.read("word/document.xml").decode("utf-8", "ignore") if "word/document.xml" in names else ""
            for tag, key in [("dc:title", "title"), ("dc:creator", "author"), ("cp:lastModifiedBy", "last_edited_by"),
                             ("dcterms:created", "created"), ("dcterms:modified", "last_edited")]:
                m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", core, re.S)
                if m and m.group(1).strip():
                    props[key] = htmllib.unescape(m.group(1).strip())
            paras = re.findall(r"<w:p[ >].*?</w:p>", body, re.S)
            text = "\n".join("".join(re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", p, re.S)) for p in paras)
            text = htmllib.unescape(text)
        elif ext == ".odt":
            with zipfile.ZipFile(path) as z:
                meta = z.read("meta.xml").decode("utf-8", "ignore") if "meta.xml" in z.namelist() else ""
                body = z.read("content.xml").decode("utf-8", "ignore")
            for tag, key in [("dc:title", "title"), ("meta:initial-creator", "author"), ("dc:creator", "last_edited_by"),
                             ("meta:creation-date", "created"), ("dc:date", "last_edited")]:
                m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", meta, re.S)
                if m and m.group(1).strip():
                    props[key] = htmllib.unescape(m.group(1).strip())
            text = htmllib.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"</text:p>", "\n", body)))
        elif ext == ".pdf":
            fitz = _import_fitz()
            with fitz.open(str(path)) as doc:
                md = doc.metadata or {}
                for k_src, key in [("title", "title"), ("author", "author"),
                                   ("creationDate", "created"), ("modDate", "last_edited")]:
                    if md.get(k_src):
                        props[key] = md[k_src]
                text = "\n".join(doc[i].get_text() for i in range(min(3, len(doc))))
    except Exception as e:
        log.info(f"  [parent] Couldn't read the file's own details ({e}); using what else is known")
    for k in ("created", "last_edited"):
        if k in props:
            props[k] = _iso_date(props[k])
    return {"props": props, "text": re.sub(r"[ \t]+", " ", text).strip()[:4000]}

def propose_parent_details(config, filename, facts, date_added):
    """Title / authors / date / type for the new parent item, plus where each came from.
    Uses Haiku to read the document when available; otherwise sticks to the file's
    own properties. Never invents an author: blank if there isn't a real one."""
    props, text = facts["props"], facts["text"]
    stem = re.sub(r"[_]+", " ", Path(filename).stem).strip()
    out = {"type": "manuscript", "title": props.get("title") or stem, "authors": [], "date": "",
           "publication": "", "src": {"title": "file properties" if props.get("title") else "file name"}}

    # Only a published PDF prints its OWN DOI near the top of page 1. (A Word draft's text is
    # full of DOIs of works it cites -- using those would give the draft someone else's details.)
    doi_m = re.search(r"\b10\.\d{4,9}/[^\s\"'<>]+", text[:1500]) if Path(filename).suffix.lower() == ".pdf" else None
    if doi_m:
        meta = lookup_crossref(doi_m.group(0).rstrip(".,;)"))
        if meta and meta.get("title"):
            out.update(type="journalArticle" if meta.get("journal") else "document", title=meta["title"],
                       authors=meta.get("authors", []), date=meta.get("year", ""), publication=meta.get("journal", ""),
                       doi=meta.get("doi", ""), src={"all": f"Crossref (DOI {meta.get('doi')})"})
            return out

    if config.get("anthropic_api_key") and (text or props):
        try:
            reply = reply_lines(claude_text(config, (
                "Work out the bibliographic details of this document for a Zotero library.\n\n"
                f"FILE NAME: {filename}\n"
                f"FILE PROPERTIES: {json.dumps(props, ensure_ascii=False)}\n\n"
                f"START OF THE DOCUMENT:\n{text[:3000] or '(no text)'}\n\n"
                "Rules. TITLE: the title as written at the top of the document; else the title property; "
                "else a tidied file name. AUTHOR: people named as its authors in the document (a byline); "
                "else the Author property, but only if it is a real person's full name (not a placeholder "
                "like 'Microsoft Office User' or a login name); otherwise give no AUTHOR line -- never guess. "
                "One line per author, 'Last, First'. DATE: only a date written in the document itself (a "
                "draft date, a submission date); otherwise leave it blank. TYPE: manuscript (unpublished "
                "draft, working paper, abstract, handout), journalArticle (a published article), or document "
                "(anything else). PUBLICATION: the journal, only for a published article.\n"
                "Reply in exactly this form, nothing else:\n"
                "TYPE: ...\nTITLE: ...\nAUTHOR: Last, First\nDATE: YYYY-MM-DD or YYYY or blank\n"
                "PUBLICATION: ...\nAUTHOR_FROM: byline | properties | none"), max_tokens=400, model=CLAUDE_FAST_MODEL))
            def field(name):
                return [l.split(":", 1)[1].strip() for l in reply if l.upper().startswith(name + ":")]
            t = (field("TYPE") or [""])[0]
            if t in ("manuscript", "journalArticle", "document"):
                out["type"] = t
            if (field("TITLE") or [""])[0]:
                out["title"] = field("TITLE")[0]; out["src"]["title"] = "document"
            authors = [a for a in field("AUTHOR") if a and a.lower() not in ("none", "unknown", "n/a")]
            if authors:
                out["authors"] = authors
                out["src"]["author"] = {"byline": "the document's byline",
                                        "properties": "the file's Author property"}.get(
                                            (field("AUTHOR_FROM") or [""])[0].lower(), "the document")
            d = (field("DATE") or [""])[0]
            if re.match(r"^\d{4}(-\d{2}(-\d{2})?)?$", d):
                out["date"] = d; out["src"]["date"] = "a date written in the document"
            if out["type"] == "journalArticle":
                out["publication"] = (field("PUBLICATION") or [""])[0]
        except Exception as e:
            log.info(f"  [parent] Couldn't ask Claude about the document ({e}); using the file's properties")

    if not out["authors"] and _real_person(props.get("author")):
        out["authors"] = [props["author"]]; out["src"]["author"] = "the file's Author property"
    if not out["date"]:
        for key, label in [("last_edited", "when the file was last edited"), ("created", "when the file was created")]:
            if props.get(key):
                out["date"] = props[key]; out["src"]["date"] = label; break
    if not out["date"] and date_added:
        out["date"] = date_added[:10]; out["src"]["date"] = "when it was added to Zotero"
    return out

def _creators(names):
    out = []
    for n in names:
        n = n.strip()
        if "," in n:
            last, first = [x.strip() for x in n.split(",", 1)]
        else:
            parts = n.rsplit(" ", 1)
            first, last = (parts[0], parts[1]) if len(parts) == 2 else ("", n)
        out.append({"creatorType": "author", "firstName": first, "lastName": last} if first
                   else {"creatorType": "author", "name": last})
    return out

def give_standalone_file_a_parent(zot, config, att, collection_key):
    """Claim the bare file (take it out of Send Remarkable -- only one machine can), create a
    parent item with the best details available, and move the file under it. The parent goes
    into every collection the file was in, including Send Remarkable, so it is sent on the next
    check. If anything fails after the claim, the file is put back in Send Remarkable."""
    d = att["data"]
    fname = d.get("filename") or d.get("title") or "document"
    try:
        zot.deletefrom_collection(collection_key, att)
    except Exception as e:
        log.info(f"[Zotero→reMarkable] Not handling '{fname}' this round (taken by the other machine, "
                 f"or just edited -- will retry): {e}")
        return None
    parent_key = None
    try:
        local = STAGING_DIR / f"standalone{Path(fname).suffix.lower() or '.bin'}"
        local.write_bytes(zot.file(att["key"]))
        facts = document_facts(local)
        if not facts["text"] and not facts["props"] and local.suffix in (".doc", ".rtf"):
            pdf = STAGING_DIR / "standalone_text.pdf"
            if word_to_pdf(local, pdf)[0]:
                facts = document_facts(pdf)
        info = propose_parent_details(config, fname, facts, d.get("dateAdded", ""))

        tpl = zot.item_template(info["type"])
        tpl["title"] = info["title"]
        tpl["creators"] = _creators(info["authors"])
        tpl["date"] = info["date"]
        if info.get("publication") and "publicationTitle" in tpl:
            tpl["publicationTitle"] = info["publication"]
        if info.get("doi") and "DOI" in tpl:
            tpl["DOI"] = info["doi"]
        src = info["src"]
        provenance = (src["all"] if "all" in src else
                      f"title from {src.get('title', 'file name')}; "
                      f"author from {src.get('author', 'nowhere (none found)')}; "
                      f"date from {src.get('date', 'nowhere (none found)')}")
        tpl["extra"] = (f"Parent item created automatically for '{fname}' on {date.today().isoformat()} -- "
                        f"{provenance}. Check before citing.")
        tpl["collections"] = list(dict.fromkeys(d.get("collections", []) + [collection_key]))
        resp = zot.create_items([tpl])
        parent_key = resp["successful"]["0"]["key"]

        fresh = zot.item(att["key"])
        r = _zotero_api(config, "PATCH", f"/items/{att['key']}", json={"parentItem": parent_key, "collections": []},
                        headers={"If-Unmodified-Since-Version": str(fresh["version"])})
        if r.status_code not in (200, 204):
            raise RuntimeError(f"couldn't move the file under its new parent (HTTP {r.status_code})")
        who = ", ".join(info["authors"]) or "no author found"
        log.info(f"[Zotero→reMarkable] Gave '{fname}' a parent item: '{info['title']}' ({who}; {info['date'] or 'no date'})"
                 f" → {parent_key}; it goes to the tablet on the next check")
        return parent_key
    except Exception as e:
        log.error(f"[Zotero→reMarkable] Couldn't give '{fname}' a parent item ({e}); putting it back in Send Remarkable")
        if parent_key:
            try:
                p = zot.item(parent_key)
                _zotero_api(config, "DELETE", f"/items/{parent_key}",
                            headers={"If-Unmodified-Since-Version": str(p["version"])})
            except Exception:
                pass
        try:
            zot.addto_collection(collection_key, zot.item(att["key"]))
        except Exception as e2:
            log.error(f"[Zotero→reMarkable] Could not put '{fname}' back in Send Remarkable ({e2}); add it again by hand")
        return None
    finally:
        _clear_staging()

# ============================================================
# SYNC: Zotero -> reMarkable (with smart filenames)
# ============================================================
_no_pdf_logged = set()
_convert_failed = {}   # item key -> attachment version that couldn't be converted (not retried until it changes)

def sync_zotero_to_remarkable(zot, config, state, rmapi_path):
    send_folder    = config.get("remarkable_send_folder", DEFAULT_SEND_FOLDER)
    collection_key = config["zotero_export_collection"]

    try:
        everything = zot.collection_items(collection_key, itemType="-note")
    except Exception as e:
        log.error(f"[Zotero→reMarkable] Error fetching collection: {e}"); return
    if not everything:
        return
    items = [i for i in everything if i["data"].get("itemType") not in ("attachment", "note", "annotation")]

    # Bare files dragged straight into Send Remarkable: give each a parent item first.
    for att in everything:
        d = att["data"]
        if d.get("itemType") != "attachment" or d.get("parentItem"):
            continue
        if _is_pdf_attachment(att) or _is_word_attachment(att):
            STAGING_DIR.mkdir(exist_ok=True)
            give_standalone_file_a_parent(zot, config, att, collection_key)
        elif att["key"] not in _no_pdf_logged:
            log.info(f"[Zotero→reMarkable] Can't send '{d.get('filename') or d.get('title')}' -- only PDFs and "
                     f"Word files can go to the tablet. Leaving it in Send Remarkable.")
            _no_pdf_logged.add(att["key"])
    if not items:
        return

    if not ensure_rm_folder(rmapi_path, send_folder):
        log.error(f"[Zotero→reMarkable] Send folder {send_folder} unavailable, will retry next cycle"); return
    on_tablet = {n for t, n in rmapi_ls(rmapi_path, send_folder) if t == "f"}

    new_count = 0
    STAGING_DIR.mkdir(exist_ok=True)
    for item in items:
        item_key = item["key"]
        title = item["data"].get("title", "Untitled")
        try:
            children = zot.children(item_key)
            pdf_att = next((c for c in children if _is_pdf_attachment(c)), None)
            word_att = None if pdf_att else next((c for c in children if _is_word_attachment(c)), None)
            if not pdf_att and not word_att:
                # Left in Send Remarkable so it goes out once a PDF (or Word file) is attached.
                if item_key not in _no_pdf_logged:
                    log.info(f"[Zotero→reMarkable] No PDF or Word file yet, leaving in Send Remarkable: {title}")
                    _no_pdf_logged.add(item_key)
                continue
            if word_att and _convert_failed.get(item_key) == word_att.get("version"):
                continue   # already failed for this exact file; logged once, not retried every 2 minutes

            smart_name = build_filename(item["data"], item_key)

            # Word only: convert BEFORE claiming, so a failed conversion leaves the item
            # sitting in Send Remarkable exactly as it was.
            converted = None
            if word_att and Path(smart_name).stem not in on_tablet:
                try:
                    converted, reason = convert_word_attachment(zot, word_att, STAGING_DIR / smart_name)
                except DownloadFailed as e:
                    log.warning(f"[Zotero→reMarkable] Couldn't download the Word file for '{title}' from Zotero "
                                f"({e}) -- will try again on the next check")
                    continue
                if not converted:
                    _convert_failed[item_key] = word_att.get("version")
                    log.warning(f"[Zotero→reMarkable] Couldn't convert the Word file for '{title}' to PDF "
                                f"({reason}). Left in Send Remarkable; it will be retried if the file "
                                f"changes or the watcher restarts.")
                    continue
                log.info(f"[Zotero→reMarkable] Converted Word file to PDF: {word_att['data'].get('filename')}")

            # Claim: take it out of Send Remarkable first. Zotero accepts this from only one
            # machine; the other is refused (item version changed) and skips it.
            try:
                zot.deletefrom_collection(collection_key, item)
            except Exception as e:
                log.info(f"[Zotero→reMarkable] Not sending '{title}' this round "
                         f"(taken by the other machine, or just edited -- will retry): {e}")
                continue

            if Path(smart_name).stem in on_tablet:
                log.info(f"[Zotero→reMarkable] Already in {send_folder}, not uploading again: {smart_name}")
            else:
                local_path = STAGING_DIR / smart_name
                if not converted:
                    with open(local_path, "wb") as f:
                        f.write(zot.file(pdf_att["key"]))
                ok, _, stderr = rmapi_put(rmapi_path, str(local_path), send_folder)
                if not ok:
                    log.error(f"[Zotero→reMarkable] Upload failed: {stderr} -- putting it back in Send Remarkable")
                    try:
                        zot.addto_collection(collection_key, zot.item(item_key))
                    except Exception as e2:
                        log.error(f"[Zotero→reMarkable] Could not put '{title}' back in Send Remarkable ({e2}); add it again by hand")
                    continue
                log.info(f"[Zotero→reMarkable] ✓ Uploaded to {send_folder}: {smart_name}")
                on_tablet.add(Path(smart_name).stem)
                new_count += 1
            state["synced_from_zotero"][item_key] = smart_name

        except Exception as e:
            log.error(f"[Zotero→reMarkable] Error: {e}")
        finally:
            _clear_staging()

    if new_count:
        log.info(f"[Zotero→reMarkable] Uploaded {new_count} file(s)")

# ============================================================
# ONEDRIVE WATCHER (with smart metadata import)
# ============================================================
def run_onedrive_watcher(config, zot, stop_flag):
    watch_path     = Path(config.get("onedrive_watch_folder", ""))
    collection_key = config.get("zotero_import_collection", "")

    if not watch_path or not watch_path.exists():
        log.warning(f"[OneDrive] Watch folder not found: {watch_path}"); return

    processed_dir = watch_path / "processed"
    processed_dir.mkdir(exist_ok=True)
    seen = {f.name for f in processed_dir.glob("*.pdf")}
    log.info(f"[OneDrive] Watching: {watch_path}")

    while not stop_flag["stop"]:
        for pdf in watch_path.glob("*.pdf"):
            if pdf.name in seen: continue
            time.sleep(2)
            if not pdf.exists(): continue
            log.info(f"[OneDrive] New PDF: {pdf.name}")
            success = smart_import_pdf(pdf, zot, collection_key)
            seen.add(pdf.name)
            if success:
                dest = processed_dir / pdf.name
                i = 1
                while dest.exists():
                    dest = processed_dir / f"{pdf.stem} ({i}){pdf.suffix}"
                    i += 1
                shutil.move(str(pdf), str(dest))
                log.info(f"[OneDrive] Moved to processed: {dest.name}")
            else:
                seen.discard(pdf.name)
        time.sleep(15)

# ============================================================
# SETUP
# ============================================================
def run_setup():
    print("=" * 60)
    print("  Zotero <-> reMarkable Sync — Setup Wizard")
    print("=" * 60)
    config = load_config() or {}

    print("\nSTEP 0: Checking rmapi...")
    rmapi_path = find_rmapi()
    if not rmapi_path:
        print("  ✗ rmapi not found!"); return
    print(f"  ✓ Found: {rmapi_path}")
    ok, _, _ = rmapi_run(rmapi_path, ["ls", "/"])
    if not ok:
        print("  ✗ Cannot reach reMarkable Cloud."); return
    print("  ✓ Connected to reMarkable Cloud")

    print("\nSTEP 1: Zotero Credentials")
    existing_id  = config.get("zotero_library_id", "")
    existing_key = config.get("zotero_api_key", "")
    val = input(f"  Zotero User ID [{existing_id}]: ").strip()
    config["zotero_library_id"] = val or existing_id
    val = input(f"  Zotero API Key [{existing_key[:6]}...]: ").strip()
    config["zotero_api_key"] = val or existing_key
    config["zotero_library_type"] = "user"

    try:
        zot = zotero.Zotero(config["zotero_library_id"], "user", config["zotero_api_key"])
        collections = zot.collections()
        print(f"  ✓ Connected ({len(collections)} collections)")
    except Exception as e:
        print(f"  ✗ Zotero connection failed: {e}"); return

    print("\nSTEP 2: Zotero Collections")
    for i, c in enumerate(collections):
        print(f"  {i+1:3}. {c['data']['name']}  ({c['key']})")

    def pick(prompt, current):
        name = next((c["data"]["name"] for c in collections if c["key"] == current), current)
        val  = input(f"  {prompt} [{name}]: ").strip()
        if not val: return current
        try: return collections[int(val)-1]["key"]
        except: return current

    config["zotero_export_collection"] = pick("Number for 'Send Remarkable'", config.get("zotero_export_collection",""))
    config["zotero_import_collection"] = pick("Number for import collection", config.get("zotero_import_collection",""))

    print("\nSTEP 3: reMarkable Folder")
    current = config.get("remarkable_folder", "/Zotero Upload")
    val = input(f"  reMarkable folder [{current}]: ").strip()
    config["remarkable_folder"] = val or current

    print("\nSTEP 4: OneDrive Watch Folder")
    default = str(Path.home() / "OneDrive" / "Remarkable Upload")
    current = config.get("onedrive_watch_folder", default)
    val = input(f"  OneDrive folder [{current}]: ").strip()
    config["onedrive_watch_folder"] = val or current
    Path(config["onedrive_watch_folder"]).mkdir(parents=True, exist_ok=True)
    print(f"  ✓ Ready: {config['onedrive_watch_folder']}")

    print("\nSTEP 5: Poll Interval")
    current = config.get("poll_interval_seconds", 120)
    val = input(f"  Seconds between reMarkable syncs [{current}]: ").strip()
    config["poll_interval_seconds"] = int(val) if val.isdigit() else current
    config["log_file"] = "sync.log"

    save_config(config)
    print("\n✓ Setup complete! Run:  python zotero_remarkable_sync.py")

# ============================================================
# SINGLE INSTANCE
# ============================================================
def acquire_single_instance_lock():
    """Only one copy of the watcher may run per folder on a computer. Holds an OS-level
    lock on a small file in the temp folder for as long as the process lives (the OS
    releases it automatically when the process ends, even if it is killed). Returns the
    open lock file -- keep a reference to it -- or None if another copy holds the lock."""
    import hashlib, tempfile
    tag = hashlib.sha1(str(SCRIPT_DIR.resolve()).lower().encode()).hexdigest()[:12]
    try:
        fh = open(Path(tempfile.gettempdir()) / f"zotero_remarkable_sync_{tag}.lock", "a+")
    except OSError as e:
        # Can't even create the lock file: run unguarded rather than not at all.
        log.warning(f"Could not create the single-instance lock file ({e}); running without it.")
        return True
    try:
        if sys.platform == "win32":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh

# ============================================================
# MAIN
# ============================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--setup", action="store_true")
    parser.add_argument("--once",  action="store_true")
    parser.add_argument("--enrich", metavar="ITEMKEY",
                        help="only add tags + Related links to this existing Zotero item, then exit")
    args = parser.parse_args()

    if args.setup:
        run_setup(); return

    config = load_config()
    if not config:
        print("No config. Run:  python zotero_remarkable_sync.py --setup")
        sys.exit(1)

    setup_logging(config.get("log_file", "sync.log"))

    if args.enrich:
        zot = zotero.Zotero(config["zotero_library_id"], "user", config["zotero_api_key"])
        sys.exit(0 if enrich_existing_item(zot, config, args.enrich.strip()) else 1)

    lock = acquire_single_instance_lock()
    if lock is None:
        log.warning("Another copy of the watcher is already running for this folder -- exiting.")
        return

    rmapi_path = find_rmapi()
    if not rmapi_path:
        log.error("rmapi not found."); sys.exit(1)

    log.info("=" * 50)
    log.info("Zotero <-> reMarkable Sync starting")
    log.info(f"  reMarkable inbox:  {config['remarkable_folder']}  (archive: {config['remarkable_folder']}/{IMPORTED_SUBFOLDER})")
    log.info(f"  reMarkable sends:  {config.get('remarkable_send_folder', DEFAULT_SEND_FOLDER)}")
    log.info(f"  Import collection: {config['zotero_import_collection']}")
    log.info(f"  Export collection: {config['zotero_export_collection']}")
    log.info(f"  OneDrive folder:   {config.get('onedrive_watch_folder','not set')}")
    log.info(f"  Poll interval:     {config['poll_interval_seconds']}s")
    log.info(f"  Handwriting OCR:   {'enabled' if config.get('anthropic_api_key') else 'disabled (no anthropic_api_key set)'}")
    log.info("=" * 50)

    try:
        zot = zotero.Zotero(config["zotero_library_id"], "user", config["zotero_api_key"])
        zot.collections(limit=1)
        log.info("✓ Connected to Zotero")
    except Exception as e:
        log.error(f"Zotero connection failed: {e}"); sys.exit(1)

    ok, _, stderr = rmapi_run(rmapi_path, ["ls", "/"])
    if not ok:
        log.error(f"Cannot reach reMarkable Cloud: {stderr}"); sys.exit(1)
    log.info("✓ Connected to reMarkable Cloud")

    stop_flag = {"stop": False}
    if config.get("onedrive_watch_folder"):
        # NOTE: pyzotero's Zotero client is not thread-safe (it stores in-flight
        # request state as instance attributes). Sharing one client between the
        # main sync loop and this background thread caused ~2,300 intermittent
        # "itemType not provided" 400 errors when both threads hit the API at
        # the same moment. Give the watcher its own independent client.
        watcher_zot = zotero.Zotero(config["zotero_library_id"], "user", config["zotero_api_key"])
        Thread(target=run_onedrive_watcher, args=(config, watcher_zot, stop_flag), daemon=True).start()
        log.info("✓ OneDrive watcher started")

    try:
        while True:
            state = load_state()
            sync_remarkable_to_zotero(zot, config, state, rmapi_path)
            sync_zotero_to_remarkable(zot, config, state, rmapi_path)
            save_state(state)
            if args.once:
                log.info("Single run complete (--once)"); break
            time.sleep(config["poll_interval_seconds"])
    except KeyboardInterrupt:
        log.info("Stopped.")
    finally:
        stop_flag["stop"] = True

if __name__ == "__main__":
    main()
