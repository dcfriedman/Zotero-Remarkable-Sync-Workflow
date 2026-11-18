"""
Simulation tests for zotero_remarkable_sync.py -- no network, no real Zotero or
reMarkable. Fakes both services (including rmapi's glob-matching of mv sources and
Zotero's version check on collection edits) and checks the behaviours that matter:
returning annotations to the original item, one authoritative PDF, two machines
racing for the same work, and undoing a claim when something fails.

Run:  python tests/test_sync_simulation.py
"""
import sys, types, tempfile, importlib.util, posixpath, logging, fnmatch, copy
from pathlib import Path
from datetime import date
sys.modules.setdefault("pyzotero", types.ModuleType("pyzotero")); sys.modules["pyzotero"].zotero = types.SimpleNamespace(Zotero=None)
spec = importlib.util.spec_from_file_location("zrs", Path(__file__).resolve().parents[1] / "zotero_remarkable_sync.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
logging.basicConfig(level=logging.INFO, format="   log: %(message)s")
TODAY = date.today().isoformat()
ENRICH = []

class FakeRM:
    def __init__(s, tree): s.t, s.mv_fails, s.calls, s.after_inbox_ls = tree, False, [], None
    def run(s, path, args, timeout=120):
        s.calls.append(args); c = args[0]
        if c == "ls":
            if args[1] not in s.t: return False, "", "not found"
            out = "\n".join(f"[{k}]\t{n}" for n, k in s.t[args[1]].items())
            if args[1] == "/Zotero Upload" and s.after_inbox_ls:   # the other machine acts right after our listing
                hook, s.after_inbox_ls = s.after_inbox_ls, None; hook()
            return True, out, ""
        if c == "mkdir":
            p, b = posixpath.split(args[1]); p = p or "/"
            if b in s.t.get(p, {}): return False, "", "exists"
            s.t[p][b] = "d"; s.t[args[1]] = {}; return True, "", ""
        if c == "mv":
            if s.mv_fails: return False, "", "simulated network failure"
            src, dst = args[1], args[2]; sp, pat = posixpath.split(src)
            hits = [n for n in s.t.get(sp, {}) if fnmatch.fnmatchcase(n, pat)]   # real rmapi globs the source
            if len(hits) != 1: return False, "", f"no matches for '{pat}'"
            sn = hits[0]
            if dst in s.t: dp, dn = dst, sn
            else:
                dp, dn = posixpath.split(dst)
                if dp not in s.t: return False, "", "no parent"
            if dn in s.t[dp]: return False, "", "already exists"
            del s.t[sp][sn]; s.t[dp][dn] = "f"; return True, "", ""
        raise AssertionError(args)
    def put(s, path, local, folder):
        s.calls.append(["put", Path(local).name, folder])
        if getattr(s, "put_fails", False): return False, "", "simulated upload failure"
        s.t[folder][Path(local).stem] = "f"; return True, "", ""

class FakeZot:
    """Zotero with item versions, so removing from a collection is a compare-and-swap like the real API."""
    def __init__(s): s.items, s.kids, s.coll, s.notes, s.attached, s.trashed, s.n, s.attach_fails = {}, {}, {}, [], [], [], 0, False
    def _k(s): s.n += 1; return f"NEW{s.n:05d}"
    def add(s, key, data, colls=()):
        data.setdefault("tags", []); data["collections"] = list(colls); s.items[key] = {"key": key, "version": 1, "data": data}
        for c in colls: s.coll.setdefault(c, []).append(key)
    def kid(s, parent, filename, annotated_in_zotero=False):
        k = s._k(); s.kids.setdefault(parent, []).append({"key": k, "version": 1, "data": {"itemType": "attachment", "title": filename, "filename": filename, "contentType": "application/pdf"}})
        if annotated_in_zotero: s.kids[k] = [{"key": s._k(), "version": 1, "data": {"itemType": "annotation"}}]
        return k
    def item(s, key):
        if getattr(s, "item_down", False): raise Exception("ReadTimeout: timed out")
        if key not in s.items: raise Exception("404")
        return copy.deepcopy(s.items[key])
    def item_template(s, t): return {"itemType": t, "title": "", "collections": [], "tags": []}
    def create_items(s, lst):
        it = lst[0]; key = s._k()
        if it.get("itemType") == "note": s.notes.append(it["parentItem"])
        else:
            s.items[key] = {"key": key, "version": 1, "data": dict(it)}
            for c in it.get("collections", []): s.coll.setdefault(c, []).append(key)
        return {"successful": {"0": {"key": key}}}
    def attachment_simple(s, paths, parent):
        if s.attach_fails: return {"failure": [1]}
        s.attached.append((parent, Path(paths[0]).name)); s.kid(parent, Path(paths[0]).name)
    def children(s, k): return copy.deepcopy(s.kids.get(k, []))
    def collection_items(s, coll, q=None, tag=None, itemType=None, **kw):
        if getattr(s, "stale", None) is not None: out, s.stale = s.stale, None; return out
        return [copy.deepcopy(s.items[k]) for k in s.coll.get(coll, [])]
    def top(s, q=None, tag=None, limit=25, **kw):
        words = m._norm(q or "")
        return [copy.deepcopy(i) for i in s.items.values()
                if all(w in m._norm(i["data"].get("title", "")) for w in words)][:limit]
    def file(s, key): return b"%PDF-1.4 clean" + b"x" * 300
    def deletefrom_collection(s, coll, item):
        cur = s.items[item["key"]]
        if cur["version"] != item["version"]: raise Exception("412 Precondition Failed")
        s.coll[coll].remove(item["key"]); cur["version"] += 1
    def addto_collection(s, coll, item):
        s.coll.setdefault(coll, []).append(item["key"]); s.items[item["key"]]["version"] += 1

def fake_patch_factory(z):
    def patch(url, json=None, timeout=None, headers=None):
        key = url.rsplit("/", 1)[1]
        for parent, kids in z.kids.items():
            for c in kids:
                if c["key"] == key:
                    kids.remove(c); z.trashed.append(c["data"]["filename"]); return types.SimpleNamespace(status_code=204)
        return types.SimpleNamespace(status_code=404)
    return patch

CFG = {"remarkable_folder": "/Zotero Upload", "zotero_import_collection": "IMP", "zotero_export_collection": "SEND",
       "zotero_library_id": "1", "zotero_api_key": "k"}
U, I, F = "/Zotero Upload", "/Zotero Upload/Imported", "/From Zotero"
def setup(tree, z):
    rm = FakeRM(tree); m.STAGING_DIR = Path(tempfile.mkdtemp()); m.rmapi_run = rm.run; m.rmapi_put = rm.put
    def get(p, remote, d):
        (Path(d) / (posixpath.basename(remote) + "-annotations.pdf")).write_bytes(b"%PDF-1.4 annotated" + b"x" * 500); return True, "", ""
    m.rmapi_get = get
    m.requests = types.SimpleNamespace(patch=fake_patch_factory(z), utils=None)
    def search(config, q, limit=15, **kw):
        if getattr(z, "search_down", False): raise m.ZoteroUnavailable("simulated 30s timeout")
        return z.top(q=q, limit=limit)
    m.zotero_quick_search = search
    tr = []; m.transcribe_annotations_to_note = lambda zz, r, path, k, c, s_, **kw: (tr.append((path, k)), ENRICH.append(kw.get("enrich")))
    m._no_pdf_logged.clear()
    return rm, tr
st = lambda: {"synced_to_zotero": [], "synced_from_zotero": {}, "state_version": 2}
def hdr(t): print(f"\n=== {t} ===")
def orig(z, key="ABCD2345", colls=()):
    z.add(key, {"title": "Some Title", "date": "2026", "creators": [{"creatorType": "author", "lastName": "AUTHOR"}]}, colls)
NAME = "AUTHOR - 2026 - Some Title [ABCD2345]"

hdr("1 return: attach to original, trash clean PDF, nothing dangling")
z = FakeZot(); orig(z); z.kid("ABCD2345", "AUTHOR.pdf")
rm, tr = setup({"/": {"Zotero Upload": "d"}, U: {NAME: "f"}}, z)
m.sync_remarkable_to_zotero(z, CFG, st(), "rmapi")
assert list(z.items) == ["ABCD2345"], list(z.items)
assert [c["data"]["filename"] for c in z.kids["ABCD2345"]] == [f"AUTHOR - 2026 - Some Title (annotated {TODAY}).pdf"]
assert z.trashed == ["AUTHOR.pdf"] and tr == [(f"{I}/{NAME}", "ABCD2345")] and rm.t[U] == {"Imported": "d"}
assert ENRICH[-1] is True

hdr("2 old PDF with Zotero-reader highlights is kept")
z = FakeZot(); orig(z); z.kid("ABCD2345", "AUTHOR.pdf", annotated_in_zotero=True)
rm, tr = setup({"/": {"Zotero Upload": "d"}, U: {NAME: "f"}}, z)
m.sync_remarkable_to_zotero(z, CFG, st(), "rmapi")
assert z.trashed == [] and len(z.kids["ABCD2345"]) == 2

hdr("3 second round (name with ' (2)'): attaches, trashes first annotated copy")
z = FakeZot(); orig(z); z.kid("ABCD2345", "AUTHOR - 2026 - Some Title (annotated 2026-11-01).pdf")
rm, tr = setup({"/": {"Zotero Upload": "d"}, U: {NAME + " (2)": "f", "Imported": "d"}, I: {NAME: "f"}}, z)
m.sync_remarkable_to_zotero(z, CFG, st(), "rmapi")
assert list(z.items) == ["ABCD2345"] and z.trashed == ["AUTHOR - 2026 - Some Title (annotated 2026-11-01).pdf"]
assert len(z.kids["ABCD2345"]) == 1

hdr("4 two machines race for the same returned doc: exactly one processes it")
z = FakeZot(); orig(z); z.kid("ABCD2345", "AUTHOR.pdf")
tree = {"/": {"Zotero Upload": "d"}, U: {NAME: "f", "Imported": "d"}, I: {}}
rm, tr = setup(tree, z)
def other_machine_claims(): del tree[U][NAME]; tree[I][NAME] = "f"
rm.after_inbox_ls = other_machine_claims     # other laptop moves it right after our listing
m.sync_remarkable_to_zotero(z, CFG, st(), "rmapi")
assert z.attached == [] and tr == [] and list(z.items) == ["ABCD2345"]

hdr("5 attach fails -> claim undone, doc back in inbox, nothing created")
z = FakeZot(); orig(z); z.kid("ABCD2345", "AUTHOR.pdf"); z.attach_fails = True
rm, tr = setup({"/": {"Zotero Upload": "d"}, U: {NAME: "f", "Imported": "d"}, I: {}}, z)
m.sync_remarkable_to_zotero(z, CFG, st(), "rmapi")
assert NAME in rm.t[U] and rm.t[I] == {} and tr == [] and z.trashed == [] and list(z.items) == ["ABCD2345"]

hdr("6 mv fails (network) -> nothing processed, doc stays for next cycle, then works")
z = FakeZot(); orig(z); z.kid("ABCD2345", "AUTHOR.pdf")
rm, tr = setup({"/": {"Zotero Upload": "d"}, U: {NAME: "f", "Imported": "d"}, I: {}}, z); rm.mv_fails = True; s = st()
m.sync_remarkable_to_zotero(z, CFG, s, "rmapi"); assert z.attached == [] and NAME in rm.t[U]
rm.mv_fails = False; m.sync_remarkable_to_zotero(z, CFG, s, "rmapi"); assert len(z.attached) == 1 and rm.t[I] == {NAME: "f"}

hdr("7 no key -> new item in import collection (title without suffix); deleted original -> new item")
z = FakeZot(); rm, tr = setup({"/": {"Zotero Upload": "d"}, U: {"My Own Notes": "f", NAME: "f"}}, z)
m.sync_remarkable_to_zotero(z, CFG, st(), "rmapi")
assert sorted(i["data"]["title"] for i in z.items.values()) == sorted(["My Own Notes", "AUTHOR - 2026 - Some Title"])
assert ENRICH[-2:] == [False, False]   # new items are never tagged/linked

hdr("8 legacy pending-archive entry: only moved, not re-imported")
z = FakeZot(); orig(z); rm, tr = setup({"/": {"Zotero Upload": "d"}, U: {NAME: "f", "Imported": "d"}, I: {}}, z)
s = st(); s["synced_to_zotero"] = [NAME]
m.sync_remarkable_to_zotero(z, CFG, s, "rmapi")
assert z.attached == [] and rm.t[I] == {NAME: "f"} and s["synced_to_zotero"] == []

hdr("9 send: claimed via collection removal, uploaded with key")
z = FakeZot(); orig(z, colls=["SEND"]); z.kid("ABCD2345", "AUTHOR.pdf")
rm, tr = setup({"/": {"Zotero Upload": "d"}, U: {}}, z)
m.sync_zotero_to_remarkable(z, CFG, st(), "rmapi")
assert rm.t[F] == {"AUTHOR - 2026 - Some Title [ABCD2345]": "f"} and z.coll["SEND"] == []

hdr("10 send race: other machine claimed first -> we skip (no second upload)")
z = FakeZot(); orig(z, colls=["SEND"]); z.kid("ABCD2345", "AUTHOR.pdf")
rm, tr = setup({"/": {"Zotero Upload": "d"}, U: {}}, z)
z.stale = z.collection_items("SEND")                     # our snapshot, taken before...
z.deletefrom_collection("SEND", z.item("ABCD2345"))      # ...the other machine claimed it
m.sync_zotero_to_remarkable(z, CFG, st(), "rmapi")
assert not any(c[0] == "put" for c in rm.calls)

hdr("11 upload fails -> put back in Send Remarkable for retry")
z = FakeZot(); orig(z, colls=["SEND"]); z.kid("ABCD2345", "AUTHOR.pdf")
rm, tr = setup({"/": {"Zotero Upload": "d"}, U: {}}, z); rm.put_fails = True
m.sync_zotero_to_remarkable(z, CFG, st(), "rmapi")
assert z.coll["SEND"] == ["ABCD2345"]

hdr("12 full round trip on two machines, alternating")
z = FakeZot(); orig(z, colls=["SEND"]); z.kid("ABCD2345", "AUTHOR.pdf")
tree = {"/": {"Zotero Upload": "d"}, U: {}}; rm, tr = setup(tree, z); home, work = st(), st()
m.sync_zotero_to_remarkable(z, CFG, home, "rmapi"); m.sync_zotero_to_remarkable(z, CFG, work, "rmapi")
m.sync_remarkable_to_zotero(z, CFG, work, "rmapi")
tree[F].pop(NAME); tree[U][NAME] = "f"                 # annotate + move to Zotero Upload
m.sync_remarkable_to_zotero(z, CFG, work, "rmapi"); m.sync_remarkable_to_zotero(z, CFG, home, "rmapi")
assert sum(1 for c in rm.calls if c[0] == "put") == 1 and len(z.attached) == 1 and list(z.items) == ["ABCD2345"]
assert [c["data"]["filename"] for c in z.kids["ABCD2345"]] == [f"AUTHOR - 2026 - Some Title (annotated {TODAY}).pdf"]

assert m.KEY_SUFFIX_RE.search(NAME + " (3)").group(1) == "ABCD2345" and not m.KEY_SUFFIX_RE.search("Kant [of Pure Reason]")
hdr("13 legacy names without an item ID are matched to the original")
def lib(z):
    z.add("ORIGINAL", {"title": "A Long Title, Applied to the Case of Something Specific", "date": "2025-07-03", "creators": [{"creatorType": "author", "lastName": "Author"}]})
    z.add("DUPEITEM", {"title": "Author - 2025 - A Long Title, Applied to the Case of Something Specific", "creators": [], "tags": [{"tag": "reMarkable-import"}]})
    z.add("WHYSETTL", {"title": "Why Bother?", "date": "2024", "creators": [{"creatorType": "author", "lastName": "Writer"}]})
    z.add("ABILITY1", {"title": "Abilities", "date": "2019", "creators": [{"creatorType": "author", "lastName": "Scholar"}]})
    z.add("ABILITY2", {"title": "Abilities and Skills", "date": "2021", "creators": [{"creatorType": "author", "lastName": "Scholar"}]})
    z.add("LONGTTL1", {"title": "Structures and informal norms: competition and coordination within scientific communities of practice", "date": "2015", "creators": [{"creatorType": "author", "lastName": "Longname"}]})
z = FakeZot(); lib(z); f = m.find_original_by_name
STU = "Author - 2025 - A Long Title, Applied to the Case of Something Specific"
assert f(z, STU) == "ORIGINAL"                                   # a full title
assert f(z, STU + " (2)") == "ORIGINAL"                          # moved back from Imported
assert f(z, "Writer - 2024 - Why Bother_") == "WHYSETTL"       # '?' was replaced in the old filename
assert f(z, "Writer - Why Bother") == "WHYSETTL"               # no year
assert f(z, "Scholar - Abilities") == "ABILITY1"                 # one-word title must match in full
assert f(z, "Longname - 2015 - Structures and informal norms competition and coordination within scientific communities of pra") == "LONGTTL1"  # cut short
assert f(z, "Author - 2019 - A Long Title") is None   # wrong year
assert f(z, "Smith - 2025 - A Long Title") is None     # wrong author
assert f(z, "Unknown - n.d. - Abilities") is None and f(z, "My own notebook") is None
z.add("DUPLICAT", {"title": "Why Bother?", "date": "2024", "creators": [{"creatorType": "author", "lastName": "Writer"}]})
assert f(z, "Writer - 2024 - Why Bother_") is None             # two possible originals -> don't guess

z = FakeZot(); lib(z); z.kid("ORIGINAL", "Author - 2025 - A Long Title.pdf")
rm, tr = setup({"/": {"Zotero Upload": "d"}, U: {STU: "f", "Imported": "d"}, I: {STU: "f"}}, z)
before = set(z.items)
m.sync_remarkable_to_zotero(z, CFG, st(), "rmapi")
assert set(z.items) == before and z.attached[0][0] == "ORIGINAL" and ENRICH[-1] is True
assert z.attached[0][1].startswith("Author - 2025 - A Long Title") and z.trashed == ["Author - 2025 - A Long Title.pdf"]
assert rm.t[I] == {STU: "f", STU + " (2)": "f"}
hdr("14 Zotero too slow to answer -> document put back and retried, never a new item")
z = FakeZot(); lib(z); z.kid("ORIGINAL", "Author.pdf"); z.search_down = True
rm, tr = setup({"/": {"Zotero Upload": "d"}, U: {STU: "f", "Imported": "d"}, I: {}}, z); s_ = st()
before = set(z.items)
m.sync_remarkable_to_zotero(z, CFG, s_, "rmapi")
assert set(z.items) == before and z.attached == [] and STU in rm.t[U] and rm.t[I] == {}     # back in the inbox
z.search_down = False
m.sync_remarkable_to_zotero(z, CFG, s_, "rmapi")                                            # next cycle works
assert set(z.items) == before and z.attached[0][0] == "ORIGINAL"
# same for the item-ID path: a timeout is not "item deleted"
z = FakeZot(); orig(z); z.kid("ABCD2345", "AUTHOR.pdf"); z.item_down = True
rm, tr = setup({"/": {"Zotero Upload": "d"}, U: {NAME: "f", "Imported": "d"}, I: {}}, z)
m.sync_remarkable_to_zotero(z, CFG, st(), "rmapi")
assert list(z.items) == ["ABCD2345"] and z.attached == [] and NAME in rm.t[U]
z.item_down = False; m.sync_remarkable_to_zotero(z, CFG, st(), "rmapi")
assert z.attached[0][0] == "ABCD2345" and list(z.items) == ["ABCD2345"]
assert m.search_words(m._norm("A Long Title, Applied to the Case of Something Specific")) == ["long", "title", "applied", "something", "specific"]
hdr("15 Word files in Send Remarkable are converted with Word and sent as PDF")
def word_item(z, key="WORDITEM", fname="Draft chapter.docx", ctype="application/vnd.openxmlformats-officedocument.wordprocessingml.document", ver=1):
    z.add(key, {"title": "A Draft Chapter", "date": "2026", "creators": [{"creatorType": "author", "lastName": "Writer"}]}, ["SEND"])
    z.kids.setdefault(key, []).append({"key": "ATT" + key[:5], "version": ver, "data": {"itemType": "attachment", "title": fname, "filename": fname, "contentType": ctype}})
conversions = []
def fake_word(src, dst, ok=True):
    conversions.append(Path(src).suffix)
    if ok: Path(dst).write_bytes(b"%PDF-1.4 from word" + b"x" * 300); return True, ""
    return False, "Word isn't installed"
z = FakeZot(); word_item(z)
rm, tr = setup({"/": {"Zotero Upload": "d"}, U: {}}, z)
m.word_to_pdf = fake_word
m._convert_failed.clear()
m.sync_zotero_to_remarkable(z, CFG, st(), "rmapi")
assert rm.t[F] == {"Writer - 2026 - A Draft Chapter [WORDITEM]": "f"} and z.coll["SEND"] == [] and conversions == [".docx"]
# conversion fails -> stays in Send Remarkable, tried once (not every 2 minutes), retried when the file changes
z = FakeZot(); word_item(z, fname="old.doc", ctype="application/msword")
rm, tr = setup({"/": {"Zotero Upload": "d"}, U: {}}, z); conversions.clear(); m._convert_failed.clear()
m.word_to_pdf = lambda src, dst: fake_word(src, dst, ok=False)
m.sync_zotero_to_remarkable(z, CFG, st(), "rmapi"); m.sync_zotero_to_remarkable(z, CFG, st(), "rmapi")
assert z.coll["SEND"] == ["WORDITEM"] and conversions == [".doc"] and not any(c[0] == "put" for c in rm.calls)
z.kids["WORDITEM"][0]["version"] = 2; m.word_to_pdf = fake_word
m.sync_zotero_to_remarkable(z, CFG, st(), "rmapi")
assert z.coll["SEND"] == [] and conversions == [".doc", ".doc"]
# an item with both a PDF and a Word file sends the PDF, no conversion
z = FakeZot(); word_item(z); z.kid("WORDITEM", "paper.pdf")
rm, tr = setup({"/": {"Zotero Upload": "d"}, U: {}}, z); conversions.clear(); m._convert_failed.clear()
m.sync_zotero_to_remarkable(z, CFG, st(), "rmapi")
assert conversions == [] and z.coll["SEND"] == []
# recognised by type or extension; other files aren't
A = lambda fn, ct="": {"data": {"itemType": "attachment", "filename": fn, "contentType": ct}}
assert m._is_word_attachment(A("x.DOCX")) and m._is_word_attachment(A("x.rtf")) and m._is_word_attachment(A("noext", "application/msword"))
assert not m._is_word_attachment(A("x.pdf", "application/pdf")) and not m._is_word_attachment(A("x.xlsx"))
# the PowerShell script survives the encoding round trip and is plain ASCII
import base64 as _b
enc = _b.b64encode(m._WORD_TO_PDF_PS.encode("utf-16-le")).decode()
assert _b.b64decode(enc).decode("utf-16-le") == m._WORD_TO_PDF_PS and m._WORD_TO_PDF_PS.isascii()
hdr("16 bare files in Send Remarkable get a parent item with sensible details, then are sent")
import zipfile as _zf, json as _json
def make_docx(path, title_prop="", author="", created="2026-03-02T10:00:00Z", modified="2026-11-10T15:32:00Z", body=()):
    core = ('<cp:coreProperties xmlns:cp="x" xmlns:dc="y" xmlns:dcterms="z">'
            + (f"<dc:title>{title_prop}</dc:title>" if title_prop else "")
            + (f"<dc:creator>{author}</dc:creator>" if author else "")
            + f'<dcterms:created xsi:type="w3cdtf">{created}</dcterms:created><dcterms:modified xsi:type="w3cdtf">{modified}</dcterms:modified></cp:coreProperties>')
    doc = "<w:document><w:body>" + "".join(f'<w:p><w:r><w:t xml:space="preserve">{t}</w:t></w:r></w:p>' for t in body) + "</w:body></w:document>"
    with _zf.ZipFile(path, "w") as z:
        z.writestr("docProps/core.xml", core); z.writestr("word/document.xml", doc)
    return Path(path).read_bytes()

tmpd = Path(tempfile.mkdtemp())
make_docx(tmpd / "a.docx", author="Jane Author", body=["My Real Title", "Jane Author", "Draft of 3 Nov 2026", "Abstract. We argue &amp; show..."])
facts = m.document_facts(tmpd / "a.docx")
assert facts["props"] == {"author": "Jane Author", "created": "2026-03-02", "last_edited": "2026-11-10"}, facts["props"]
assert facts["text"].startswith("My Real Title\nJane Author") and "&amp;" not in facts["text"]

# details: Claude reads byline + written date; no key -> properties only; placeholders rejected; DOIs in drafts ignored
CFGK = dict(CFG, anthropic_api_key="k")
m.claude_text = lambda config, prompt, max_tokens=1500, model=None: (
    "TYPE: manuscript\nTITLE: My Real Title\nAUTHOR: Author, Jane\nDATE: 2026-11-03\nPUBLICATION:\nAUTHOR_FROM: byline")
info = m.propose_parent_details(CFGK, "My Draft.docx", facts, "2026-11-18T14:00:00Z")
assert (info["title"], info["authors"], info["date"], info["type"]) == ("My Real Title", ["Author, Jane"], "2026-11-03", "manuscript")
assert info["src"]["author"] == "the document's byline" and info["src"]["date"] == "a date written in the document"
f2 = {"props": {"author": "Microsoft Office User", "last_edited": "2026-11-10", "created": "2026-03-02"}, "text": "Abstract text with no byline. See doi 10.1111/nous.12345 for the view."}
m.claude_text = lambda *a, **k: "TYPE: manuscript\nTITLE: Abstract for X\nDATE:\nAUTHOR_FROM: none"
crossref_calls = []; m.lookup_crossref = lambda doi: crossref_calls.append(doi) or {"title": "SOMEONE ELSE'S PAPER"}
info = m.propose_parent_details(CFGK, "Abstract_X.docx", f2, "2026-11-18T14:00:00Z")
assert info["authors"] == [] and info["date"] == "2026-11-10" and info["src"]["date"] == "when the file was last edited"
assert crossref_calls == [] and info["title"] == "Abstract for X"                          # a draft's cited DOI is NOT used
info = m.propose_parent_details(dict(CFG, anthropic_api_key=""), "Abstract_X.docx", {"props": {"author": "jauthor"}, "text": ""}, "2026-11-18T14:00:00Z")
assert info["authors"] == [] and info["title"] == "Abstract X" and info["date"] == "2026-11-18" and info["src"]["date"] == "when it was added to Zotero"
m.lookup_crossref = lambda doi: {"title": "Real Published Paper", "authors": ["Jane Roe"], "year": "2021", "journal": "Mind", "doi": doi}
info = m.propose_parent_details(CFGK, "paper.pdf", {"props": {}, "text": "Mind, Vol 1. https://doi.org/10.1093/mind/fzab001 Real Published Paper"}, "")
assert info["type"] == "journalArticle" and info["publication"] == "Mind" and "Crossref" in info["src"]["all"]

# full flow with a fake Zotero (claim -> parent -> file moved under it -> sent next check)
m.claude_text = lambda config, prompt, max_tokens=1500, model=None: (
    "TYPE: manuscript\nTITLE: My Real Title\nAUTHOR: Author, Jane\nDATE:\nAUTHOR_FROM: byline")
def standalone(z, key="STANDALN", fname="My Draft.docx", ctype="application/vnd.openxmlformats-officedocument.wordprocessingml.document", colls=("SEND", "PAPERS")):
    z.items[key] = {"key": key, "version": 3, "data": {"key": key, "itemType": "attachment", "title": fname, "filename": fname, "contentType": ctype,
                    "linkMode": "imported_file", "collections": list(colls), "dateAdded": "2026-11-18T13:00:00Z", "tags": []}}
    for c in colls: z.coll.setdefault(c, []).append(key)
DOCX = make_docx(tmpd / "b.docx", author="Jane Author", body=["My Real Title", "Jane Author"])
def wire(z, fail_create=False):
    z.file = lambda k: DOCX if k in z.items and z.items[k]["data"].get("itemType") == "attachment" else b"%PDF-1.4 x" + b"x" * 300
    real_create = z.create_items
    def create(lst):
        if fail_create: raise RuntimeError("Zotero hiccup")
        out = real_create(lst); k = out["successful"]["0"]["key"]; z.items[k]["data"]["collections"] = lst[0]["collections"]
        for c in lst[0]["collections"]:
            if k not in z.coll.setdefault(c, []): z.coll[c].append(k)
        return out
    z.create_items = create
    def api(config, method, path, **kw):
        key = path.rsplit("/", 1)[1]
        if method == "PATCH":
            it = z.items[key]; it["data"].update(kw["json"]); it["version"] += 1
            for c, ks in z.coll.items():
                if key in ks and c not in it["data"]["collections"]: ks.remove(key)
            z.kids.setdefault(kw["json"]["parentItem"], []).append({"key": key, "version": it["version"], "data": it["data"]})
            return types.SimpleNamespace(status_code=204)
        if method == "DELETE":
            z.items.pop(key, None); [ks.remove(key) for ks in z.coll.values() if key in ks]; return types.SimpleNamespace(status_code=204)
    m._zotero_api = api
z = FakeZot(); standalone(z); wire(z)
_tmpl = z.item_template
z.item_template = lambda t: dict(_tmpl(t), creators=[], date="", extra="", publicationTitle="")
rm, tr = setup({"/": {"Zotero Upload": "d"}, U: {}}, z); m._convert_failed.clear(); m._no_pdf_logged.clear()
m.word_to_pdf = lambda src, dst: (Path(dst).write_bytes(b"%PDF-1.4 from word" + b"x" * 300), (True, ""))[1]
m.sync_zotero_to_remarkable(z, CFGK, st(), "rmapi")                                        # check 1: parent made
parents = [k for k, i in z.items.items() if i["data"].get("itemType") == "manuscript"]
assert len(parents) == 1; P = z.items[parents[0]]["data"]
assert P["title"] == "My Real Title" and P["creators"] == [{"creatorType": "author", "firstName": "Jane", "lastName": "Author"}]
assert P["date"] == "2026-11-10" and "date from when the file was last edited" in P["extra"]   # no date written in it
assert set(P["collections"]) == {"SEND", "PAPERS"} and "author from the document's byline" in P["extra"]
assert z.items["STANDALN"]["data"]["parentItem"] == parents[0] and z.items["STANDALN"]["data"]["collections"] == []
assert not any(c[0] == "put" for c in rm.calls)                                             # sent next check, not now
m.sync_zotero_to_remarkable(z, CFGK, st(), "rmapi")                                        # check 2: converted + sent
assert list(rm.t[F]) == [f"Author - 2026 - My Real Title [{parents[0]}]"] and "SEND" in z.coll and parents[0] not in z.coll["SEND"]

# both laptops at once: the second claim is refused -> only one parent
z = FakeZot(); standalone(z); wire(z); z.item_template = lambda t: dict(_tmpl(t), creators=[], date="", extra="", publicationTitle="")
stale = z.item("STANDALN"); z.deletefrom_collection("SEND", z.item("STANDALN"))            # other laptop claimed it
assert m.give_standalone_file_a_parent(z, CFG, stale, "SEND") is None
assert [k for k, i in z.items.items() if i["data"].get("itemType") == "manuscript"] == []
# failure after claiming -> file back in Send Remarkable, nothing half-made left
z = FakeZot(); standalone(z); wire(z, fail_create=True); z.item_template = lambda t: dict(_tmpl(t), creators=[], date="", extra="", publicationTitle="")
assert m.give_standalone_file_a_parent(z, CFG, z.item("STANDALN"), "SEND") is None
assert "STANDALN" in z.coll["SEND"] and [k for k, i in z.items.items() if i["data"].get("itemType") == "manuscript"] == []
# a bare spreadsheet is left alone (logged once)
z = FakeZot(); standalone(z, key="XLSXFILE", fname="budget.xlsx", ctype="application/vnd.ms-excel", colls=("SEND",)); wire(z)
rm, tr = setup({"/": {"Zotero Upload": "d"}, U: {}}, z); m._no_pdf_logged.clear()
m.sync_zotero_to_remarkable(z, CFG, st(), "rmapi")
assert z.coll["SEND"] == ["XLSXFILE"] and len(z.items) == 1
hdr("17 Zotero storage hiccup while downloading a Word file -> retried next check, not given up on")
z = FakeZot(); word_item(z)
rm, tr = setup({"/": {"Zotero Upload": "d"}, U: {}}, z); m._convert_failed.clear(); conversions.clear()
m.word_to_pdf = fake_word
calls_ = {"n": 0}
def flaky_file(k):
    calls_["n"] += 1
    if calls_["n"] == 1: raise Exception("Code: 500\nURL: https://zoterofilestorage.s3...")
    return b"PK fake docx"
z.file = flaky_file
m.sync_zotero_to_remarkable(z, CFG, st(), "rmapi")
assert z.coll["SEND"] == ["WORDITEM"] and conversions == [] and "WORDITEM" not in m._convert_failed
m.sync_zotero_to_remarkable(z, CFG, st(), "rmapi")
assert z.coll["SEND"] == [] and conversions == [".docx"]
print("\nALL TESTS PASSED")
