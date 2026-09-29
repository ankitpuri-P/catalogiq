# CatalogIQ — User Guide

This guide takes you from receiving the folder to having CatalogIQ running,
tested and connected to a real LLM. It is written for **Windows 11**, with
macOS/Linux commands where they differ. Allow about 10 minutes, plus about 7 GB of
model downloads if you want the real LLM.

For how it works inside, see `DESIGN.md`. For reference details (all settings,
the API, assumptions), see `README.md`.

---

## 1. What you received

```
catalogiq/
├── catalogiq/          the Python server (pipeline, API, database, LLM providers)
├── static/             the web page (HTML, CSS, JavaScript)
├── tests/              automated tests
├── scripts/
│   ├── smoke.py            end-to-end check against a running server
│   ├── bench_ollama.py     picks the best Ollama model/concurrency for this GPU
│   └── generate_sample.py  regenerates the sample CSV
├── data/
│   └── sample_listings.csv 234 messy product listings, with duplicates
├── README.md           reference documentation
├── DESIGN.md           architecture and design answers
└── USER_GUIDE.md       this file
```

There is **nothing to install from the internet except Python** (and Ollama, if
you want a real LLM). The project uses only Python's standard library. It
contains no API keys or passwords, and needs none.

---

## 2. Unpack the folder

1. Save `catalogiq.zip` somewhere simple, e.g. `C:\Users\<you>\Projects\`.
2. Right-click it, choose **Extract All…**, then **Extract**.
3. Open the extracted folder. It should contain `README.md` and the
   `catalogiq`, `static` and `tests` folders directly inside it.

> Avoid running it from inside the zip, or from a OneDrive-synced folder if you
> can. OneDrive can lock the database file while syncing.

---

## 3. Install Python (once)

You need **Python 3.10 or newer**. Check first:

1. Press **Start**, type `PowerShell` and open **Windows PowerShell** (or
   **Terminal**).
2. Run:

   ```powershell
   py --version
   ```

   If it prints `Python 3.10` or higher, skip to step 4.

Otherwise, install it:

```powershell
winget install Python.Python.3.13
```

Or download the installer from https://www.python.org/downloads/. If you use
the installer, tick **"Add python.exe to PATH"** on its first screen.

**Close PowerShell and open a new window**, then run `py --version` again to
confirm.

*macOS/Linux:* use `python3 --version`. Wherever this guide says `py`, type
`python3` instead.

---

## 4. Open a terminal in the project folder

In PowerShell, `cd` into the extracted folder, i.e. the folder that contains
`README.md`:

```powershell
cd C:\Users\<you>\Projects\catalogiq
dir
```

You should see `catalogiq`, `data`, `scripts`, `static`, `tests`, `README.md`
and so on. **Every command below is run from this folder.**

> Shortcut: in File Explorer, open the folder, right-click an empty space and
> choose **Open in Terminal**.

---

## 5. Run the automated tests

```powershell
py -m unittest discover -s tests -v
```

Expected: a list of test names each ending in `ok`, then:

```
Ran 44 tests in 3.1s

