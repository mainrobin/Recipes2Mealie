#!/usr/bin/env python3
"""
recipes2mealie.py - convert a folder of mixed-format recipe files into Mealie recipes.

Pipeline (every stage is resumable: re-running skips work already done):

  extract    recipe files -> plain text                  work/text/<id>.txt
  structure  plain text   -> recipe JSON via local LLM   work/json/<id>.json
  report     review spreadsheet of what was found        work/report.csv
  upload     recipe JSON  -> Mealie API                  work/uploaded.json

Handles: .pdf (text, or OCR if scanned), .doc/.docx/.odt/.rtf/.abw (via LibreOffice),
.txt and extension-less text files, .jpg/.jpeg/.png (OCR). Exact duplicate files and
empty files are skipped. Collections (one file, many recipes) are split into recipes.

Setup (Fedora):
  sudo dnf install poppler-utils tesseract tesseract-langpack-eng libreoffice-writer
  pip install requests

Trial run on 20 files:
  ./recipes2mealie.py extract ~/Recipes --limit 20
  ./recipes2mealie.py structure
  ./recipes2mealie.py report          # open work/report.csv and check
  export MEALIE_TOKEN=...             # Mealie: Profile > API Tokens
  ./recipes2mealie.py upload --mealie http://your-mealie-host:9000

Then run the same four commands without --limit for the rest.
"""
import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import requests

PDF_EXT = {".pdf"}
OFFICE_EXT = {".doc", ".docx", ".odt", ".rtf", ".abw"}
IMG_EXT = {".jpg", ".jpeg", ".png"}
TEXT_EXT = {".txt", ""}
ALL_EXT = PDF_EXT | OFFICE_EXT | IMG_EXT | TEXT_EXT
SKIP_NAMES = {"files.txt"}
MIN_PDF_TEXT = 150            # chars; less than this means a scanned PDF -> OCR
OCR_MAX_PAGES = 15
CHUNK_CHARS = 12000           # long documents are sent to the LLM in chunks
MAX_ATTACH_BYTES = 5 * 1024 * 1024

CATEGORIES = ["Breakfast", "Main", "Soup", "Salad", "Side", "Snack", "Dessert",
              "Baking", "Drink", "Sauce & Condiment"]

RECIPE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "description": {"type": "string"},
        "recipeYield": {"type": "string"},
        "prepTime": {"type": "string"},
        "cookTime": {"type": "string"},
        "totalTime": {"type": "string"},
        "recipeIngredient": {"type": "array", "items": {"type": "string"}},
        "recipeInstructions": {"type": "array", "items": {"type": "string"}},
        "recipeCategory": {"type": "array", "items": {"type": "string"}},
        "keywords": {"type": "array", "items": {"type": "string"}},
        "recipeCuisine": {"type": "string"},
        "url": {"type": "string"},
    },
    "required": ["name", "recipeIngredient", "recipeInstructions"],
}
SCHEMA = {"type": "object",
          "properties": {"recipes": {"type": "array", "items": RECIPE_SCHEMA}},
          "required": ["recipes"]}

PROMPT = """You convert recipe documents into structured data.
The text below was extracted from the file "{fname}". It may contain website clutter
(navigation, ads, comments, ratings) or OCR errors; ignore those.

Rules:
- recipes: every distinct recipe in the text. Usually one; cookbooks and collections may
  contain several. Return an empty list if the text is not a recipe (tips, articles,
  substitution charts, health notes).
- Copy ingredients and steps faithfully. Never invent quantities, ingredients or steps.
  Keep the original units. Fix obvious OCR typos only.
- recipeIngredient: one ingredient per item, e.g. "1 cup red lentils, rinsed".
  Sub-headings such as "For the sauce:" become their own item ending in a colon.
- recipeInstructions: one step per item, without leading step numbers.
- name: the recipe title, in Title Case, without the website name.
- description: one short sentence, or "" if none is given.
- prepTime/cookTime/totalTime: ISO 8601 durations like "PT15M" or "PT1H10M", or "".
- recipeYield: e.g. "4 servings", or "".
- recipeCategory: one or more of: {categories}.
- keywords: 2-6 short lowercase tags, e.g. "curry", "air fryer", "tofu", "gluten free".
- url: the source web address if it appears in the text, else "".

TEXT:
{text}"""


