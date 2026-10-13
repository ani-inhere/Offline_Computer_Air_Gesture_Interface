# Word ops → VBA object model

How each `WordAdapter` op (`agc/engine/automation/adapters/word_com.py`) maps onto the Word object model, so the VBA reference can be used directly. Python calls go through pywin32 late binding, so they look like VBA with positional arguments.

All ops run on the ComWorker thread. Each COM call is retried while Word reports it is busy (`RPC_E_CALL_REJECTED`), and the cancel token is checked before every call.

## Ops

| Op | `args` | VBA equivalent | Python call in the adapter |
|---|---|---|---|
| `new_document` | – | `Set app = GetObject(, "Word.Application")`<br>(if none: `Set app = CreateObject("Word.Application")`)<br>`app.Visible = False` *(only if AGC started Word)*<br>`app.DisplayAlerts = wdAlertsNone` *(only if AGC started Word)*<br>`Set doc = app.Documents.Add` | `win32com.client.GetActiveObject("Word.Application")`<br>`win32com.client.DispatchEx("Word.Application")`<br>`app.Visible = cfg.visible`<br>`app.DisplayAlerts = 0`<br>`app.Documents.Add()` |
| `insert_text` | exactly one of `text`, `snippet_id`, `source: clipboard`; optional `position: end \| selection` | end: `doc.Content.InsertAfter Text:=s`<br>selection: `Selection.InsertAfter s` then `Selection.Collapse wdCollapseEnd` | `doc.Content.InsertAfter(s)`<br>`app.Selection.InsertAfter(s)`; `app.Selection.Collapse(0)` |
| `insert_paragraph` | optional `text`, `position` | `doc.Content.InsertParagraphAfter`<br>`doc.Content.InsertAfter s` | `rng.InsertParagraphAfter()`; `rng.InsertAfter(s)` |
| `set_style` | `style`: Normal, Heading 1, Heading 2, Heading 3; optional `position` | `doc.Paragraphs.Last.Range.Style = wdStyleHeading1`<br>(selection: `Selection.Range.Style = ...`) | `doc.Paragraphs.Last.Range.Style = -2` |
| `export_pdf` | optional `out_dir`, `name_template` | `doc.ExportAsFixedFormat OutputFileName:=path, ExportFormat:=wdExportFormatPDF` | `doc.ExportAsFixedFormat(path, 17)` |
| `save_as_docx` | optional `out_dir`, `name_template` | `doc.SaveAs2 FileName:=path, FileFormat:=wdFormatXMLDocument` | `doc.SaveAs2(path, 12)` |
| `status` | – | `GetObject(, "Word.Application")` (never `CreateObject`)<br>`app.Visible`, `app.Documents.Count`, `app.ActiveDocument.Name` | same members, read only |
| `close_document` *(opt-in: `allow_close: true`)* | optional `name_template` (used if an untitled doc must be saved first) | `If Not doc.Saved Then`<br>&nbsp;&nbsp;`If doc.Path <> "" Then doc.Save Else doc.SaveAs2 path, wdFormatXMLDocument`<br>`doc.Close SaveChanges:=wdSaveChanges` | `doc.Save()` / `doc.SaveAs2(path, 12)`<br>`doc.Close(-1)` |
| *(adapter shutdown)* | – | `For Each d In app.Documents: If Not d.Saved ...`<br>`app.Quit` *(only if AGC started Word and nothing is unsaved; otherwise `app.Visible = True`)* | `app.Documents.Item(i).Saved`<br>`app.Quit()` |

**Target document:** the last document AGC created, if it's still open; otherwise `app.ActiveDocument`. `insert_text` and `insert_paragraph` create a document when none is open. `set_style`, `export_pdf`, `save_as_docx` and `close_document` fail with "no document is open in Word" instead.

**`position: selection`** is honored only when Word is visible and the target is the active document. Otherwise the text goes at the end of the document.

**`close_document`** only closes the document AGC created. It never uses `wdDoNotSaveChanges`.

## Constants used

| VBA constant | Enum | Value |
|---|---|---|
| `wdExportFormatPDF` | `WdExportFormat` | 17 |
| `wdFormatXMLDocument` (.docx) | `WdSaveFormat` | 12 |
| `wdAlertsNone` | `WdAlertLevel` | 0 |
| `wdSaveChanges` | `WdSaveOptions` | -1 |
| `wdDoNotSaveChanges` *(never used)* | `WdSaveOptions` | 0 |
| `wdCollapseEnd` | `WdCollapseDirection` | 0 |
| `wdStyleNormal` / `wdStyleHeading1` / `2` / `3` | `WdBuiltinStyle` | -1 / -2 / -3 / -4 |

Built-in style ids are used instead of names like `"Heading 1"` because style names are localized ("Überschrift 1" in German Word).

## Output files

- Files go only inside `allowed_output_dir` (default `~/Documents/AGC`), which is created if missing. `out_dir` may name a subfolder. Anything that resolves outside the folder (`..`, absolute paths elsewhere, symlinks) is rejected.
- `name_template` placeholders: `{timestamp}` (`20261014-093000`), `{date}`, `{time}`, `{doc}` (document name without extension). Any other `{name}` is an error. Path separators and characters Windows forbids become `_`.
- Existing files are never overwritten: `report.pdf`, `report-1.pdf`, `report-2.pdf` and so on. The name is reserved with an exclusive create before Word writes to it, and removed again if the export fails.

## COM errors → results

| HRESULT | Meaning | Result |
|---|---|---|
| `0x80010001` RPC_E_CALL_REJECTED, `0x8001010A` RPC_E_SERVERCALL_RETRYLATER | Word busy (modal dialog open) | retried with backoff (`max_retries`, default 5), then FAILED "Word is busy ..." |
| `0x80010108` RPC_E_DISCONNECTED, `0x800706BA` RPC_S_SERVER_UNAVAILABLE, … | user closed Word | handles dropped, re-attach, op retried once |
| `0x80040154` REGDB_E_CLASSNOTREG, `0x800401F3`, `0x80080005` | Word not installed or won't start | UNAVAILABLE |
| `0x80020009` DISP_E_EXCEPTION | Word raised an error | FAILED "Word reported: \<Word's description\>" |
| anything else | – | FAILED "COM error 0x…" |

## Snippets

`insert_text` with `snippet_id` reads `agc/config/snippets.yaml` (`snippets_path` in `WordConfig`). Ids are limited to `[A-Za-z0-9_-]{1,64}`. Only `{date}` and `{time}` are filled in; any other braces are inserted as typed.