OK
```

If anything says `FAIL` or `ERROR`, stop here and see **Troubleshooting**
(section 12).

---

## 6. Start the server (mock LLM)

```powershell
py -m catalogiq
```

Expected output:

```
... INFO catalogiq: CatalogIQ on http://localhost:8000 (provider=mock, concurrency=5)
```

Leave this window open while you use the app, because the server runs in it.
Each request shows up as a log line there.

The **mock LLM** is a built-in fake. Each call takes 200 ms, 10% of calls fail
at random (to exercise retries), and it cleans titles with simple rules. It is
what the assignment is graded with.

---

## 7. Use the web page

Open **http://localhost:8000** in your browser.

### 7.1 Upload listings
1. Under **Upload listings**, click **Choose file** and pick
   `data\sample_listings.csv`.
2. Click **Start job**.
3. A progress bar appears, with **Done**, **Failed**, **Cache hits** and
   **Total** counts. It updates once a second. With the sample file it
   finishes in about 8 seconds.

   - **Cache hits** about 72: duplicate listings reused an earlier result
     instead of calling the LLM again.
   - **Failed** is usually 0, because random mock failures get retried up to
     3 times.

### 7.2 Browse the catalogue
- **Search:** type e.g. `amul`. Results update after you pause typing.
- **Category:** pick one from the dropdown to filter.
- **Previous / Next:** page through the results, 20 per page.

### 7.3 Review a product
1. Click any product in the list.
2. A dialog shows the **raw** listing on the left and the **cleaned** version
   on the right.
3. Edit the title, category or tags (comma separated, up to 5).
4. Click **Save & approve**. The product's status badge changes to
   `approved`.
5. **Close** or **Esc** closes the dialog without saving.

Failed products show their error message in red at the top of this dialog.

### 7.4 Keyboard and phone
- Everything works with **Tab**, **Shift+Tab**, **Enter** and **Esc**.
- For a phone-sized view, press **F12** in the browser, click the device icon,
  and set the width to **360**.

---

## 8. Run the end-to-end check

With the server still running, open a **second** PowerShell window in the same
folder and run:

```powershell
py scripts\smoke.py
```

It uploads the sample CSV, follows the job to completion, then checks
de-duplication, approval and all the error responses. It should end with:

```
ALL CHECKS PASSED
```

---

## 9. Try different settings

Settings are environment variables. In PowerShell, they apply to the current
window only. Stop the server with **Ctrl+C**, set them, then start it again:

```powershell
$env:LLM_CONCURRENCY   = "2"     # at most 2 LLM calls at once
$env:MOCK_LATENCY_MS   = "500"   # each mock call takes 0.5 s
$env:MOCK_FAILURE_RATE = "0.5"   # half of all calls fail
py -m catalogiq
```

Things to observe:
- `http://localhost:8000/api/metrics` should show
  `max_concurrent_llm_calls` never higher than `LLM_CONCURRENCY`.
- With a high failure rate, some products end up **failed**. Open one to see
  its error.

To go back to the defaults, close the window and open a new one, or run:

```powershell
Remove-Item Env:LLM_CONCURRENCY, Env:MOCK_LATENCY_MS, Env:MOCK_FAILURE_RATE
```

*macOS/Linux:* put the variables in front of the command, e.g.
`LLM_CONCURRENCY=2 MOCK_LATENCY_MS=500 python3 -m catalogiq`.

---

## 10. Use a real LLM (Ollama)

CatalogIQ supports a real local LLM through Ollama. For this submission, the
end-to-end model test used **`qwen2.5:3b`**. The development machine did not have
a usable NVIDIA driver, but Ollama successfully used the **Intel HD Graphics 630
through its Vulkan backend** for this small model.

### 10.1 Install and start Ollama

Install Ollama from its official installer, then verify it is reachable:

```powershell
ollama --version
ollama serve
```

Leave `ollama serve` running in its own terminal.

### 10.2 Download a small model

For the tested setup:

```powershell
ollama pull qwen2.5:3b
ollama run qwen2.5:3b "Say hello"
ollama ps
```

The exact `PROCESSOR` column depends on the local Ollama backend. On the
validation machine it showed the model loaded through the Vulkan GPU backend.

The application default is `qwen2.5:7b`, but smaller hardware should select a
smaller model explicitly. `llama3.2:3b` is another small local option.

### 10.3 Start CatalogIQ with the real provider

In a new PowerShell window, from the project folder:

```powershell
cd C:\Users\<you>\Projects\catalogiq
$env:LLM_PROVIDER    = "ollama"
$env:OLLAMA_MODEL    = "qwen2.5:3b"
$env:LLM_CONCURRENCY = "1"
py -m catalogiq
```

Expected startup lines look like:

```text
... INFO catalogiq: loading qwen2.5:3b into memory...
... INFO catalogiq: CatalogIQ on http://localhost:8000 (provider=ollama, concurrency=1)
... INFO catalogiq: qwen2.5:3b loaded
```

The real-LLM path is substantially slower on the validation machine than mock mode,
so test with a small CSV first. A single-product end-to-end check succeeded before
this submission was finalized.

### 10.4 Optional local benchmark

The repository includes `scripts\bench_ollama.py` to compare model/concurrency
settings on the current machine:

```powershell
py scripts\bench_ollama.py --n 10 --models qwen2.5:3b
```

Use the measured settings rather than assuming a GPU from another machine will
behave the same way.

### 10.5 Switch back to mock mode

Stop CatalogIQ with **Ctrl+C**, then in a new PowerShell window run:

```powershell
$env:LLM_PROVIDER = "mock"
py -m catalogiq
```

## 11. Crash recovery demo (optional)

