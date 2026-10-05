#!/usr/bin/env python3
"""Add verified Marburg product previews to the Pages artifact.

The source archive contains sitemap-only products without images. This runs in
GitHub Actions, where the publisher site is reachable, and changes only the
temporary Pages artifact produced by that workflow.
"""
import concurrent.futures
import gzip
import hashlib
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from PIL import Image

SITE = Path(__file__).resolve().parent.parent / "site"
PRODUCTS = SITE / "board/indices/products.js.json.gz"
WORKERS = int(os.environ.get("MARBURG_PREVIEW_WORKERS", "12"))
USER_AGENT = "Mozilla/5.0 (compatible; AMONArchivePreview/1.0)"


class ProductPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta = {}
        self.images = []

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if tag == "img":
            self.images.append(fields)
            return
        if tag != "meta":
            return
        key = fields.get("property") or fields.get("name")
        if key in ("og:title", "og:image"):
            self.meta[key] = fields.get("content", "")


def get(url):
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=25) as response:
        return response.read(), response.geturl()


def preview(row, source_url):
    code = str(row["code"]).strip()
    if not code or not source_url.startswith("https://marburg.com/en/search/"):
        return row["id"], "", "unsupported source URL"
    page_url = source_url.replace("/en/search/", "/de/tapetensuche/", 1)
    for attempt in range(2):
        try:
            html, final_url = get(page_url)
            page = ProductPage()
            page.feed(html.decode("utf-8", "replace"))
            title = page.meta.get("og:title", "")
            matching_images = [image for image in page.images if image.get("alt", "").lstrip().startswith(code + " ")]
            image_url = page.meta.get("og:image", "")
            if matching_images:
                candidates = []
                for entry in matching_images[0].get("srcset", "").split(","):
                    parts = entry.strip().split()
                    if len(parts) == 2 and parts[1].endswith("w") and parts[1][:-1].isdigit():
                        candidates.append((int(parts[1][:-1]), parts[0]))
                if candidates:
                    image_url = min(candidates, key=lambda item: (item[0] < 300, abs(item[0] - 400)))[1]
                else:
                    image_url = matching_images[0].get("src", image_url)
            if not title.lstrip().startswith(code + " ") and not title.lstrip().startswith(code + "-"):
                return row["id"], "", "product number mismatch"
            if urlparse(final_url).hostname not in ("marburg.com", "www.marburg.com"):
                return row["id"], "", "unexpected product host"
            image = urlparse(image_url)
            if image.hostname not in ("marburg.com", "www.marburg.com") or not image.path.startswith("/wp-content/uploads/"):
                return row["id"], "", "no publisher image"
            raw, final_image = get(image_url)
            if urlparse(final_image).hostname not in ("marburg.com", "www.marburg.com"):
                return row["id"], "", "unexpected image host"
            with Image.open(io.BytesIO(raw)) as source:
                source.thumbnail((440, 440))
                out = SITE / "media/marburg" / (hashlib.sha256(row["id"].encode()).hexdigest()[:24] + ".webp")
                out.parent.mkdir(parents=True, exist_ok=True)
                source.convert("RGB").save(out, "WEBP", quality=72, method=3)
            return row["id"], out.relative_to(SITE).as_posix(), ""
        except Exception as error:
            if attempt:
                return row["id"], "", type(error).__name__
            time.sleep(1.5)


def main():
    with gzip.open(PRODUCTS, "rt", encoding="utf-8") as stream:
        rows = json.load(stream)
    missing = [r for r in rows if r["company"] == "Marburg" and not r["image"]]
    chunks = {}
    sources = {}
    for row in missing:
        chunk = row["chunk"]
        if chunk not in chunks:
            with gzip.open(SITE / "board/details" / (chunk + ".js.json.gz"), "rt", encoding="utf-8") as stream:
                chunks[chunk] = json.load(stream)
        sources[row["id"]] = chunks[chunk][row["id"]].get("url", "")
    failures = {}
    completed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(preview, row, sources[row["id"]]) for row in missing]
        mapped = {}
        for future in concurrent.futures.as_completed(futures):
            record_id, path, reason = future.result()
            completed += 1
            if path:
                mapped[record_id] = path
            else:
                failures[reason] = failures.get(reason, 0) + 1
            if completed % 250 == 0:
                print(f"Marburg previews: {completed}/{len(missing)}, matched {len(mapped)}", flush=True)
    for row in missing:
        row["image"] = mapped.get(row["id"], "")
    with PRODUCTS.open("wb") as file:
        with gzip.GzipFile(fileobj=file, mode="wb", mtime=0, compresslevel=6) as stream:
            stream.write(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    report = {"requested": len(missing), "matched": len(mapped), "unmatched": len(missing)-len(mapped), "reasons": failures}
    (SITE / "board/marburg-preview-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False), flush=True)
    if not mapped:
        raise SystemExit("No Marburg previews could be retrieved")
    total = sum(p.stat().st_size for p in SITE.rglob("*") if p.is_file())
    if total >= 950_000_000:
        raise SystemExit(f"Pages artifact too large: {total}")


if __name__ == "__main__":
    main()
