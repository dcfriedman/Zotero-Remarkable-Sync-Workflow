"""
Simulation tests for the tags + Related-links step (papers returning to an existing
item). Fakes Zotero (with version checks) and Claude's replies, including replies that
try to sneak in workflow tags, too many new tags, a wrong-year citation match and a
made-up item key.

Run:  python tests/test_enrichment_simulation.py
"""
import sys, types, importlib.util, logging, json, copy, re
from pathlib import Path
sys.modules.setdefault("pyzotero", types.ModuleType("pyzotero")); sys.modules["pyzotero"].zotero = types.SimpleNamespace(Zotero=None)
spec = importlib.util.spec_from_file_location("zrs", Path(__file__).resolve().parents[1] / "zotero_remarkable_sync.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
logging.basicConfig(level=logging.INFO, format="   log: %(message)s")
CFG = {"anthropic_api_key": "k", "zotero_api_key": "z", "zotero_library_id": "42"}
ENRICH_REAL, CLAUDE_REAL = m.enrich_returned_item, m.claude_text
LIB = "http://zotero.org/users/42/items/"
MODELS = []
TAG_PROMPTS = []

def itm(key, title, surname, year, tags=(), abstract=""):
    return {"key": key, "version": 5, "data": {"key": key, "itemType": "journalArticle", "title": title, "date": year,
            "creators": [{"creatorType": "author", "lastName": surname}], "tags": [{"tag": t} for t in tags],
            "abstractNote": abstract, "relations": {}}}

class Z:
    def __init__(s):
        s.items = {i["key"]: i for i in [
            itm("TARGET01", "Social Truths and Their Grounds", "Author", "2026", tags=["phil-digest"]),
            itm("CITED001", "Social Ontology: A Primer", "Élder", "2015", tags=["social ontology"]),
            itm("CITEDBAD", "Social Ontology: A Primer", "Elder", "1999"),            # wrong year -> not a match
            itm("CONVO001", "Grounding Social Facts", "Schaffer", "2019", tags=["grounding"]),
            itm("CONVO002", "Truthmakers for Social Claims", "Griffith", "2018", tags=["testimony"]),
            itm("NOISE001", "Truth in Fiction", "Lewis", "1978", tags=["testimony"]),
        ]}
        s.patch_fail_once = set()
    def item(s, k): return copy.deepcopy(s.items[k])
    def top(s, tag=None, q=None, qmode=None, limit=20):
        out = []
        for i in s.items.values():
            d = i["data"]
            if tag and tag in [t["tag"] for t in d["tags"]]: out.append(i)
            elif q and set(m._norm(q)) & set(m._norm(d["title"])) and len(set(m._norm(q)) & set(m._norm(d["title"]))) >= 2: out.append(i)
        return copy.deepcopy(out[:limit])
    def create_items(s, lst): s.note = lst[0]; return {"successful": {"0": {"key": "NOTE0001"}}}

ZLIB = {}
API_LOG = []
import tempfile as _tf
def install(z, vocab_rows):
    ZLIB.clear(); ZLIB.update(version=100, changed=[], deleted=[], rows=vocab_rows); API_LOG.clear()
    m.TAG_CACHE_FILE = Path(_tf.mkdtemp()) / "tag_cache.json"
    NS = types.SimpleNamespace
    def api(config, method, path, **kw):
        params, hdrs = kw.get("params", {}), kw.get("headers", {})
        if method == "GET" and path == "/tags":
            ims = hdrs.get("If-Modified-Since-Version")
            API_LOG.append(("tags", params.get("since"), ims, params.get("start")))
            vh = {"Last-Modified-Version": str(ZLIB["version"])}
            if ims:
                if int(ims) >= ZLIB["version"]: return NS(status_code=304, headers={})
                return NS(status_code=200, headers=vh, json=lambda: [], raise_for_status=lambda: None)
            rows = ZLIB["changed"] if params.get("since") else ZLIB["rows"]
            st = params["start"]; page = rows[st:st + 100]
            return NS(json=lambda: page, raise_for_status=lambda: None, status_code=200, headers=vh)
        if method == "GET" and path == "/deleted":
            return NS(status_code=200, headers={}, raise_for_status=lambda: None, json=lambda: {"tags": ZLIB["deleted"], "items": []})
        key = path.rsplit("/", 1)[1]; ver = int(kw["headers"]["If-Unmodified-Since-Version"])
        if key in z.patch_fail_once:
            z.patch_fail_once.discard(key); z.items[key]["version"] += 1; return types.SimpleNamespace(status_code=412)
        if ver != z.items[key]["version"]: return types.SimpleNamespace(status_code=412)
        z.items[key]["data"].update(copy.deepcopy(kw["json"])); z.items[key]["version"] += 1
        return types.SimpleNamespace(status_code=204)
    m._zotero_api = api; m._vocab_cache.update(at=0.0, tags=[])
    m.zotero_quick_search = lambda config, q, limit=15, **kw: z.top(q=q, limit=limit)
    calls = []
    def claude(config, prompt, max_tokens=1500, model=None):
        calls.append(prompt); MODELS.append(model or m.CLAUDE_MODEL)
        if "best topical tags" in prompt:
            assert "phil-rank-2" not in prompt and "needs-pdf" not in prompt, "workflow tags leaked into vocabulary"
            TAG_PROMPTS.append(prompt)
            return ("TAG: Testimony\nTAG: phil-digest\nTAG: social ontology\nTAG: collective truth\n"
                    "TAG: metaphysics of the social\nTAG: third new tag\n"
                    "QUERY: grounding social facts\nQUERY: truthmakers social claims")
        if "List every work it cites" in prompt:   # quotes in a title used to break the JSON reply
            return ('1. Elder | 2015 | Social ontology: a primer\n'
                    '- Nobody | 2001 | The "Problem" of the \'Missing\' Book: A Study\n'
                    'Sideways | | An undated "work", with commas')
        if "same conversation" in prompt:
            assert "CITED001" not in prompt.split("CANDIDATES")[1], "cited item offered again as a candidate"
            return ("CONVO001 | Grounds social facts the way the paper does.\n"
                    "- CONVO002 | The view the paper responds to, \"as it were\".\n"
                    "MADEUP99 | hallucinated")
        raise AssertionError(prompt[:80])
    m.claude_text = claude
    return calls

VOCAB = [{"tag": t, "meta": {"type": 0, "numItems": n}} for t, n in
         [("testimony", 40), ("social ontology", 25), ("grounding", 12), ("phil-rank-2", 9), ("needs-pdf", 30), ("read", 50)]] + \
        [{"tag": f"filler tag {i}", "meta": {"type": 0, "numItems": 1}} for i in range(150)] + \
        [{"tag": "Imported Keyword", "meta": {"type": 1, "numItems": 99}}]

print("=== 1 full enrichment through the note writer ===")
z = Z(); calls = install(z, VOCAB)
m.transcribe_annotations = lambda *a, **k: [(3, "Isn't this just Elder's anchoring?")]
z.patch_fail_once = {"TARGET01"}     # you edited the item a moment ago -> one 412, then retry
m.transcribe_annotations_to_note(z, "rmapi", "/x", "TARGET01", CFG, Path("."), enrich=True, printed_text="abstract...", refs="References\nElder 2015 ...")
tags = [t["tag"] for t in z.items["TARGET01"]["data"]["tags"]]
print("   tags:", tags)
assert tags[0] == "phil-digest" and "testimony" in tags and "social ontology" in tags        # vocab spelling used, existing kept
assert "Testimony" not in tags and tags.count("phil-digest") == 1
assert tags == ["phil-digest", "testimony", "social ontology", "collective truth"]   # 3 added, <= 1 new, best-first
assert MODELS == ["claude-haiku-5-5", "claude-haiku-5-5", "claude-sonnet-5-5"], MODELS   # citations, tags: Haiku; judgment: Sonnet
assert "TAGS THIS READER PUT ON WORKS THIS PAPER CITES" in TAG_PROMPTS[0] and "social ontology (1)" in TAG_PROMPTS[0].split("FULL TAG VOCABULARY")[0]
assert "filler tag 149" in TAG_PROMPTS[0]          # the whole vocabulary is sent, not just the top tags
rel = set(z.items["TARGET01"]["data"]["relations"]["dc:relation"])
assert rel == {LIB + "CITED001", LIB + "CONVO001", LIB + "CONVO002"}, rel                    # cited + convo, no wrong-year, no hallucination
for k in ["CITED001", "CONVO001", "CONVO002"]:
    assert z.items[k]["data"]["relations"]["dc:relation"] == [LIB + "TARGET01"]                # reciprocal links
assert "dc:relation" not in z.items["CITEDBAD"]["data"]["relations"] and "dc:relation" not in z.items["NOISE001"]["data"]["relations"]
note = z.note["note"]
assert "Handwritten notes" in note and "Tags added" in note and "(new tag)" in note and "Linked as Related" in note
assert "cited in this paper" in note and "zotero://select/library/items/CONVO001" in note and "Elder&#39;" not in note
assert len(calls) == 3
assert len(m.library_tag_vocabulary(CFG)) == 150 + 3   # usable manual tags; workflow + automatic excluded, cap 300

print("=== 2 running it again doesn't duplicate tags or links ===")
m._vocab_cache.update(at=0.0, tags=[])
m.enrich_returned_item(z, CFG, "TARGET01", [], "", "References\nElder")
assert sorted(z.items["TARGET01"]["data"]["relations"]["dc:relation"]) == sorted([LIB + "CITED001", LIB + "CONVO001", LIB + "CONVO002"])
assert z.items["CONVO001"]["data"]["relations"]["dc:relation"] == [LIB + "TARGET01"]
assert len([t for t in z.items["TARGET01"]["data"]["tags"] if t["tag"] == "testimony"]) == 1

print("=== 3 no API key -> nothing happens; no handwriting + nothing found -> no note ===")
z = Z(); calls = install(z, VOCAB)
assert m.enrich_returned_item(z, {**CFG, "anthropic_api_key": ""}, "TARGET01", []) is None and calls == []
m.claude_text = lambda *a, **k: "{}"
z.note = None; m.transcribe_annotations = lambda *a, **k: []
m.transcribe_annotations_to_note(z, "rmapi", "/x", "TARGET01", CFG, Path("."), enrich=True)
assert z.note is None

print("=== 4 Claude/API blows up -> never raises; new items are never enriched ===")
z = Z(); install(z, VOCAB)
def boom(*a, **k): raise RuntimeError("API down")
m.claude_text = boom
out = m.enrich_returned_item(z, CFG, "TARGET01", [(1, "x")], "", "References...")
assert out == {"added": [], "new": [], "linked": []} and [t["tag"] for t in z.items["TARGET01"]["data"]["tags"]] == ["phil-digest"]
m.enrich_returned_item = lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not enrich new items"))
z.note = None; m.transcribe_annotations = lambda *a, **k: [(1, "note")]
m.transcribe_annotations_to_note(z, "rmapi", "/x", "TARGET01", CFG, Path("."))   # enrich defaults to False
assert "Handwritten notes" in z.note["note"] and "Tags added" not in z.note["note"]
print("=== 5 at most 2 new tags even if the model proposes more ===")
z = Z(); install(z, VOCAB)
m.enrich_returned_item = ENRICH_REAL
m.claude_text = lambda config, prompt, max_tokens=1500, model=None: (
    "TAG: brand new one\nTAG: brand new two\nTAG: brand new three\nTAG: testimony\nTAG: grounding"
    if "best topical tags" in prompt else "NONE")
m.enrich_returned_item(z, CFG, "TARGET01", [])
assert [t["tag"] for t in z.items["TARGET01"]["data"]["tags"]] == ["phil-digest", "brand new one", "brand new two", "testimony"]

print("=== 6 Haiku failing (e.g. retired) falls back to Sonnet once ===")
seen = []
class Msgs:
    def create(self, model, max_tokens, messages):
        seen.append(model)
        if model == "claude-haiku-5-5": raise RuntimeError("404 model not found")
        return types.SimpleNamespace(content=[types.SimpleNamespace(text='{"ok": 1}')])
sys.modules["anthropic"] = types.SimpleNamespace(Anthropic=lambda api_key: types.SimpleNamespace(messages=Msgs()))
m.claude_text = CLAUDE_REAL
assert m.claude_text(CFG, "hi", model=m.CLAUDE_FAST_MODEL) == '{"ok": 1}' and seen == ["claude-haiku-5-5", "claude-sonnet-5-5"]

print("=== 7 cropping: blank / highlight-only pages skipped, handwriting cropped tight ===")
from PIL import Image, ImageDraw
import io
def page(draw_fn):
    im = Image.new("RGB", (1275, 1650), "white"); draw_fn(ImageDraw.Draw(im)); b = io.BytesIO(); im.save(b, "PNG"); return b.getvalue()
assert m.crop_to_ink(page(lambda d: None)) is None
assert m.crop_to_ink(page(lambda d: d.rectangle([100, 300, 1100, 340], fill=(255, 237, 117)))) is None   # yellow highlight
assert m.crop_to_ink(page(lambda d: d.rectangle([100, 300, 1100, 340], fill=(162, 241, 135)))) is None   # green highlight
note = m.crop_to_ink(page(lambda d: d.text((1100, 800), "cf. Kant!", fill=(20, 20, 20))))                # small margin note
w, h = Image.open(io.BytesIO(note)).size
assert note and w * h < 0.02 * 1275 * 1650, (w, h)
pencil = m.crop_to_ink(page(lambda d: d.line([(200, 200), (600, 260)], fill=(127, 127, 127), width=3)))   # grey pen still counts
assert pencil is not None
both = m.crop_to_ink(page(lambda d: (d.line([(100, 100), (300, 120)], fill="black", width=3), d.line([(900, 1500), (1100, 1520)], fill="black", width=3))))
w, h = Image.open(io.BytesIO(both)).size
assert w < 1275 and h < 1650   # spans both notes, still trimmed
print("=== 8 near-duplicates collapse onto your existing tags (and onto the item's own) ===")
for a, b in [("Epistemic Trust", "trust, epistemic"), ("epistemic-trust", "epistemic trusts"), ("Theories of Truth", "truth theory"),
             ("Épistémologie", "epistemologie"), ("analysis", "analysis"), ("ethics", "ethics")]:
    assert m.tag_key(a) == m.tag_key(b), (a, b)
assert m.tag_key("social epistemology") != m.tag_key("social ontology")
z = Z(); install(z, VOCAB + [{"tag": "epistemic trust", "meta": {"type": 0, "numItems": 7}}])
z.items["TARGET01"]["data"]["tags"].append({"tag": "Testimony"})
m.enrich_returned_item = ENRICH_REAL
m.claude_text = lambda config, prompt, max_tokens=1500, model=None: (
    "TAG: Trust, Epistemic\nTAG: \"Testimonies\"\nTAG: PHIL-DIGEST\nTAG: Social Ontologies\nTAG: grounding\nTAG: extra"
    if "best topical tags" in prompt else "NONE")
out = m.enrich_returned_item(z, CFG, "TARGET01", [])
assert [t["tag"] for t in z.items["TARGET01"]["data"]["tags"]] == ["phil-digest", "Testimony", "epistemic trust", "social ontology", "grounding"]
assert out["new"] == []
print("=== 9 reading the reference list fails -> tags and same-conversation links still happen ===")
z = Z(); calls = install(z, VOCAB)
real = m.claude_text
def flaky(config, prompt, max_tokens=1500, model=None):
    if "List every work it cites" in prompt: raise RuntimeError("Haiku hiccup")
    if "same conversation" in prompt: return "CONVO001 | Same debate.\nCONVO002 | The view it answers."
    return real(config, prompt, max_tokens, model)
m.claude_text = flaky
out = m.enrich_returned_item(z, CFG, "TARGET01", [(1, "notes")], "", "References\nElder")
assert out["added"] and {k for k, _, _ in out["linked"]} == {"CONVO001", "CONVO002"}
print("=== 10 --enrich ITEMKEY: tags + links for an item already in Zotero, from its PDF and notes ===")
z = Z(); calls = install(z, VOCAB)
m.enrich_returned_item = ENRICH_REAL
z.children = lambda k: ([
    {"key": "OLDPDF01", "data": {"itemType": "attachment", "contentType": "application/pdf", "filename": "x.pdf", "dateAdded": "2026-01-01T00:00:00Z"}},
    {"key": "ANNOT001", "data": {"itemType": "attachment", "contentType": "application/pdf", "filename": "x (annotated 2026-11-17).pdf", "dateAdded": "2026-11-17T00:00:00Z"}},
    {"key": "NOTE0001", "data": {"itemType": "note", "tags": [{"tag": "AI-transcribed"}],
        "note": "<p><strong>From reMarkable</strong></p><p><em>Page 5:</em><br/>Isn&#39;t this &quot;just&quot; Elder?<br/>cf. p.12</p>"}},
] if k == "TARGET01" else [])
fetched = []
z.file = lambda k: (fetched.append(k), b"%PDF-1.4 fake")[1]
z.note = None
seen = {}
def spy(zot, config, key, transcripts, printed="", refs=""):
    seen["t"] = transcripts; return {"added": ["testimony"], "new": [], "linked": [("CONVO001", "Schaffer (2019). Grounding Social Facts", "Same debate.")]}
m.enrich_returned_item = spy
assert m.enrich_existing_item(z, CFG, "TARGET01") is True
assert fetched == ["ANNOT001"]                                                    # newest annotated PDF used
assert seen["t"] == [(5, "Isn't this \"just\" Elder?\ncf. p.12")]           # your notes recovered from the note
assert "Tags and links" in z.note["note"] and "Handwritten notes" not in z.note["note"] and "CONVO001" in z.note["note"]
m.enrich_returned_item = ENRICH_REAL
print("=== 11 saved tag list: full download once, then only changes ===")
z = Z(); install(z, VOCAB); m._vocab_cache.update(at=0.0, tags=[])
pages = lambda: [e for e in API_LOG if e[0] == "tags" and e[2] is None]
v1 = m.library_tag_vocabulary(CFG)
assert len(pages()) == 2 and m.TAG_CACHE_FILE.exists()                       # 157 tags = 2 pages, saved
API_LOG.clear(); m._vocab_cache.update(at=0.0, tags=[])
assert m.library_tag_vocabulary(CFG) == v1 and pages() == []                  # unchanged library: one 'not modified' reply
ZLIB.update(version=101, changed=[{"tag": "brand fresh tag", "meta": {"type": 0, "numItems": 1}}], deleted=["filler tag 3"])
API_LOG.clear(); m._vocab_cache.update(at=0.0, tags=[])
v2 = m.library_tag_vocabulary(CFG)
assert "brand fresh tag" in v2 and "filler tag 3" not in v2 and all(e[1] == 100 for e in pages())   # only changes fetched
assert json.loads(m.TAG_CACHE_FILE.read_text())["version"] == 101
c = json.loads(m.TAG_CACHE_FILE.read_text()); c["full_at"] -= 8 * 86400; m.TAG_CACHE_FILE.write_text(json.dumps(c))
API_LOG.clear(); m._vocab_cache.update(at=0.0, tags=[]); m.library_tag_vocabulary(CFG)
assert len([e for e in pages() if e[1] is None]) == 2                        # older than a week: full refresh
m.TAG_CACHE_FILE.write_text("{ broken"); API_LOG.clear(); m._vocab_cache.update(at=0.0, tags=[])
assert len(m.library_tag_vocabulary(CFG)) > 100 and len(pages()) == 2         # damaged file: just download again

print("=== 12 cited works looked up four at a time, same results ===")
import time as _t
z = Z(); install(z, VOCAB)
def slow_search(config, q, limit=15, **kw):
    _t.sleep(0.25); return z.top(q=q, limit=limit)
m.zotero_quick_search = slow_search
lines = "\n".join(["Elder | 2015 | Social ontology: a primer"] + [f"Nobody{i} | 2001 | Unrelated work number {i}" for i in range(11)])
m.claude_text = lambda config, prompt, max_tokens=1500, model=None: lines
t0 = _t.time(); found = m.find_cited_in_library(z, CFG, "References...", "TARGET01"); dt = _t.time() - t0
assert dt < 1.5, dt                                                            # 12 x 0.25 s would be 3 s one at a time
z2 = Z(); z2.items["CITED001"]["data"]["creators"][0]["lastName"] = "Elder"
m.zotero_quick_search = lambda config, q, limit=15, **kw: (_t.sleep(0.1), z2.top(q=q, limit=limit))[1]
assert list(m.find_cited_in_library(z2, CFG, "References...", "TARGET01")) == ["CITED001"]   # only the real match

print("=== 13 Zotero says 'too many requests': wait as told, then retry ===")
answers = [types.SimpleNamespace(status_code=429, headers={"Retry-After": "0"}),
           types.SimpleNamespace(status_code=200, headers={}, json=lambda: [{"key": "X"}])]
m._zotero_api = lambda config, method, path, **kw: answers.pop(0)
real_search = importlib.util.module_from_spec(spec); spec.loader.exec_module(real_search)
real_search._zotero_api = m._zotero_api
assert real_search.zotero_quick_search(CFG, "q", attempts=2) == [{"key": "X"}]
print("\nALL ENRICHMENT TESTS PASSED")
