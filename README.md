# Cafe Replenishment

> A browser-based decision aid that tells an independent café how much of each ingredient to buy before tomorrow's demand is known — and never places or pays for an order without the manager's explicit approval.

---

## Team Information

| Field                | Details                                |
| ---------------------| ---------------------------------------|
| **Tean Name**        | CodeFirst                              |
| **Team Domain**      | Domain 2                               |
| **SDG Track (1-17)** | SDG 12                                 |
| **Team Leader**      | Fwn Jiarui                             |
| **University**       | Xiamen University Malaysia             |
| **Number of Members**| 5                                      |

 ## Links

|Resource              | Link                                   |
| ---------------------| ---------------------------------------|
| GitHub Repository    |  https://github.com/wanblairr/code-first.git                                      |
| Replit Project       |  https://replit.com/@fanjiarui975/cafe-replenishment-v37-sdc2026                                      |
| Live Demo            |  https://www.cafe-replenishment-demo.com                                      |
| RunPod Endpoint      |  https://api.runpod.ai/v2/nyx4zh6l53tlh4                                      |
| Slide Deck           |  https://docs.google.com/presentation/d/1ydphny-VpOXl_yy1LX5OH6iu_LI5TUr0BjizHWDaeYc/edit?usp=sharing                                      |
| Deno Video           |  https://youtube.com/shorts/75G3trUX0No?si=N73FmfBbZ-0buI5X                                      |

---

## How to Run the Project

There are two ways in. Reviewers who only want to see the product should use the live demo; the local workbench is what the Python source in this repository actually runs.

### Option 1 — Live demo (no installation, no login)

Open **https://www.cafe-replenishment-demo.com/demo/**

Everything runs in the browser. Nothing is uploaded and no account is needed:

1. Keep or edit the simulated café data.
2. Press **Calculate suggestions** — the demand estimate and the purchase suggestion appear.
3. Adjust the quantities, then press **Manager approved – create order**.
4. A purchase draft and an activity log entry are generated at the bottom of the page.

All data in the public demo is simulated and stored in your browser profile; *Reset* clears it. The demo never contacts a supplier and never places or pays for an order.

### Option 2 — Local Python workbench

**Requirements**