# ---------------------------------------------------------------- state helpers

def load(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def dump(path, obj):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False))
    tmp.replace(path)


class Work:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.text = self.root / "text"
        self.json = self.root / "json"
        for d in (self.text, self.json):
            d.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / "manifest.json"
        self.uploaded_path = self.root / "uploaded.json"
        self.manifest = load(self.manifest_path, {})
        self.uploaded = load(self.uploaded_path, {})

    def save(self):
        dump(self.manifest_path, self.manifest)
        dump(self.uploaded_path, self.uploaded)


def run(cmd, timeout=300):
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                          timeout=timeout)


def select(items, args):
    """Apply --match / --limit to a list of (id, manifest-entry) pairs."""
    if args.match:
        items = [(k, m) for k, m in items if re.search(args.match, m["rel"], re.I)]
    return items[: args.limit] if args.limit else items


# ---------------------------------------------------------------- 1. extract

def office_to_text(path, work):
    profile = work.root / "lo-profile"   # private profile: works even if LibreOffice is open
    with tempfile.TemporaryDirectory() as td:
        r = run(["soffice", f"-env:UserInstallation=file://{profile}", "--headless",
                 "--convert-to", "txt:Text (encoded):UTF8", "--outdir", td, str(path)],
                timeout=240)
        outs = list(Path(td).glob("*.txt"))
        if not outs:
            raise RuntimeError(f"LibreOffice conversion failed: {r.stderr.strip()[:200]}")
        return outs[0].read_text(errors="replace")


def ocr_pdf(path):
    with tempfile.TemporaryDirectory() as td:
        run(["pdftoppm", "-r", "300", "-gray", "-png", "-l", str(OCR_MAX_PAGES),
             str(path), f"{td}/p"], timeout=600)
        pages = sorted(Path(td).glob("p*.png"))
        return "\n\n".join(run(["tesseract", str(p), "-", "-l", "eng"]).stdout for p in pages)


def extract_text(path, data, work):
    ext = path.suffix.lower()
    if ext in PDF_EXT:
        text = run(["pdftotext", str(path), "-"]).stdout
        if len(text.strip()) >= MIN_PDF_TEXT:
            return text, "pdf"
        return ocr_pdf(path), "pdf-ocr"
    if ext in IMG_EXT:
        return run(["tesseract", str(path), "-", "-l", "eng"]).stdout, "image-ocr"
    if ext in OFFICE_EXT or data[:2] == b"PK" or data[:4] == b"\xd0\xcf\x11\xe0":
        return office_to_text(path, work), "office"
    try:
        return data.decode("utf-8"), "text"
    except UnicodeDecodeError:
        return data.decode("latin-1"), "text"


def cmd_extract(args, work):
    src = Path(args.src).expanduser().resolve()
    files = sorted(p for p in src.rglob("*")
                   if p.is_file() and p.suffix.lower() in ALL_EXT
                   and p.name not in SKIP_NAMES and not p.name.startswith("."))
    if args.match:
        files = [p for p in files if re.search(args.match, p.name, re.I)]
    known = {m["path"] for m in work.manifest.values()}
    new = [p for p in files if str(p) not in known]
    if args.limit:
        new = new[: args.limit]
    seen = {m["sha"]: k for k, m in work.manifest.items()
            if m["status"] not in ("duplicate", "empty")}

    for i, path in enumerate(new, 1):
        rel = str(path.relative_to(src))
        fid = hashlib.sha1(rel.encode()).hexdigest()[:12]
        data = path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        entry = {"path": str(path), "rel": rel, "size": len(data), "sha": sha}
        if not data:
            entry["status"] = "empty"
        elif sha in seen:
            entry["status"] = "duplicate"
            entry["dup_of"] = work.manifest[seen[sha]]["rel"]
        else:
            seen[sha] = fid
            try:
                text, kind = extract_text(path, data, work)
                (work.text / f"{fid}.txt").write_text(text)
                entry["kind"] = kind
                entry["chars"] = len(text.strip())
                entry["status"] = "extracted" if entry["chars"] >= 40 else "no-text"
            except Exception as e:  # keep going; the report lists failures
                entry["status"] = "extract-error"
                entry["error"] = str(e)[:300]
        work.manifest[fid] = entry
        print(f"[{i}/{len(new)}] {entry['status']:<13} {rel}")
        if i % 10 == 0:
            work.save()
    work.save()


