#!/usr/bin/env python3
"""Convert .txt / .md recipe files to schema.org Recipe JSON for Mealie.

Output can be pasted into Mealie: Create (+) -> Import -> "HTML or JSON" tab,
or uploaded through the API. No third-party dependencies, no LLM.

Usage:
    txt2mealie.py FILE_OR_DIR [...] [-o OUTDIR]

Recognised layout (headings may be plain, '#' markdown, or end with ':'):
    Title (first non-empty line)
    optional intro text            -> description
    Ingredients  + bullet lines
    Instructions / Directions / Method / Steps + numbered lines
    optional: Prep time, Cook time, Total time, Servings / Yield / Makes,
              Notes, Tags / Category
"""
import argparse
import json
import re
import sys
from pathlib import Path

SECTIONS = {
    "ingredients": "ingredients",
    "ingredient": "ingredients",
    "instructions": "steps",
    "instruction": "steps",
    "directions": "steps",
    "method": "steps",
    "steps": "steps",
    "preparation": "steps",
    "notes": "notes",
    "note": "notes",
    "tips": "notes",
}
BULLET = re.compile(r"^\s*(?:[-*•▪◦·–]|\d+[.)])\s+")
HEADING = re.compile(r"^\s*(?:#{1,6}\s*)?(?:\*\*)?([A-Za-z ]+?)(?:\*\*)?\s*:?\s*$")
META = re.compile(
    r"^\s*(?:[-*•]\s*)?(?:\*\*)?(prep(?:aration)? time|cook(?:ing)? time|total time|"
    r"servings?|serves|yield|makes|tags?|categor(?:y|ies))(?:\*\*)?\s*[:\-]\s*(.+)$",
    re.I,
)
TITLE_FLUFF = re.compile(r"\s+recipe\s*$", re.I)


def to_iso_duration(text):
    """'1 hr 30 min' / '45 minutes' -> 'PT1H30M'. Returns None if unparseable."""
    h = re.search(r"(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours)\b", text, re.I)
    m = re.search(r"(\d+)\s*(?:m|min|mins|minute|minutes)\b", text, re.I)
    if not (h or m):
        return None
    mins = int(float(h.group(1)) * 60) if h else 0
    mins += int(m.group(1)) if m else 0
    out = "PT"
    if mins // 60:
        out += f"{mins // 60}H"
    if mins % 60 or not mins // 60:
        out += f"{mins % 60}M"
    return out


def clean(line):
    line = BULLET.sub("", line).strip()
    return re.sub(r"\*\*|__", "", line)


def strip_step_label(step):
    """'Melt the Base: In a small...' -> keep text as-is (label is useful)."""
    return step


def parse(text, fallback_title):
    lines = text.replace("\r\n", "\n").split("\n")
    recipe = {"title": None, "intro": [], "ingredients": [], "steps": [],
              "notes": [], "meta": {}}
    section = "intro"
    for raw in lines:
        if not raw.strip():
            continue
        if recipe["title"] is None:
            recipe["title"] = re.sub(r"^#+\s*", "", raw).strip().strip("*")
            continue
        mm = META.match(raw)
        if mm:
            recipe["meta"][mm.group(1).lower()] = mm.group(2).strip()
            continue
        hm = HEADING.match(raw)
        if hm and hm.group(1).strip().lower() in SECTIONS and not BULLET.match(raw):
            section = SECTIONS[hm.group(1).strip().lower()]
            continue
        recipe[section].append(raw)

    ingredients = [clean(l) for l in recipe["ingredients"] if clean(l)]
    steps = [clean(l) for l in recipe["steps"] if clean(l)]
    notes = " ".join(clean(l) for l in recipe["notes"])
    intro = " ".join(l.strip() for l in recipe["intro"]).strip()
    title = TITLE_FLUFF.sub("", recipe["title"] or fallback_title).strip() or fallback_title

    meta = recipe["meta"]
    out = {
        "@context": "https://schema.org",
        "@type": "Recipe",
        "name": title,
        "description": intro,
        "recipeIngredient": ingredients,
        "recipeInstructions": [{"@type": "HowToStep", "text": s} for s in steps],
    }
    for key, field in (("prep time", "prepTime"), ("prep", "prepTime"),
                       ("preparation time", "prepTime"), ("cook time", "cookTime"),
                       ("cooking time", "cookTime"), ("total time", "totalTime")):
        if key in meta and (d := to_iso_duration(meta[key])):
            out[field] = d
    for key in ("servings", "serving", "serves", "yield", "makes"):
        if key in meta:
            out["recipeYield"] = meta[key]
            break
    for key in ("category", "categories"):
        if key in meta:
            out["recipeCategory"] = meta[key]
    for key in ("tag", "tags"):
        if key in meta:
            out["keywords"] = meta[key]
    if notes:
        out["description"] = (out["description"] + "\n\nNotes: " + notes).strip()
    return out, bool(ingredients), bool(steps)


def convert(path, outdir):
    text = path.read_text(encoding="utf-8", errors="replace")
    recipe, has_ing, has_steps = parse(text, path.stem)
    dest = outdir / (path.stem + ".json")
    dest.write_text(json.dumps(recipe, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    warn = []
    if not has_ing:
        warn.append("no ingredients found")
    if not has_steps:
        warn.append("no instructions found")
    print(f"{path} -> {dest}" + (f"  WARNING: {', '.join(warn)}" if warn else ""))
    return not warn


def main():
    ap = argparse.ArgumentParser(description="Convert .txt/.md recipes to Mealie-importable JSON")
    ap.add_argument("inputs", nargs="+", type=Path, help="files or folders")
    ap.add_argument("-o", "--outdir", type=Path, default=Path("."),
                    help="output folder (default: current directory)")
    args = ap.parse_args()
    args.outdir.mkdir(parents=True, exist_ok=True)

    files = []
    for p in args.inputs:
        if p.is_dir():
            files += sorted(f for f in p.rglob("*") if f.suffix.lower() in (".txt", ".md"))
        elif p.is_file():
            files.append(p)
        else:
            print(f"not found: {p}", file=sys.stderr)
    if not files:
        sys.exit("no .txt or .md files to convert")
    ok = [convert(f, args.outdir) for f in files]
    sys.exit(0 if all(ok) else 1)


if __name__ == "__main__":
    main()