- Python **3.11 or newer**, with Tkinter (the standard Windows and python.org macOS builds include it)
- [Ollama](https://ollama.com) running locally with the model **`llama3.1:8b`** — the workbench connects to `http://127.0.0.1:11434`
- One Python dependency: `openpyxl==3.1.5` (Excel import/export only)

```bash
git clone https://github.com/wanblairr/code-first.git
cd code-first

python -m pip install -r requirements.txt
ollama pull llama3.1:8b            # one-off, several GB

python src/cafe_ollama_stock.py doctor   # checks the Ollama service and the model
python src/cafe_ollama_stock.py gui      # opens the desktop workbench
```

On Windows you can double-click `start.cmd` instead of the last command.

`doctor` prints a small JSON health object. If it fails, Ollama is not reachable at `127.0.0.1:11434` or the model has not been pulled — the program will not silently substitute another model.

**First run inside the GUI**

1. *Input & forecast* — import `examples/cafe_input.xlsx`, or edit the simulated defaults directly.
2. Confirm **Check model** succeeds, then start the **10-run AI estimate**. CPU inference is slow; each request times out after 180 s with at most one retry.
3. *Results & approval* — read the cup estimate, the ten individual runs and their spread.
4. Switch the simulated role to `owner`, tick the acknowledgement, and only then approve the purchase. The `operator` role cannot approve.
5. *Orders & records* — confirm a simulated receipt. Approval does not add stock; only a confirmed receipt does.
6. Export to Excel to keep the input snapshot, all ten runs, the approval and the activity log.

### Option 3 — Command line only (no GUI)

```bash
# write a blank Excel input template
python src/cafe_ollama_stock.py template --out examples/cafe_input.xlsx

# run one forecast; .json or .xlsx output
python src/cafe_ollama_stock.py predict examples/input.json --out runtime/result.xlsx

# time-ordered backtest against synthetic cases (14 cases ≈ 140 model calls)
python src/cafe_ollama_stock.py evaluate examples/synthetic_backtest_cases.json --limit 14

# optional bias calibration (needs ≥ 30 paired predictions/results)
python src/cafe_ollama_stock.py calibrate runtime/live_evaluation_40.json --out runtime/calibration.json
```

### Tests

```bash
python run_tests.py
```

**52 tests, about 2 seconds, no network and no model required.** The suite uses a stand-in HTTP server and a simulated model, so a pass means the program behaves as specified — it is **not** a measure of forecast accuracy. Results are written to `reports/engineering_tests.txt` and `reports/engineering_summary.json`.

---

## Overall Project Structure

```
code-first/
├── src/                        all application source code
│   ├── cafe_ollama_stock.py    entry point — GUI and CLI (gui, doctor, template, predict, evaluate, calibrate)
│   ├── cafe_core.py            model client, 10-run aggregation, purchase arithmetic, approval + audit store
│   ├── cafe_gui.py             Tkinter desktop workbench
│   ├── cafe_io.py              Excel / JSON import and export
│   ├── cafe_eval.py            time-ordered backtest and bias calibration
│   ├── make_examples.py        regenerates the synthetic example data (fixed seeds)
│   ├── setup_owner_password.py creates a salted local password verifier
│   └── verify_package.py       checks files against a SHA-256 manifest
├── tests/test_cafe.py          52 unit and interface tests
├── examples/                   input template, history and synthetic backtest cases
├── reports/                    engineering test output and acceptance notes
├── public_site/                generator for the deployed public pages
├── verification/               offline acceptance and route smoke-check scripts
├── original/                   preserved baseline script referenced by NOTICE
├── patches/                    version-to-version diffs and full sources
├── 1_to_2 2/  3_to_4/  4_to_5/ archived sources for each later version transition
├── run_tests.py                test entry point
├── start.cmd                   Windows launcher
├── requirements.txt            openpyxl==3.1.5
├── LICENSE                     Apache License 2.0
└── NOTICE                      attribution and third-party notes
```

### How the pieces fit

| Layer | Files | Responsibility |
| --- | --- | --- |
| **Model layer** | `src/cafe_core.py` | Sends the same structured prompt ten times with different seeds, validates every reply against a JSON schema, rejects fabricated evidence, drops the two highest and two lowest results and averages the remaining six. |
| **Business arithmetic** | `src/cafe_core.py` | `estimated usage = cups × grams per cup ÷ 1000`; `gap = max(0, usage + safety stock − available stock − confirmed inbound)`; rounds up to whole supplier packs and **blocks** approval when a storage, per-order or delivery-date constraint is violated instead of quietly clamping the number. |
| **Persistence & audit** | `src/cafe_core.py` (`AuditStore`, SQLite in `runtime/`) | Stores every run, approval and receipt as an append-only event list; approval requires an `owner` role and an explicit acknowledgement. |
| **Interfaces** | `src/cafe_gui.py`, `src/cafe_io.py`, `src/cafe_ollama_stock.py` | Desktop GUI, Excel/JSON round-trip, CLI sub-commands. |
| **Evaluation** | `src/cafe_eval.py` | Time-ordered backtest, where the model only ever sees information available at that date, plus an optional calibration that must beat the uncalibrated model and both baselines on a held-out date range before it may be used. |
| **Public demo** | `public_site/` | Generates the static pages served at the live-demo URL: page shell plus privacy and terms text in four languages, the recipe/BOM tables, the synthetic demand model shipped to the browser, and the disclosure strings. |

**Notes**

- `runtime/` is created on first run and is not committed. Generated state never lands inside `src/`.
- `public_site/` holds the generator for the deployed pages; the generated output is deployed to Netlify and is not committed.
- `original/cafe_ollama_stock.py` is the single-file prototype the build started from, kept unmodified for traceability and credited in `NOTICE`.

---

## How You Used Replit & RunPod

Both platforms are named in the SDC rules as required tools, so this section states exactly what each was used for — and, just as importantly, what it was **not** used for.

### Replit — the development environment

| | |
| --- | --- |
| **Platform** | Replit Core, the SDC-provided allocation |
| **Role** | Where the Python workbench was written, reviewed and debugged |
| **Not its role** | Production hosting — the public demo is not served by Replit |

What actually ran inside the Replit Project:

- The workbench modules in this repository — `cafe_core.py`, `cafe_io.py`, `cafe_eval.py`, `cafe_gui.py`, `cafe_ollama_stock.py` — were edited, reviewed and corrected there during the hacking period.
- **Replit Agent** was used for code review and bug fixing on that code base: the purchase-order date basis, catch-up handling for the nightly job, and the client for OpenAI-compatible endpoints.
- The SDC credit allocation was consumed during this work; the account shows the credit-exhausted state.

**Why the demo is not hosted on Replit.** The workbench is a Tkinter desktop GUI, which cannot be served as a Replit web app. The public demo is a static HTML/CSS/JS build and is deployed to Netlify at the live-demo URL above. Replit is therefore the build environment and Netlify is the runtime — neither claim is stretched to cover the other.

**Evidence:** `SDC2026_CodeFirst_Replit_POU.pdf` — screenshots of the Replit Agent session on this code base, and the credit-exhausted screen after the allocation was used. The Replit Project itself is kept private; the POU screenshots are the reviewable record.

### RunPod — GPU inference

| | |
| --- | --- |
| **Platform** | RunPod, the SDC-provided GPU credit |
| **Role** | Running the open-source model on a GPU for the optional advisory path |
| **Not its role** | Training or fine-tuning — no weights were trained |

What was actually run:

- A GPU pod — **`extreme_ivory_sheep`**: 1× GPU, 24 vCPU, 251 GB RAM, region **US-CA-2** (Secure Cloud), image `runpod/pytorch:1.4.1-cu1281-torch280-cu1284`, billed at **US$7.99/hr**, with JupyterLab exposed on port 8888. The pod was used to load and test the open-source model that a visitor may select for the advisory review.
- A **Serverless endpoint** — `https://api.runpod.ai/v2/nyx4zh6l53tlh4` — is listed in the Links table above. An unauthenticated request returns `HTTP 401 "no token provided"`, which confirms the endpoint exists and is access-controlled rather than open.
- **How the application uses it.** Set `CAFE_OLLAMA_URL` to the pod's Ollama proxy and `CAFE_INFERENCE_MODE=runpod_gpu`; the workbench then sends the identical `/api/generate` calls it would have sent to a local Ollama, so the GPU and CPU paths share one code path and one prompt. The core replenishment workflow needs no GPU — it runs on CPU, and the default configuration forces CPU (`num_gpu=0`) so that no credit is spent by accident.

```bash
export CAFE_OLLAMA_URL="https://<pod-id>-11434.proxy.runpod.net"
export CAFE_INFERENCE_MODE="runpod_gpu"
export CAFE_MODEL="llama3.1:8b"
export CAFE_OLLAMA_TOKEN="<gateway token, if your endpoint is protected>"
python src/cafe_ollama_stock.py doctor
```

**Boundaries we are not blurring**

- No model was trained or fine-tuned on RunPod. The ten-run estimate is repeated inference, not learning.
- No API key, token or endpoint secret is stored in this repository; the token is read from an environment variable.
- RunPod is not on the critical path of the demo: if it is unavailable, the workflow still completes on CPU.

**Evidence:** `SDC2026_CodeFirst_Runpod_POU.pdf` — pod details (GPU, vCPU, memory, region, hourly rate), the runtime view and the Connect tab showing the exposed service.
