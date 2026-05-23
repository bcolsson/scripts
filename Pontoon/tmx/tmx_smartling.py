import os
import re
import argparse
import requests
import sys
from pathlib import Path
from xml.etree import ElementTree as et

BRANDS = {"-brand-product-name": "Firefox", "-brand-short-name": "Firefox"}
BRAND_RE = re.compile(r"\{\s*(-[\w-]+)\s*\}")

#POLISH_BRANDS = {
#    "-brand-product-name": {
#        "nom": "Firefox",
#        "gen": "Firefoksa",
#        "dat": "Firefoksowi",
#        "acc": "Firefoksa",
#        "ins": "Firefoksem",
#        "loc": "Firefoksie",
#    },
#    "-brand-short-name": {
#        "nom": "Firefox",
#        "gen": "Firefoksa",
#        "dat": "Firefoksowi",
#        "acc": "Firefoksa",
#        "ins": "Firefoksem",
#        "loc": "Firefoksie",
#    },
#}

#BRAND_RE = re.compile(
#    r'\{\s*(-[\w-]+)(?:\(\s*case\s*:\s*"(\w+)"\s*\))?\s*\}'
#)

def replace_brand(m: re.Match) -> str:
    term, case = m.group(1), m.group(2) or "nom"
    forms = POLISH_BRANDS.get(term)
    if forms is None:
        return m.group(0)              # unknown term — leave untouched
    return forms.get(case, forms["nom"])  # Fluent's *[nom] = default fallback

def export_tmx(locale_list, project):
    root_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
    )

    locale_path = os.path.join(root_path, "pontoon_exports")
    if not os.path.isdir(locale_path):
        os.mkdir(locale_path)

    file_list = []

    for locale in locale_list:
        try:
            response = requests.get(
                f"https://pontoon.mozilla.org/translation-memory/{locale}.{project}.tmx"
            )

            file_path = os.path.join(locale_path, f"{locale}_{project}.tmx")

            with open(os.path.join(file_path), "wb") as f:
                f.write(response.content)
                file_list.append(Path(file_path))
        except Exception as e:
            print(e)

    return file_list


def filter_tmx(in_path: Path, out_path: str, resource_path: str) -> int:

    # Iterate and parse the the tmx file, finding start and end of a <tu> and determine to keep or discard
    kept = 0
    context = et.iterparse(in_path, events=("start", "end"))
    _, root = next(context)  # <tmx>
    out_tmx = et.Element("tmx", root.attrib)
    header = None
    body = et.SubElement(out_tmx, "body")

    for event, elem in context:
        if event == "end" and elem.tag == "header" and header is None:
            header = elem
        elif event == "end" and elem.tag == "tu":
            tuid = elem.get("tuid", "")
            parts = tuid.split(":", 2)  # project:path:keyslug
            if len(parts) == 3 and parts[1] == resource_path:
                # Replace common Firefox fluent placeholders
                for seg in elem.iter("seg"):
                    if seg.text:
                        seg.text = BRAND_RE.sub(lambda m: BRANDS.get(m.group(1), m.group(0)), seg.text)
                        #seg.text = BRAND_RE.sub(replace_brand, seg.text)
                body.append(elem)
                kept += 1
            else:
                elem.clear()
            root.clear()

    if header is not None:
        out_tmx.insert(0, header)
    et.ElementTree(out_tmx).write(out_path, encoding="utf-8", xml_declaration=True)
    return kept


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Filter a Pontoon TMX to entries from a single resource path."
    )
    parser.add_argument(
        "--locales",
        required=True,
        dest="locale_list",
        help="Path to .txt file with each required locale code entered on a new line. The appropriate .tmx file will be exported from Pontoon.",
    )
    parser.add_argument(
        "--project",
        required=True,
        dest="project",
        help="Project name from Pontoon project slug"
    )
    parser.add_argument(
        "--resource_path",
        required=True,
        dest="resource_path",
        help="resource path as stored in Pontoon, e.g. browser/browser.ftl",
    )
    args = parser.parse_args()
    with open(args.locale_list) as f:
        locale_list = [locale.strip() for locale in f]

    tmx_files = export_tmx(locale_list, args.project)

    for file in tmx_files:
        output_file = f"{file.stem}_extract{file.suffix}"
        n = filter_tmx(file, output_file, args.resource_path)
        print(f"Kept {n} <tu> entries", file=sys.stderr)


if __name__ == "__main__":
    main()