# ---------------------------------------------------------------- 2. structure

def clean(text):
    text = re.sub(r"[ \t]+\n", "\n", text)       # keep \f: it marks PDF page breaks
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def chunks(text, size):
    """Split long text for the model. PDFs are split on page breaks (cookbook recipes
    usually sit on whole pages), other text on blank lines."""
    if len(text) <= size:
        yield text.replace("\f", "\n")
        return
    units = text.split("\f") if "\f" in text else text.split("\n\n")
    parts = []
    for u in units:
        if len(u) <= size:
            parts.append(u)
        else:                                     # one huge page: fall back to paragraphs
            parts += u.split("\n\n")
    buf = ""
    for part in parts:
        if buf and len(buf) + len(part) > size:
            yield buf
            buf = ""
        buf += part + "\n\n"
    if buf.strip():
        yield buf


def llm_once(args, fname, text, temperature):
    body = {
        "model": args.model,
        "stream": False,
        "format": SCHEMA,
        "messages": [{"role": "user", "content": PROMPT.format(
            fname=fname, text=text, categories=", ".join(CATEGORIES))}],
        # num_predict caps the answer length, so a model stuck repeating itself
        # stops instead of generating until the request times out.
        "options": {"temperature": temperature, "num_ctx": args.num_ctx,
                    "num_predict": args.max_tokens, "repeat_penalty": 1.1},
    }
    if re.match(r"(qwen3|deepseek-r1)", args.model):
        body["think"] = False
    r = requests.post(f"{args.ollama.rstrip('/')}/api/chat", json=body,
                      timeout=args.timeout)
    if not r.ok:
        raise RuntimeError(f"Ollama HTTP {r.status_code}: {r.text[:200]}")
    content = r.json()["message"]["content"]
    try:
        return json.loads(content).get("recipes", [])
    except json.JSONDecodeError:
        raise RuntimeError(f"model output was cut off or invalid JSON "
                           f"({len(content)} chars, likely a repetition loop)")


def llm_recipes(args, fname, text):
    """Greedy first; if the model loops or times out, retry once with some randomness,
    which usually breaks the loop."""
    try:
        return llm_once(args, fname, text, 0)
    except (RuntimeError, requests.exceptions.Timeout) as first:
        print(f"      retrying ({str(first)[:80]})")
        return llm_once(args, fname, text, 0.4)


def usable(rec):
    return rec.get("name", "").strip() and (rec.get("recipeIngredient")
                                            or rec.get("recipeInstructions"))


def cmd_structure(args, work):
    todo = [(k, m) for k, m in work.manifest.items()
            if m["status"] == "extracted" and not (work.json / f"{k}.json").exists()]
    todo = select(todo, args)
    for i, (k, m) in enumerate(todo, 1):
        text = clean((work.text / f"{k}.txt").read_text())
        try:
            recipes = []
            for part in chunks(text, args.chunk_chars):
                recipes += [r for r in llm_recipes(args, m["rel"], part) if usable(r)]
            dump(work.json / f"{k}.json",
                 {"source": m["rel"], "model": args.model, "recipes": recipes})
            m["recipes"] = len(recipes)
            m["status"] = "structured" if recipes else "not-recipe"
            m.pop("error", None)
            print(f"[{i}/{len(todo)}] {len(recipes)} recipe(s)  {m['rel']}")
        except Exception as e:  # stays 'extracted', so a re-run retries it
            m["error"] = str(e)[:300]
            print(f"[{i}/{len(todo)}] ERROR {m['rel']}: {m['error']}")
        work.save()


