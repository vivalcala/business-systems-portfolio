"""
PDF Case Splitter
=================

Turns one multi-page PDF of release forms into one PDF per case, plus a small
JSON record per case that a Power Automate flow uses to create or update the
case in a Microsoft List.

What it does
------------
1. Reads every PDF dropped in  ./input_pdf  (one PDF can hold one or many forms).
2. Reads each page. Works with digital PDFs AND scanned PDFs: if a page has no
   text layer, it runs OCR on that page only.
3. Extracts the case ID, name, requesting facility, date of birth, date of death
   and release date.
4. Saves one PDF per case, named  <CASE-ID>_<First_Last>.pdf, in the output folder.
5. Saves the same data as  _data/<CASE-ID>_<First_Last>.json  (written BEFORE the
   PDF, because the flow is triggered by the PDF and must find the data ready).
6. Moves the original PDF to  ./processed_archive.

Pages it cannot read go to  ./input_pdf/NEEDS_REVIEW  for manual handling,
so a bad scan never creates a wrong record.

All names, IDs and facilities in this repository are fictional.
"""

import json
import os
import re
import shutil
import sys
from datetime import datetime

from pypdf import PdfReader, PdfWriter

# ---------------------------------------------------------------- settings
HERE = os.path.dirname(os.path.abspath(__file__))
INPUT_FOLDER = os.path.join(HERE, "input_pdf")
ARCHIVE_FOLDER = os.path.join(HERE, "processed_archive")
REVIEW_FOLDER = os.path.join(INPUT_FOLDER, "NEEDS_REVIEW")

# Output goes to a synced cloud folder (e.g. OneDrive) watched by Power Automate.
# Set CASE_OUTPUT_FOLDER to that path; defaults to ./output for local testing.
OUTPUT_FOLDER = os.environ.get("CASE_OUTPUT_FOLDER", os.path.join(HERE, "output"))
DATA_FOLDER = os.path.join(OUTPUT_FOLDER, "_data")

# Known facility names, spelled exactly as in the tracker's "Received From" column.
# OCR sometimes drops spaces ("ExampleGeneralHospital"); this list restores them.
FACILITIES_FILE = os.path.join(HERE, "facilities.txt")

DATE_RE = r"(\d{1,2}/\d{1,2}/\d{4})"
CASE_ID_RE = r"([PA]\d{4}-\d{3,5})"


