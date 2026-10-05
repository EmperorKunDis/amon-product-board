#!/usr/bin/env python3
"""Assign product previews using only sitemap snapshots in the local archive."""
import gzip
import json
from pathlib import Path
import xml.etree.ElementTree as ET

REPO = Path(__file__).resolve().parent.parent
ARCHIVE = REPO.parent
NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9", "i": "http://www.google.com/schemas/sitemap-image/1.1"}


def readjs(path):
    text = path.read_text()
    prefix, value = text.split("=", 1)
    return prefix, json.loads(value.strip().removesuffix(";"))


def slug(url):
    return url.rstrip("/").split("/")[-1]


def main():
    mappings = {}
    for source in json.loads((ARCHIVE / "Marburg/research/web_sources.json").read_text()):
        if "/product-sitemap" not in source["url"]:
            continue
        path = ARCHIVE / "Marburg" / source["local_file"]
        try:
            tree = ET.parse(path)
        except (ET.ParseError, FileNotFoundError):
            continue
        for entry in tree.getroot().findall("s:url", NS):
            product_url = entry.findtext("s:loc", default="", namespaces=NS)
            image_urls = [node.text for node in entry.findall("i:image/i:loc", NS) if node.text and node.text.startswith("https://")]
            if not image_urls:
                continue
            modified = entry.findtext("s:lastmod", default="", namespaces=NS)
            key = slug(product_url)
            value = {"image": image_urls[0], "product_url": product_url, "snapshot": str(path.relative_to(ARCHIVE)), "lastmod": modified}
            if key not in mappings or modified > mappings[key]["lastmod"]:
                mappings[key] = value
    index_path = ARCHIVE / "board/index.json"
    index = json.loads(index_path.read_text())
    cache = {}
    report_path = REPO / "site/board/preview-assignments.json"
    assigned = json.loads(report_path.read_text()).get("sources", {}) if report_path.exists() else {}
    unresolved = []
    for row in index["rows"]:
        if row["kind"] != "products" or row["image"]:
            continue
        chunk = row["chunk"]
        if chunk not in cache:
            cache[chunk] = readjs(ARCHIVE / "board/details" / (chunk + ".js"))[1]
        detail = cache[chunk][row["id"]]
        product_url = detail.get("url", "")
        key = slug(product_url)
        match = mappings.get(key) if row["company"] == "Marburg" else None
        # A B2B sample keeps the same article number and collection slug.
        if not match and key.endswith("-b2b-sample"):
            match = mappings.get(key.removesuffix("-b2b-sample"))
        if not match:
            raw = detail.get("raw", {})
            image_url = raw.get("preview_url") or raw.get("image_url") or ""
            if image_url.startswith("https://") and "placeholder" not in image_url.lower():
                match = {"image": image_url, "product_url": product_url, "snapshot": detail.get("origins", [""])[0], "lastmod": ""}
        if match:
            row["image"] = match["image"]
            assigned[row["id"]] = {**match, "record_url": product_url}
        else:
            unresolved.append({"id": row["id"], "code": row["code"], "url": product_url})
    index_path.write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")))
    local_path = ARCHIVE / "board/indices/products.js"
    prefix, rows = readjs(local_path)
    for row in rows:
        if row["id"] in assigned:
            row["image"] = assigned[row["id"]]["image"]
    local_path.write_text(prefix + "=" + json.dumps(rows, ensure_ascii=False, separators=(",", ":")) + ";\n")
    deployed_path = REPO / "site/board/indices/products.js.json.gz"
    with gzip.open(deployed_path, "rt", encoding="utf-8") as stream:
        deployed = json.load(stream)
    for row in deployed:
        if row["id"] in assigned:
            row["image"] = assigned[row["id"]]["image"]
    with deployed_path.open("wb") as file:
        with gzip.GzipFile(fileobj=file, mode="wb", mtime=0, compresslevel=6) as stream:
            stream.write(json.dumps(deployed, ensure_ascii=False, separators=(",", ":")).encode())
    report = {"assigned": len(assigned), "unresolved": unresolved, "sources": assigned}
    report_path.write_text(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    print(f"Assigned {len(assigned)} local archive image references; unresolved {len(unresolved)}")


if __name__ == "__main__":
    main()