# ---------------------------------------------------------------- 3. report

def norm(name):
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def cmd_report(args, work):
    rows, name_count, counts = [], {}, {}
    for k, m in work.manifest.items():
        recipes = load(work.json / f"{k}.json", {}).get("recipes", [])
        for r in recipes:
            name_count[norm(r["name"])] = name_count.get(norm(r["name"]), 0) + 1
        rows.append((k, m, recipes))

    out = work.root / "report.csv"
    with out.open("w", newline="") as f:
        cw = csv.writer(f)
        cw.writerow(["file", "status", "kind", "chars", "recipes", "names", "flags", "note"])
        for k, m, recipes in sorted(rows, key=lambda r: r[1]["rel"].lower()):
            flags = set()
            for r in recipes:
                if len(r.get("recipeIngredient", [])) < 3:
                    flags.add("few-ingredients")
                if len(r.get("recipeInstructions", [])) < 2:
                    flags.add("few-steps")
                if name_count[norm(r["name"])] > 1:
                    flags.add("same-name-elsewhere")
            if m.get("kind", "").endswith("ocr"):
                flags.add("ocr")
            if len(recipes) > 1:
                flags.add("multi-recipe")
            done = sum(1 for j in range(len(recipes)) if f"{k}#{j}" in work.uploaded)
            status = "uploaded" if recipes and done == len(recipes) else m["status"]
            counts[status] = counts.get(status, 0) + 1
            cw.writerow([m["rel"], status, m.get("kind", ""), m.get("chars", ""),
                         len(recipes), " | ".join(r["name"] for r in recipes),
                         " ".join(sorted(flags)), m.get("dup_of") or m.get("error", "")])
    print(f"Wrote {out}")
    for s, n in sorted(counts.items()):
        print(f"  {s:<14} {n}")


# ---------------------------------------------------------------- 4. upload

def to_jsonld(rec, source):
    desc = rec.get("description", "").strip()
    d = {"@context": "https://schema.org", "@type": "Recipe",
         "name": rec["name"].strip(),
         "description": (desc + "\n\n" if desc else "") + f"Imported from: {source}"}
    for key in ("recipeYield", "prepTime", "cookTime", "totalTime", "recipeCuisine", "url"):
        if rec.get(key, "").strip():
            d[key] = rec[key].strip()
    d["recipeIngredient"] = [s.strip() for s in rec.get("recipeIngredient", []) if s.strip()]
    d["recipeInstructions"] = [{"@type": "HowToStep", "text": s.strip()}
                               for s in rec.get("recipeInstructions", []) if s.strip()]
    if rec.get("recipeCategory"):
        d["recipeCategory"] = rec["recipeCategory"]
    tags = [t.strip().lower() for t in rec.get("keywords", []) if t.strip()] + ["imported"]
    d["keywords"] = ", ".join(dict.fromkeys(tags))
    return d


def attach_original(session, base, slug, path):
    ext = path.suffix.lstrip(".").lower() or "txt"
    with path.open("rb") as f:
        r = session.post(f"{base}/api/recipes/{slug}/assets",
                         data={"name": "Original file", "icon": "mdi-file", "extension": ext},
                         files={"file": (path.name, f)}, timeout=120)
    if not r.ok:
        print(f"      (could not attach original: HTTP {r.status_code})")


