# Zotero ⇄ reMarkable sync

A background watcher for Windows that moves papers between Zotero and a reMarkable
tablet. Papers go out to the tablet to read. Annotated copies come back to the
same Zotero item, with your handwriting transcribed into a note. It's safe to run
on more than one computer at once.

## The workflow

1. In Zotero, add a paper to the **Send Remarkable** collection.
   It can be a PDF or a Word file (.docx, .doc, .rtf, .odt). A Word file is converted to
   PDF by Microsoft Word, run hidden in the background through PowerShell, so no extra
   Python packages are needed. Macros are never run, and the Word file in Zotero is left
   as it is. If the conversion fails, the item stays in Send Remarkable and the log says
   why; it's retried when the file changes or the watcher restarts.
   A **bare file** dragged straight into Send Remarkable (a PDF or Word file with no Zotero
   item around it) first gets a parent item, like Zotero's own "Create Parent Item", and is
   sent on the following check. The parent's details are filled in as well as the file
   allows:
   - **Title:** the title at the top of the document, then the file's Title property,
     then a tidied file name.
   - **Author:** the document's byline, then the file's Author property if it's a real
     person's name (not "Microsoft Office User" or a login name). Otherwise it's left
     blank rather than guessed.
   - **Date:** a date written in the document, then when the file was last edited, then
     when it was created, then when it was added to Zotero.
   - **Published PDFs:** a PDF with its own DOI near the top of page 1 gets full
     details from Crossref. DOIs inside a Word draft are ignored, since those are works
     it cites.

   The parent's Extra field records where each detail came from, so you know what to
   check. Note that a file's Author property records who created the file, which isn't
   always who wrote the paper.
2. Within about 2 minutes it appears on the tablet in **`/From Zotero`**, named
   `Author - Year - Title [ZOTEROKEY]`, and it leaves Send Remarkable.
3. Read and annotate it on the tablet.
4. When you're done, move it into **`/Zotero Upload`**. Within about 2 minutes:
   - the annotated PDF (ink rendered in, all pages, including inserted note pages)
     is attached to the **original** Zotero item as `… (annotated YYYY-MM-DD).pdf`;
   - the item's older PDF goes to Zotero's trash (recoverable for 30 days), so each
     item keeps **one** authoritative PDF. The exception: a PDF with highlights or
     notes made in Zotero's own reader is kept, because trashing it would delete them;
   - your handwriting is transcribed into a child note tagged `AI-transcribed`;
   - the item gets **3 topical tags**, chosen from **all** of your own tags, with the
     tags you put on the works it cites as a guide to your practice. Up to 2 can be
     new, but each proposed new tag is first checked for near-duplicates of a tag you
     already have (case, accents, plurals, hyphens, little words like "of", and word
     order, so "Theories of Truth" = "truth theory"); a match uses your existing tag.
     Workflow tags such as `read`, `needs-pdf` or `phil-…` are never used;
   - the item is linked as **Related** in Zotero, in both directions, to works in
     your library that **it cites** (found by reading its reference list) and to up
     to 3 works in **the same conversation**. Candidates include items whose notes or
     full text match, and they are weighted by what your handwritten notes focus on;
   - the note lists the tags added and every Related link, with the reason for each;
   - the tablet copy moves to `/Zotero Upload/Imported`.

A document *without* a `[ZOTEROKEY]` suffix, for example one sent to the tablet before
item IDs were added to names, is still returned to its original item if its name has
the form `Author - Year - Title` (or `Author - Title`) and exactly one item in your
library matches it. The match needs the first author's surname, the same year, and the
title word for word. Items this script created itself never count as originals, and
when the match is uncertain the document is treated as new rather than guessed.

Anything else in `/Zotero Upload` *without* a `[ZOTEROKEY]` suffix, such as your own
notebooks or PDFs you added straight to the tablet, comes in as a **new item** in
the import collection. It gets the same PDF and transcription treatment, but no
automatic tags or Related links.

**Repairing an item:** to attach a tablet document to an existing Zotero item,
rename it on the tablet so it ends in `[ITEMKEY]`, then move it into
`/Zotero Upload`.

**Annotating again:** move the copy from `Imported` back to `/Zotero Upload`. The
new render already contains the earlier ink, so it replaces the previous annotated
PDF.

A third, independent path: PDFs dropped into a watched **OneDrive folder** are
imported into Zotero. The script tries to fill in metadata via the DOI or a
Crossref title search, then moves the file to `processed/`.

## Running it on two computers

Each computer has its own `sync_state.json`, and those are never shared. What
keeps two running copies from duplicating work is that each piece of work is
**claimed** first, using an operation only one machine can win:

