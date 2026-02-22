from flask import Flask, render_template, request, redirect, url_for, send_file
import csv
from io import StringIO
import time
import unicodedata
import re
import os

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(BASE_DIR, "dictionary.csv")
# ===============================
# 🚀 DICTIONARY CACHE SETTINGS
# ===============================
_dictionary_cache = None
_cache_time = 0
CACHE_TTL = 30  # seconds


# ===============================
# 🔤 NORMALIZATION (TONE-SAFE)
# ===============================
def to_plain(text: str) -> str:
    """
    Tone-safe plain form:
    - lowercase
    - unicode normalize
    - remove combining marks
    - normalize whitespace
    """
    if not text:
        return ""
    text = text.strip().lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = " ".join(text.split())
    return text


def split_meanings(english_cell: str):
    """
    english column can contain:
    - "a | b | c"
    - or "a, b"
    """
    if not english_cell:
        return []
    s = english_cell.strip()
    if "|" in s:
        parts = [p.strip() for p in s.split("|")]
        return [p for p in parts if p]
    if "," in s:
        parts = [p.strip() for p in s.split(",")]
        return [p for p in parts if p]
    return [s]


# ===============================
# 🧠 TOKENIZATION (KEEP PUNCTUATION)
# ===============================
_TOKEN_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)

def tokenize(text: str):
    """
    Returns a list of tokens where:
    - words (including numbers/letters) are separate tokens
    - punctuation marks are separate tokens
    - spaces are NOT returned (we rebuild spacing later)
    """
    return _TOKEN_RE.findall(text)


def rebuild_text(tokens):
    """
    Rebuild readable text from tokens.
    Rules:
    - No space before punctuation like . , ! ? : ; ) ] }
    - No space after opening punctuation like ( [ {
    - Keep normal spacing between words
    """
    if not tokens:
        return ""

    no_space_before = set(".,!?;:)]}٪%”’\"")
    no_space_after = set("([{“‘\"")

    out = []
    for i, tok in enumerate(tokens):
        if i == 0:
            out.append(tok)
            continue

        prev = out[-1]

        if tok in no_space_before:
            out.append(tok)
        elif prev and prev[-1] in no_space_after:
            out.append(tok)
        else:
            out.append(" " + tok)

    return "".join(out)


# ===============================
# 📘 LOAD DICTIONARY (CACHED) + BUILD INDEXES
# ===============================
def load_dictionary(force_reload=False):
    global _dictionary_cache, _cache_time

    if _dictionary_cache and not force_reload:
        if time.time() - _cache_time < CACHE_TTL:
            return _dictionary_cache

    encodings = ["utf-8-sig", "utf-8", "cp1252", "latin-1"]
    rows = []

    for enc in encodings:
        try:
            with open(CSV_PATH, encoding=enc, newline="") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    english_raw = (r.get("english") or "").strip()
                    nupe_raw = (r.get("nupe") or "").strip()

                    meanings = [m.lower().strip() for m in split_meanings(english_raw)]
                    if not meanings and not nupe_raw:
                        continue

                    rows.append({
                        "english": meanings,
                        "nupe": nupe_raw,
                        "english_plain": [to_plain(m) for m in meanings],
                        "nupe_plain": to_plain(nupe_raw),
                    })
            break

        except UnicodeDecodeError:
            continue

    # Phrase indexes
    en_phrase_index = {}
    nu_phrase_index = {}

    # Word indexes
    en_word_index = {}
    nu_word_index = {}

    # Phrase lengths
    en_phrase_lens = set()
    nu_phrase_lens = set()

    for entry in rows:
        for ep in entry["english_plain"]:
            en_phrase_index.setdefault(ep, []).append(entry)
            en_phrase_lens.add(len(ep.split()))

            for w in ep.split():
                en_word_index.setdefault(w, []).append(entry)

        nu_phrase_index.setdefault(entry["nupe_plain"], []).append(entry)
        nu_phrase_lens.add(len(entry["nupe_plain"].split()))

        for w in entry["nupe_plain"].split():
            nu_word_index.setdefault(w, []).append(entry)

    _dictionary_cache = {
        "rows": rows,
        "en_phrase_index": en_phrase_index,
        "nu_phrase_index": nu_phrase_index,
        "en_word_index": en_word_index,
        "nu_word_index": nu_word_index,
        "en_phrase_lens": sorted(en_phrase_lens, reverse=True),
        "nu_phrase_lens": sorted(nu_phrase_lens, reverse=True),
    }

    _cache_time = time.time()
    return _dictionary_cache


# ===============================
# 💾 SAVE DICTIONARY (PRESERVE TONES)
# ===============================
def save_dictionary(rows):
    with open(CSV_PATH, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["english", "nupe"])
        for e in rows:
            writer.writerow([" | ".join(e["english"]), e["nupe"]])
    load_dictionary(force_reload=True)


# ===============================
# 🎯 PICK BEST MATCH WHEN AMBIGUOUS
# ===============================
def pick_best(candidates, query_plain):
    if not candidates:
        return None
    for c in candidates:
        if c.get("nupe_plain") == query_plain:
            return c
    return candidates[0]