The automated test suite covers partial job recovery. This manual demo can be used
to observe the same persistence behavior with a real running server.

1. Start with slow mock calls:

   ```powershell
   $env:MOCK_LATENCY_MS = "1000"
   py -m catalogiq
   ```

2. Upload `data\sample_listings.csv` in the browser.
3. When the progress bar is partway, open a second PowerShell window and
   hard-kill the server:

   ```powershell
   Stop-Process -Force -Id (Get-NetTCPConnection -LocalPort 8000 -State Listen).OwningProcess[0]
   ```

4. In the first window, run `py -m catalogiq` again. The log shows
   `resumed N pending items from unfinished jobs`, and the job finishes
   without redoing items that were already done. The same partial-resume behavior
   is covered deterministically by `tests/test_pipeline.py`.

---

## 12. Troubleshooting

| Symptom | Fix |
|---|---|
| `py` is not recognized | Python isn't installed, or the terminal was opened before installing. Install it (section 3) and open a **new** window. `python` may work instead of `py`. |
| `No module named catalogiq` | You're in the wrong folder. `cd` to the folder that contains `README.md` (section 4). |
| `cannot listen on localhost:8000 ... is another copy already running?` | CatalogIQ is already running in another window. Close that one, or run on another port: `$env:PORT = "8001"` then open http://localhost:8001. |
| `configuration error: LLM_CONCURRENCY must be an integer` (or similar) | A setting has an invalid value. Fix it, or open a new window to clear all settings. |
| Browser shows `{"error": "host not allowed"}` | Use `http://localhost:8000` or `http://127.0.0.1:8000`. For safety the server refuses any other address, including the machine's network name or IP. |
| Page loads but nothing happens on upload | The CSV must have a header row with at least `sku` and `raw_title` columns. `raw_description` is optional. |
| `nvidia-smi` not found, or driver below 570 | Update the NVIDIA driver (step 10.1) and reboot. |
| `ollama ps` shows CPU instead of `100% GPU` | The model doesn't fit or the driver is too old. Close GPU-heavy apps, use `llama3.2:3b`, or check the driver. Plug in the charger. |
| Ollama items fail with `ollama request failed: TimeoutError` | The GPU is overloaded or the model spilled to CPU. Lower `LLM_CONCURRENCY` to 1, or use `llama3.2:3b`. As a last resort, raise `$env:OLLAMA_TIMEOUT_S = "120"`. |
| With Ollama, every product **fails** with `ollama request failed` | Ollama isn't running. Start it from the Start menu, or run `ollama serve`, and check that `ollama run qwen2.5:3b "hi"` works. |
| With Ollama, failures say `ollama returned HTTP 404` | The model isn't downloaded. The error shows the exact `ollama pull ...` command to run. |
| Ollama is very slow | Check `ollama ps` says `100% GPU` and the charger is plugged in. Then try `llama3.2:3b`. |
| Want to start from an empty catalogue | Stop the server and delete `catalogiq.db`, `catalogiq.db-wal` and `catalogiq.db-shm` in the project folder. The cache is in that file too, so cache hits reset. |
| Test failures mentioning `PermissionError` or a locked file | Antivirus or OneDrive is scanning the temp folder. Move the project out of OneDrive, or re-run the tests. |

---

## 13. Stopping

Press **Ctrl+C** in the server window. Data (products, jobs and the cache) is
kept in `catalogiq.db` and is still there the next time you start. Any job that
was still running when you stopped picks up where it left off.

---

## Quick reference

| Task | Windows (PowerShell) | macOS / Linux |
|---|---|---|
| Run tests | `py -m unittest discover -s tests -v` | `python3 -m unittest discover -s tests -v` |
| Start (mock) | `py -m catalogiq` | `python3 -m catalogiq` |
| Start (Ollama) | `$env:LLM_PROVIDER="ollama"; $env:LLM_CONCURRENCY="2"; py -m catalogiq` | `LLM_PROVIDER=ollama LLM_CONCURRENCY=2 python3 -m catalogiq` |
| Pick best model for this GPU | `py scripts\bench_ollama.py` | `python3 scripts/bench_ollama.py` |
| End-to-end check | `py scripts\smoke.py` | `python3 scripts/smoke.py` |
| Web page | http://localhost:8000 | http://localhost:8000 |
| Health / metrics | http://localhost:8000/api/health, `/api/metrics` | same |
| Stop | Ctrl+C | Ctrl+C |