def load_facilities():
    """facilities.txt: one name per line, or 'other spelling = Tracker Name'.
    The left side can also be a phone number, to map a location's phone to its name."""
    names, aliases = [], {}
    if not os.path.isfile(FACILITIES_FILE):
        return names, aliases
    with open(FACILITIES_FILE, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                left, right = (part.strip() for part in line.split("=", 1))
                digits = re.sub(r"\D", "", left)
                key = digits if len(digits) == 10 else squash(left)
                aliases[key] = right
                names.append(right)
            else:
                names.append(line)
    return names, aliases

# ---------------------------------------------------------------- reading
_ocr_engine = None


def ocr_page(pdf_path, page_index):
    """OCR one page. The OCR engine is only loaded when a page has no digital text."""
    global _ocr_engine
    import numpy as np
    import pypdfium2 as pdfium

    if _ocr_engine is None:
        from rapidocr_onnxruntime import RapidOCR

        print("   (scanned page - reading with OCR, first time takes ~20 sec)")
        _ocr_engine = RapidOCR()
    pdf = pdfium.PdfDocument(pdf_path)
    image = pdf[page_index].render(scale=2).to_pil().convert("RGB")
    result, _ = _ocr_engine(np.array(image))
    return "\n".join(r[1] for r in (result or []))


def page_text(reader, pdf_path, page_index):
    text = reader.pages[page_index].extract_text() or ""
    if re.search(CASE_ID_RE, text):
        return text
    return ocr_page(pdf_path, page_index)


# ---------------------------------------------------------------- parsing
def squash(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


KNOWN_FACILITIES, FACILITY_ALIASES = load_facilities()
PHONE_RE = r"\(?\b\d{3}\)?[\s.\-]*\d{3}[\s.\-]*\d{4}\b\)?"


def fix_facility(raw):
    """Return the facility name only: no phone numbers, consistent spelling."""
    raw = raw.strip()
    # 1. a phone number next to the name -> known location, otherwise drop it
    phone = re.search(PHONE_RE, raw)
    if phone:
        digits = re.sub(r"\D", "", phone.group(0))
        if digits in FACILITY_ALIASES:
            return FACILITY_ALIASES[digits]
        raw = re.sub(PHONE_RE, "", raw)
    raw = re.sub(r"\(\s*\)", "", raw).strip(" -\u2013,;()")
    # 2. known names and other spellings
    for name in KNOWN_FACILITIES:
        if squash(name) == squash(raw):
            return name
    if squash(raw) in FACILITY_ALIASES:
        return FACILITY_ALIASES[squash(raw)]
    # 3. unknown: put spaces back between words that OCR glued together,
    #    and write a location suffix the same way ("Name -North" -> "Name \u2013 North")
    raw = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", raw)
    raw = re.sub(r"\s*[-\u2013]\s*(?=[A-Za-z])", " \u2013 ", raw, count=1)
    return re.sub(r"\s{2,}", " ", raw).strip()


def split_given_names(given):
    """'JaneA' -> 'Jane A'   'John R' -> 'John R'"""
    given = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", given.strip())
    return " ".join(w.capitalize() if len(w) > 1 else w.upper() for w in given.split())


def date_after(label_regex, text):
    m = re.search(label_regex + r"[\s\S]{0,40}?" + DATE_RE, text, re.IGNORECASE)
    return m.group(1) if m else ""


def to_iso(us_date):
    try:
        return datetime.strptime(us_date, "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def parse_release(text):
    """Extract case fields from one page of text. Missing fields stay empty."""
    data = {}
    case_id = re.search(r"PIP\s*#?\s*:?\s*" + CASE_ID_RE, text, re.IGNORECASE) or re.search(
        CASE_ID_RE, text
    )
    if case_id:
        prefix, num = case_id.group(1).upper().split("-")
        data["caseId"] = f"{prefix}-{num.zfill(5)}"  # P2026-123 -> P2026-00123

    dec = re.search(r"Decedent\s*:\s*([^\n\r]+)", text, re.IGNORECASE)
    if dec:
        raw = re.split(r"PIP\s*#", dec.group(1), flags=re.IGNORECASE)[0].strip()
        if "," in raw:  # "DOE, JANE A" -> "Jane A Doe"
            last, given = raw.split(",", 1)
            last = " ".join(w.capitalize() for w in last.strip().split())
            data["name"] = f"{split_given_names(given)} {last}".strip()
        else:
            data["name"] = " ".join(w.capitalize() for w in raw.split())

    fac = re.search(r"([^\n\r]+)\s*[\n\r]+\s*Requesting\s*Facility", text, re.IGNORECASE)
    if fac:
        data["facility"] = fix_facility(fac.group(1))

    data["releaseDate"] = to_iso(date_after(r"\bDate\s*:", text))
    data["dob"] = to_iso(date_after(r"Date\s*of\s*Birth", text))
    data["dod"] = to_iso(date_after(r"Date\s*of\s*Death", text))
    return data


def safe(text):
    return re.sub(r"[^A-Za-z0-9\-_ ]", "", text).strip().replace(" ", "_")


# ---------------------------------------------------------------- main
def main():
    for folder in (INPUT_FOLDER, ARCHIVE_FOLDER, REVIEW_FOLDER, OUTPUT_FOLDER, DATA_FOLDER):
        os.makedirs(folder, exist_ok=True)

    files = [f for f in os.listdir(INPUT_FOLDER) if f.lower().endswith(".pdf")]
    if not files:
        print("No PDF found in input_pdf. Drop the PDF there and run again.")
        return

    done, review = 0, 0
    for name in files:
        pdf_path = os.path.join(INPUT_FOLDER, name)
        reader = PdfReader(pdf_path)
        print(f"\n{name}  ({len(reader.pages)} pages)")

        for i in range(len(reader.pages)):
            data = parse_release(page_text(reader, pdf_path, i))
            writer = PdfWriter()
            writer.add_page(reader.pages[i])

            if not data.get("caseId") or not data.get("name"):
                out = os.path.join(REVIEW_FOLDER, f"{os.path.splitext(name)[0]}_page{i + 1}.pdf")
                with open(out, "wb") as fh:
                    writer.write(fh)
                print(f"   page {i + 1}: could not read case ID/name -> NEEDS_REVIEW")
                review += 1
                continue

            base = f"{data['caseId']}_{safe(data['name'])}"
            data["file"] = base + ".pdf"
            data["sourceFile"] = name

            # JSON first, then the PDF (the flow is triggered by the PDF)
            with open(os.path.join(DATA_FOLDER, base + ".json"), "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            with open(os.path.join(OUTPUT_FOLDER, base + ".pdf"), "wb") as fh:
                writer.write(fh)

            print(
                f"   page {i + 1}: {base}.pdf  | {data.get('facility', '?')}"
                f" | DOD {data.get('dod') or '?'} | release {data.get('releaseDate') or '?'}"
            )
            done += 1

        shutil.move(pdf_path, os.path.join(ARCHIVE_FOLDER, name))

    print(f"\nDone: {done} case(s) saved, {review} page(s) need review.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # keep the window open so the error can be read
        print("\nERROR:", exc)
    if sys.stdin and sys.stdin.isatty():
        input("\nPress Enter to close...")