# ===============================
# 🧩 LONG-TEXT TRANSLATION (SENTENCES/PARAGRAPHS)
# ===============================
def translate_long_text(text: str, direction: str, D):
    """
    Translates long text by:
    1) Tokenize to keep punctuation
    2) Convert word tokens to plain form for matching
    3) Longest-phrase match on sequences of word tokens
    4) Fallback to single-word match
    5) Rebuild punctuation spacing nicely
    """
    raw = (text or "")
    if not raw.strip():
        return ""

    tokens = tokenize(raw)

    # word positions and plain forms
    word_positions = []
    plain_words = []
    for i, t in enumerate(tokens):
        if re.match(r"^\w+$", t, re.UNICODE):
            word_positions.append(i)
            plain_words.append(to_plain(t))

    if not plain_words:
        return raw

    out_tokens = tokens[:]  # copy

    if direction == "en_to_nupe":
        phrase_index = D["en_phrase_index"]
        word_index = D["en_word_index"]
        phrase_lens = D["en_phrase_lens"]

        def phrase_to_output(entry):
            return entry["nupe"]

    else:
        phrase_index = D["nu_phrase_index"]
        word_index = D["nu_word_index"]
        phrase_lens = D["nu_phrase_lens"]

        def phrase_to_output(entry):
            return " | ".join(entry["english"])

    i = 0
    used = set()  # word indices already replaced
    while i < len(plain_words):
        if i in used:
            i += 1
            continue

        matched = False

        # 1) Try longest phrases first
        for L in phrase_lens:
            if L <= 1:
                continue
            if i + L > len(plain_words):
                continue

            # ensure range not already used
            if any((k in used) for k in range(i, i + L)):
                continue

            phrase_plain = " ".join(plain_words[i:i+L])
            candidates = phrase_index.get(phrase_plain, [])
            best = pick_best(candidates, phrase_plain)
            if best:
                # replace the first token of the phrase, blank the rest word tokens
                first_tok_pos = word_positions[i]
                out_tokens[first_tok_pos] = phrase_to_output(best)

                # blank other word tokens in this phrase
                for k in range(i + 1, i + L):
                    out_tokens[word_positions[k]] = ""

                for k in range(i, i + L):
                    used.add(k)

                matched = True
                break

        if matched:
            i += 1
            continue

        # 2) Fallback to single word
        w = plain_words[i]
        candidates = word_index.get(w, [])
        best = candidates[0] if candidates else None
        if best:
            out_tokens[word_positions[i]] = phrase_to_output(best)
        # else keep original token as is

        used.add(i)
        i += 1

    # remove empty tokens from phrase replacement
    out_tokens = [t for t in out_tokens if t != ""]

    return rebuild_text(out_tokens)


# ===============================
# 🌍 TRANSLATION LOGIC (FULL TEXT)
# ===============================
def translate(text, direction, D):
    return translate_long_text(text, direction, D)


# ===============================
# 🏠 MAIN PAGE
# ===============================
@app.route("/", methods=["GET", "POST"])
def index():
    translation = ""
    if request.method == "POST":
        text_input = request.form.get("text_input", "")
        direction = request.form.get("direction", "en_to_nupe")
        D = load_dictionary()
        translation = translate(text_input, direction, D)
    return render_template("index.html", translation=translation)


# ===============================
# 🔧 ADMIN PANEL
# ===============================
@app.route("/admin")
def admin():
    D = load_dictionary()
    return render_template("admin.html", dictionary=D["rows"])


@app.route("/admin/add", methods=["POST"])
def add_entry():
    english = (request.form.get("english") or "").strip()
    nupe = (request.form.get("nupe") or "").strip()

    D = load_dictionary()
    rows = D["rows"]

    rows.append({
        "english": [m.lower().strip() for m in split_meanings(english)],
        "nupe": nupe,
        "english_plain": [],
        "nupe_plain": "",
    })

    save_dictionary(rows)
    return redirect(url_for("admin"))


@app.route("/admin/delete/<int:index>")
def delete_entry(index):
    D = load_dictionary()
    rows = D["rows"]
    if 0 <= index < len(rows):
        rows.pop(index)
        save_dictionary(rows)
    return redirect(url_for("admin"))


# ===============================
# 📤 EXPORT CSV
# ===============================
@app.route("/admin/export")
def export_csv():
    D = load_dictionary()
    rows = D["rows"]

    si = StringIO()
    writer = csv.writer(si)
    writer.writerow(["english", "nupe"])
    for e in rows:
        writer.writerow([" | ".join(e["english"]), e["nupe"]])

    output = si.getvalue()
    return send_file(
        StringIO(output),
        mimetype="text/csv",
        as_attachment=True,
        download_name="dictionary_export.csv",
    )


# ===============================
# 📥 IMPORT CSV (REPLACE MODE)
# ===============================
@app.route("/admin/import", methods=["POST"])
def import_csv():
    file = request.files.get("file")
    if not file:
        return redirect(url_for("admin"))

    content = file.read().decode("utf-8-sig", errors="replace").splitlines()
    reader = csv.DictReader(content)

    rows = []
    for r in reader:
        english_raw = (r.get("english") or "").strip()
        nupe_raw = (r.get("nupe") or "").strip()
        rows.append({
            "english": [m.lower().strip() for m in split_meanings(english_raw)],
            "nupe": nupe_raw,
            "english_plain": [],
            "nupe_plain": "",
        })

    save_dictionary(rows)
    return redirect(url_for("admin"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