| Direction | Claim | What the loser sees |
|---|---|---|
| tablet → Zotero | move the document from `/Zotero Upload` to `Imported` | the document is gone from the inbox, so it skips it |
| Zotero → tablet | remove the item from Send Remarkable (the request carries the item's version, so Zotero accepts it once) | Zotero refuses with 412, so it skips the item |

If processing fails after a claim (download, attach or upload), the claim is undone:
the document goes back to the inbox, or the item goes back into Send Remarkable.
The work is then retried on the next cycle.

The OneDrive-drop path has no claim step. If both computers watch the same
OneDrive folder, a dropped PDF can occasionally be imported twice.

## Files

| File | What it is |
|---|---|
| `zotero_remarkable_sync.py` | the watcher (all three sync paths, transcription, claiming) |
| `setup_task.ps1` | fixes the Task Scheduler settings; `-Restart` also reloads the code |
| `sync_config.example.json` | config template; copy it to `sync_config.json` and fill it in |
| `requirements.txt` | Python packages |
| `tests/test_sync_simulation.py` | offline simulation of Zotero and reMarkable (run before deploying changes) |
| `tests/test_enrichment_simulation.py` | offline simulation of the tags + Related-links step |
| `tests/test_single_instance.py` | checks the one-copy-per-folder guard |

Not in the repo, and never commit these: `sync_config.json` (API keys),
`sync_state.json`, `tag_cache.json`, `sync.log`, `staging/`, and `rmapi.exe` (download it separately).

## Setting up a computer

1. **Python and packages.** Use the same interpreter that Task Scheduler will run:
   ```powershell
   C:\Path\To\python.exe -m pip install --upgrade -r requirements.txt
   ```
   This must be the `python.exe` that sits next to the `pythonw.exe` the task
   uses. Installing into a different Python install is the usual cause of the task
   exiting immediately with code 1.
2. **rmapi.** Download the Windows build of [ddvk/rmapi](https://github.com/ddvk/rmapi)
   (this fork is needed for `geta`, which renders annotations into the PDF). Put
   `rmapi.exe` in this folder and run it once by hand to pair it with your
   reMarkable account. It asks for a one-time code, and the pairing is stored in
   your Windows user profile, so do this once per computer.
3. **Config.** Copy `sync_config.example.json` to `sync_config.json` and fill in:
   - **Zotero:** your user ID and an API key with write access (both on
     zotero.org/settings/keys).
   - **Collection keys:** the keys of the import collection and the Send Remarkable
     collection. To find one, open the collection on zotero.org; the key is the
     8-character code in the URL. Alternatively, run
     `python zotero_remarkable_sync.py --setup`, which lists your collections.
   - **OneDrive folder:** the folder to watch for dropped PDFs.
   - **Anthropic API key:** turns on handwriting transcription. Leave it empty to
     turn transcription off.
4. **Test once:**
   ```powershell
   C:\Path\To\python.exe zotero_remarkable_sync.py --once
   ```
   You should see "Connected to Zotero", "Connected to reMarkable Cloud", and
   "Handwriting OCR: enabled".
5. **Run it continuously with Task Scheduler**, from this folder:
   ```powershell
   $action = New-ScheduledTaskAction -Execute "C:\Path\To\pythonw.exe" -Argument "`"$PWD\zotero_remarkable_sync.py`"" -WorkingDirectory "$PWD"
   $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
   $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive
   Register-ScheduledTask -TaskName "ZoteroRemarkableSync" -Action $action -Trigger $trigger -Principal $principal -Description "Continuous Zotero <-> reMarkable sync" -Force
   powershell -ExecutionPolicy Bypass -File .\setup_task.ps1 -Restart
   ```
   `setup_task.ps1` is not optional. Windows' defaults would stop the watcher on
   battery and kill it after 72 hours.

   **On a managed (work) computer** where your everyday account can't create tasks,
   run the block above from an **administrator** PowerShell window. Set the
   principal to your *everyday* account, not the admin account:
   `-UserId "DOMAIN\everydayuser"`. Otherwise the task belongs to the admin account
   and never runs while you're logged in normally. Also replace `$PWD` and
   `$env:...` with the explicit paths and account names.

## Updating the code

Replace `zotero_remarkable_sync.py` on each computer, then load it with:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_task.ps1 -Restart
```

Don't use `Stop-ScheduledTask; Start-ScheduledTask` on one line: the start is
ignored while the old copy is still exiting, which leaves nothing running.
`setup_task.ps1 -Restart` waits for the old copy to exit first. It also stops any
stray copies of the watcher started outside the task, and reports "Exactly one
watcher running". The script itself also refuses to start if another copy is
already running for the same folder, so a second copy can't keep running old code. If the code folder
lives in a synced OneDrive folder, other computers get the new file automatically,
but each one still needs its own restart.

## Re-running tags and links for one item

```powershell
C:\Path\To\python.exe zotero_remarkable_sync.py --enrich ITEMKEY
```

This runs only the tags and Related-links step for an item already in Zotero. It uses
the item's newest annotated PDF (for the reference list) and its existing
transcription notes, and adds a "Tags and links" note. It's safe to run while the
watcher is running.

## Speed

- **Your tag list is saved** in `tag_cache.json` next to the script. Each time it's
  needed, the script asks Zotero only what changed since last time, which is usually
  a single "nothing changed" reply in under a second. A full download happens once a
  week to refresh the usage counts, or if the saved copy is missing or damaged.
- **Cited works are looked up four at a time.** If Zotero signals it's busy, the
  script waits as asked and then retries.

A returned paper usually finishes in a minute or two. The transcription note follows
the annotated PDF.

## Checking on it

- Logs are per computer: `sync.log` in that computer's copy of this folder.
  `Get-Content .\sync.log -Tail 20` shows the latest entries. Zotero and the tablet
  folders look the same from everywhere.
- `Get-ScheduledTaskInfo -TaskName ZoteroRemarkableSync` → `LastTaskResult`:

| Code | Meaning |
|---|---|
| `267009` | running (good) |
| `267011` | has never run (usually the task belongs to the wrong account) |
| `1` | the script crashed at startup; run it with `python.exe` (not `pythonw.exe`) to see the error, which is usually a package missing from that interpreter |
| `2147946720` | Windows refused a start because a copy is already running (harmless) |

## Things learned the hard way

- **rmapi:** plain `rmapi get` downloads the clean original *without* annotations;
  `geta --a` renders them in. `geta --a --n` renders the annotations alone on a
  blank page, which is what the transcription step uses.
- **rmapi `mv`** treats its *source* as a glob pattern. A name containing
  `[ABCD2345]` reads as a character class and matches nothing. The script replaces
  `[ ] * ? \` in the source with `?`.
- **pyzotero:** an old version (1.11) returned "failure" from `attachment_simple`,
  creating items with no PDF. Version 1.16 works. The script checks that a *new* PDF attachment
  actually appeared and doesn't trust the return value.
- **pyzotero clients aren't thread-safe.** The OneDrive watcher thread gets its own
  client. Sharing one caused thousands of intermittent 400 errors.
- **Zotero's search is sometimes slow:** usually about 2 seconds, but it has taken
  over 30 seconds, which is pyzotero's limit. Searches that decide where a document
  goes therefore allow 60 seconds and retry. If Zotero still can't answer, the
  document goes back into the inbox and is retried on the next cycle. A failed
  lookup is never treated as "no match".
- **Replies are one item per line, not JSON.** A quotation mark in a cited title
  once broke a JSON reply and skipped a whole paper's tags and links. Each step
  (citations, tags, same-conversation links) now also fails on its own.
- **Zotero desktop conflict dialog:** if you edit an item on your computer in the
  minutes after the script removed it from Send Remarkable, Zotero desktop asks
  which version to keep. Choose the **online/remote** version. The local one still
  has the item in Send Remarkable, so it would be sent to the tablet again.
- **OneDrive Files On-Demand:** files that exist only in the cloud can't be read by
  some tools until OneDrive downloads them.
- **Windows PowerShell 5.1** shows the log's `✓` and `→` as `âœ“` and `â†’`. The log
  file itself is fine.

## Cost (Anthropic API)

Models are set at the top of the transcription section: `CLAUDE_MODEL`
(`claude-sonnet-5-5`) and `CLAUDE_FAST_MODEL` (`claude-haiku-5-5`, about 20 times
cheaper). If Haiku ever fails, for example because the model is retired, the request
is retried once with Sonnet.

- **Transcription (Sonnet):** each page of the ink-only render is checked locally and
  cropped to the handwriting before sending. A margin note goes out as a small strip
  rather than a full page. Pages with only highlighting, or nothing on them, are not
  sent at all. Cropping affects only what is sent for transcription; the annotated
  PDF in Zotero keeps every full page. Cost scales with how much you wrote: a few
  margin notes cost a fraction of a cent, and a densely written page costs up to
  about a cent.
- **Tags and Related links (returning papers only):** reading the reference list and
  choosing tags use Haiku (your whole tag list goes along, which at Haiku prices is a
  fraction of a cent even for thousands of tags); judging "same conversation" uses
  Sonnet. That comes to roughly 2 cents per returned paper, almost all of it the
  Sonnet step.