def cmd_upload(args, work):
    token = os.environ.get("MEALIE_TOKEN")
    if not token:
        sys.exit("Set MEALIE_TOKEN first (Mealie: Profile > API Tokens).")
    base = args.mealie.rstrip("/")
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {token}"

    existing = set()
    if not args.allow_duplicates:
        page = 1
        while True:
            r = s.get(f"{base}/api/recipes", params={"page": page, "perPage": 500}, timeout=60)
            r.raise_for_status()
            body = r.json()
            existing |= {norm(x["name"]) for x in body.get("items", []) if x.get("name")}
            if page >= body.get("total_pages", 1):
                break
            page += 1
        print(f"{len(existing)} recipes already in Mealie; same-name recipes will be skipped.")

    todo = select([(k, m) for k, m in work.manifest.items() if m["status"] == "structured"], args)
    ok = failed = skipped = 0
    skip_log = work.root / "skipped-duplicates.csv"
    for i, (k, m) in enumerate(todo, 1):
        recipes = load(work.json / f"{k}.json", {}).get("recipes", [])
        for j, rec in enumerate(recipes):
            key = f"{k}#{j}"
            if key in work.uploaded:
                continue
            if not args.allow_duplicates and norm(rec["name"]) in existing:
                skipped += 1
                new_file = not skip_log.exists()
                with skip_log.open("a", newline="") as f:
                    cw = csv.writer(f)
                    if new_file:
                        cw.writerow(["recipe", "source file", "json key"])
                    cw.writerow([rec["name"], m["rel"], key])
                print(f"[{i}/{len(todo)}] skip  {rec['name']} (name already in Mealie)")
                continue
            try:
                r = s.post(f"{base}/api/recipes/create/html-or-json",
                           json={"data": json.dumps(to_jsonld(rec, m["rel"])),
                                 "includeTags": True},
                           timeout=120)
                if not r.ok:
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
                slug = r.json()
                work.uploaded[key] = slug
                existing.add(norm(rec["name"]))
                ok += 1
                print(f"[{i}/{len(todo)}] ok    {slug}")
                path = Path(m["path"])
                if (args.attach and len(recipes) == 1 and path.exists()
                        and m["size"] <= MAX_ATTACH_BYTES):
                    attach_original(s, base, slug, path)
            except Exception as e:  # not recorded, so a re-run retries it
                failed += 1
                print(f"[{i}/{len(todo)}] FAIL  {m['rel']} - {rec.get('name')}: {e}")
            work.save()
    print(f"Uploaded {ok}, skipped {skipped} same-name, failed {failed}. Re-run to retry failures.")
    if skipped:
        print(f"Skipped recipes are listed in {skip_log}; upload one anyway with "
              f"--allow-duplicates --match '<file name>'.")


# ---------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--work", default="work", help="working folder (default ./work)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--limit", type=int, help="process at most N files this run")
        sp.add_argument("--match", help="only files whose name matches this regex")

    e = sub.add_parser("extract", help="extract text from recipe files")
    e.add_argument("src", help="folder containing the recipe files")
    common(e)

    st = sub.add_parser("structure", help="turn extracted text into recipe JSON")
    st.add_argument("--model", default="qwen2.5:14b")
    st.add_argument("--ollama", default="http://localhost:11434")
    st.add_argument("--num-ctx", type=int, default=8192)
    st.add_argument("--max-tokens", type=int, default=3000,
                    help="cap on the length of each model answer (default 3000)")
    st.add_argument("--chunk-chars", type=int, default=CHUNK_CHARS,
                    help=f"split long documents into pieces of about this many "
                         f"characters (default {CHUNK_CHARS}); use less for cookbooks")
    st.add_argument("--timeout", type=int, default=600,
                    help="seconds to wait for one model answer (default 600)")
    common(st)

    sub.add_parser("report", help="write work/report.csv for review")

    u = sub.add_parser("upload", help="send recipes to Mealie")
    u.add_argument("--mealie", required=True, help="e.g. http://your-mealie-host:9000")
    u.add_argument("--no-attach", dest="attach", action="store_false",
                   help="don't attach the original file to each recipe")
    u.add_argument("--allow-duplicates", action="store_true",
                   help="upload even if a recipe with the same name is already in Mealie")
    common(u)

    args = p.parse_args()
    work = Work(args.work)
    {"extract": cmd_extract, "structure": cmd_structure,
     "report": cmd_report, "upload": cmd_upload}[args.cmd](args, work)


if __name__ == "__main__":
    main()
