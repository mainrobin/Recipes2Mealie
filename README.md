# recipes2mealie

Convert a folder of recipes in mixed formats (PDF, Word, LibreOffice, text, images)
into structured recipes in a self-hosted [Mealie](https://mealie.io) server, using a
**local** language model through [Ollama](https://ollama.com). Your recipes never
leave your own machines.

It was built to move about 1,300 recipes collected over many years (saved web pages,
cookbook PDFs, Word documents, phone screenshots) into Mealie. On that collection it
uploaded 1,303 recipes. A dozen image-only files needed typing in by hand.

## Features

- **Reads mixed formats:** `.pdf` (text, or OCR for scanned PDFs), `.doc`, `.docx`,
  `.odt`, `.rtf` and `.abw` via LibreOffice, `.txt` and extension-less text files, and
  `.jpg`, `.jpeg` and `.png` via OCR.
- **Turns free text into structured recipes:** a local LLM extracts the title,
  ingredients, steps, times, servings, category and tags as
  [schema.org/Recipe](https://schema.org/Recipe) data. It is told to copy ingredients and
  steps faithfully and never invent quantities.
- **Handles collections:** a cookbook or a "10 easy recipes" PDF becomes separate
  recipes. Long PDFs are split on page boundaries.
- **Filters the noise:** website clutter is ignored, and files that are not recipes
  (tips, substitution charts, articles) are left out.
- **Removes duplicates:** byte-identical files are skipped at extraction. At upload,
  recipes whose name already exists in Mealie are skipped and listed for review.
- **Lets you review before uploading:** a CSV report flags anything that looks wrong
  before it reaches Mealie.
- **Keeps the original:** each imported recipe gets its source file attached as a
  Mealie asset, an "Imported from" note and an `imported` tag.
- **Resumes after interruptions:** every stage saves its progress. Stop it at any time
  (Ctrl+C, reboot, crash) and re-run the same command to carry on.

## How it works

The conversion runs in four stages. Each stage reads the previous stage's output from a
working folder (`./work` by default).

```
recipe files ──extract──▶ plain text ──structure──▶ recipe JSON ──upload──▶ Mealie
                          work/text/     (local LLM)  work/json/
                                                          │
                                                       report ──▶ work/report.csv
```

| Stage | What it does | Output |
|---|---|---|
| `extract` | Pulls plain text out of each file, using OCR where needed | `work/text/<id>.txt`, `work/manifest.json` |
| `structure` | Sends each text to Ollama and gets back recipe JSON | `work/json/<id>.json` |
| `report` | Writes a spreadsheet of every file, its status and any warnings | `work/report.csv` |
| `upload` | Posts each recipe to Mealie's import API and attaches the original | `work/uploaded.json`, `work/skipped-duplicates.csv` |

## Requirements

- **Python 3.9+** with the `requests` package.
- **Command-line tools:**
  - `pdftotext` and `pdftoppm` from poppler, for PDFs;
  - `tesseract` with English language data, for OCR;
  - `soffice` from LibreOffice Writer, for Word and LibreOffice files.
- **Ollama**, running locally or on your network, with a model that supports structured
  JSON output. That needs Ollama 0.5 or later; 0.9 or later is needed to switch off
  qwen3's "thinking" mode, which the script does automatically.
- **A GPU is strongly recommended.** About 12 GB of VRAM runs the 8B and 14B models
  well; CPU-only works but is very slow.
- **Mealie** (tested with v2/v3) and an API token.

## Installation

```bash
git clone https://github.com/<you>/recipes2mealie.git
cd recipes2mealie
chmod +x recipes2mealie.py
```

**System packages:**

```bash
# Fedora
sudo dnf install poppler-utils tesseract tesseract-langpack-eng libreoffice-writer

# Debian / Ubuntu
sudo apt install poppler-utils tesseract-ocr tesseract-ocr-eng libreoffice-writer
```

**Python package:**

```bash
pip install requests
```

**Model:**

```bash
ollama pull qwen3:8b
```

**Mealie API token:** in Mealie, go to **Profile → API Tokens → Generate**, then:

```bash
export MEALIE_TOKEN=your-token-here
```

## Usage

### 1. Trial run

Run the whole pipeline on 20 files first, then check the results in Mealie against the
originals:

```bash
./recipes2mealie.py extract ~/Recipes --limit 20
./recipes2mealie.py structure
./recipes2mealie.py report
./recipes2mealie.py upload --mealie http://your-mealie-host:9000
```

### 2. Full run

The full run can take many hours, so run it inside `tmux` or `screen` so it survives a
closed terminal:

```bash
tmux new -s recipes
./recipes2mealie.py extract ~/Recipes
./recipes2mealie.py structure
./recipes2mealie.py report
```

Files already done in the trial are skipped.

### 3. Second pass for cookbooks

Large collections can produce more output than fits in one model answer, so they fail
on the first pass. After the main run, only failed files are left unstructured. Re-run
those with smaller pieces and a longer answer limit:

```bash
./recipes2mealie.py structure --chunk-chars 4000 --max-tokens 4000
./recipes2mealie.py report
```

Don't use the small chunk size for the main run. It would also split long single recipes
(such as web pages with comments) into fragments.

### 4. Review and upload

Open `work/report.csv` in a spreadsheet and review it (see
[Reviewing the report](#reviewing-the-report)). Then upload:

```bash
./recipes2mealie.py upload --mealie http://your-mealie-host:9000
```

Afterwards, delete the API token in Mealie if you don't need it any more.

## Command reference

**Global option** (goes before the stage name):

| Option | Default | Description |
|---|---|---|
| `--work DIR` | `work` | Working folder. Use a different one to test settings side by side, e.g. `--work work-test`. |

**Options for every stage except `report`:**

| Option | Description |
|---|---|
| `--limit N` | Process at most N files this run |
| `--match REGEX` | Only files whose name matches (case-insensitive) |

### `extract SRC`

Scans `SRC` recursively. Hidden files and `files.txt` are ignored.

### `structure`

| Option | Default | Description |
|---|---|---|
| `--model` | `qwen3:8b` | Ollama model name |
| `--ollama` | `http://localhost:11434` | Ollama server URL |
| `--num-ctx` | `8192` | Context window, in tokens. Raise it only if your GPU has spare memory. |
| `--max-tokens` | `3000` | Cap on each answer's length. Stops a model stuck repeating itself. |
| `--chunk-chars` | `12000` | Split documents longer than this. Use about 4000 for cookbooks. |
| `--timeout` | `600` | Seconds to wait for one answer |

If an answer is cut off, invalid or times out, the script retries once with a little
randomness, which usually breaks a repetition loop. If that also fails, the file is
logged as an error and the run continues. Re-running retries only the failures.

### `report`

Writes `work/report.csv` and prints a count per status.

### `upload`

| Option | Description |
|---|---|
| `--mealie URL` | Mealie base URL (required) |
| `--no-attach` | Don't attach the original file to each recipe |
| `--allow-duplicates` | Upload even if a recipe with the same name already exists |

The original file is attached only to single-recipe files of 5 MB or less. To upload one
specific skipped duplicate:

```bash
./recipes2mealie.py upload --mealie http://your-mealie-host:9000 \
  --allow-duplicates --match 'Kung Pao Tofu 2'
```

## Reviewing the report

`work/report.csv` has one row per source file.

**Status values:**

| Status | Meaning |
|---|---|
| `uploaded` | All recipes from this file are in Mealie |
| `structured` | Converted but not uploaded yet, or skipped as a same-name duplicate |
| `not-recipe` | The model found no recipe (tips, charts, articles) |
| `extracted` | Text extracted but conversion failed. Re-run `structure`. |
| `no-text` | Almost no text found, usually an image pasted into a document or an unreadable picture |
| `extract-error` | The file couldn't be read (see the `note` column) |
| `duplicate` | Byte-identical to another file (named in `note`) |
| `empty` | 0-byte file |

**Flags worth checking:**

| Flag | Why |
|---|---|
| `few-ingredients`, `few-steps` | Possibly an incomplete conversion. Compare with the original. |
| `multi-recipe` | A collection. Check the number of recipes looks right. |
| `same-name-elsewhere` | Several versions of the same dish. Only the first is uploaded. |
| `ocr` | Text came from OCR. Watch for misread quantities. |

## Performance and model choice

These figures are from a laptop with an Intel Arc A730M (12 GB), running Ollama through
IPEX-LLM:

| Model | Time per recipe | Notes |
|---|---|---|
| `qwen3:8b` | about 30 s | Accurate enough for this task. Recommended. |
| `qwen3:14b` | about 1–2 min | Slightly cleaner output; about 4× slower |

About 1,280 files took roughly 12 hours with `qwen3:8b`. Avoid code-tuned models such as
`qwen2.5-coder`.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Ollama HTTP 404` | The model isn't installed. Check with `ollama list` and pass `--model` with an exact name from the list. |
| `llama runner process has terminated` | The model plus its context doesn't fit in GPU memory. Use `--num-ctx 8192` or a smaller model. |
| Very slow (about 5 tokens/s on a machine with a discrete GPU) | Ollama is using the integrated GPU. On Intel systems with both, pass only the discrete GPU's `/dev/dri/cardN` and `renderDN` devices into the container (find them with `ls -l /dev/dri/by-path/`). Test speed with `ollama run <model> --verbose "hi"`. |
| `retrying … likely a repetition loop` | Normal. The model started repeating itself, and the retry usually fixes it. |
| A cookbook fails repeatedly | Use the cookbook second pass (`--chunk-chars 4000 --max-tokens 4000`). |
| Many `extract-error` for Word or LibreOffice files | LibreOffice isn't installed, or `soffice` isn't on the PATH. |
| Upload `HTTP 401` | `MEALIE_TOKEN` isn't set, or the token was deleted |
| Upload `HTTP 404` | The Mealie version uses a different import endpoint. The script targets `POST /api/recipes/create/html-or-json`. |

## Limitations

- **Mistakes are possible.** LLM output is usually faithful, but steps or ingredients can
  occasionally be dropped or merged. Spot-check imported recipes, especially long ones
  and OCR'd ones; the original file is attached for comparison.
- **Ingredients are imported as plain text.** Use Mealie's ingredient parser on recipes
  where you want serving scaling and shopping lists to work.
- **Images inside documents are not read**, only text. Image-only documents show as
  `no-text`.
- **OCR is English only** by default. Install other Tesseract language packs and change
  `-l eng` in the script for other languages.
- **Duplicate detection is by recipe name only.** Two different recipes with the same
  title are treated as duplicates. Check `skipped-duplicates.csv`.

## Privacy and copyright

- All processing is local: text extraction on your machine, the LLM in your own Ollama,
  and the upload to your own Mealie.
- The `work/` folder contains the full text of your recipes and your local file paths.
  It is excluded by `.gitignore`; keep it out of any public repository.
- Recipes from books and websites remain the work of their authors. This tool is meant
  for organising your own personal collection.

## License

MIT. See `LICENSE`.
