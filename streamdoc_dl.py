#!/usr/bin/env python3
"""Download PDFs from StreamDocs viewers (e.g. standard.go.kr)."""

import argparse
import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path

import requests
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas


def parse_streamdocs_url(url: str) -> tuple[str, str]:
    """Extract base URL and document ID from a StreamDocs viewer URL."""
    if ";streamdocsId=" in url:
        doc_id = url.split(";streamdocsId=")[-1].strip("/")
        base_url = url.split("/view/")[0]
        return base_url, doc_id
    raise ValueError(f"Cannot parse StreamDocs URL: {url}")


def get_document_info(session: requests.Session, base_url: str, doc_id: str) -> dict:
    """Fetch document metadata: page layouts, filename, permissions."""
    url = f"{base_url}/v4/documents/{doc_id}/document"
    resp = session.get(url)
    resp.raise_for_status()

    heads = resp.headers.get("sd-body-heads", "").split(",")
    sizes = [int(s) for s in resp.headers.get("sd-body-sizes", "").split(",")]

    data = resp.content
    offset = 0
    result = {}
    for name, size in zip(heads, sizes):
        chunk = data[offset : offset + size]
        offset += size
        if name == "document":
            doc = json.loads(chunk)
            result["layouts"] = doc.get("layout", [])
            result["page_count"] = len(result["layouts"])
            result["info"] = doc.get("info", {})
            result["filename"] = result["info"].get("FileName", "")
        elif name == "authorize":
            auth = json.loads(chunk)
            result["download"] = auth.get("download", False)

    if "page_count" not in result:
        raise RuntimeError("Could not find layout in document response")
    return result


def try_direct_download(
    session: requests.Session, base_url: str, doc_id: str
) -> bytes | None:
    """Try to download the original PDF directly via /source endpoint."""
    url = f"{base_url}/v4/documents/{doc_id}/source"
    resp = session.get(url, allow_redirects=True)
    if resp.status_code == 200 and resp.content[:4] == b"%PDF":
        return resp.content
    return None


def fix_image_bytes(data: bytes, content_type: str) -> bytes:
    """Fix the first byte that the server corrupts as anti-scrape."""
    buf = bytearray(data)
    if "png" in content_type:
        buf[0] = 0x89
    elif "jp" in content_type:
        buf[0] = 0xFF
    return bytes(buf)


def download_page_image(
    session: requests.Session,
    base_url: str,
    doc_id: str,
    page_index: int,
    zoom: int,
) -> tuple[int, bytes]:
    """Download a single page image. Returns (index, image_bytes)."""
    url = (
        f"{base_url}/v4/documents/{doc_id}/renderings/{page_index}"
        f"?zoom={zoom}&jpegQuality=h&renderAnnots=false&increasePrint=false"
    )
    resp = session.get(url)
    resp.raise_for_status()
    ct = resp.headers.get("x-streamdocs-content-type", "")
    return page_index, fix_image_bytes(resp.content, ct)


def download_page_text(
    session: requests.Session,
    base_url: str,
    doc_id: str,
    page_index: int,
) -> tuple[int, list]:
    """Download text blocks for a page. Returns (index, text_blocks)."""
    url = f"{base_url}/v4/documents/{doc_id}/texts/{page_index}"
    resp = session.get(url)
    resp.raise_for_status()
    return page_index, resp.json()


def find_font(font_path: str | None) -> str | None:
    """Find a Korean TTF font. Returns path or None to use CID fallback."""
    if font_path:
        p = Path(font_path)
        if p.exists():
            return str(p)
        raise FileNotFoundError(f"Font not found: {font_path}")

    # Try common paths
    candidates = [
        "/usr/share/fonts/TTF/NanumGothic.ttf",
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
        "/usr/share/fonts/nanum-fonts/NanumGothic.ttf",
    ]
    for p in candidates:
        if Path(p).exists():
            return p

    # Try fc-match
    if shutil.which("fc-match"):
        try:
            result = subprocess.run(
                ["fc-match", "-f", "%{file}", ":lang=ko"],
                capture_output=True,
                text=True,
            )
            path = result.stdout.strip()
            if path and Path(path).exists() and path.endswith(".ttf"):
                return path
        except OSError:
            pass

    return None


def register_font(font_path: str | None) -> str:
    """Register a font and return its name for use in the canvas."""
    path = find_font(font_path)
    if path:
        from reportlab.pdfbase.ttfonts import TTFont

        pdfmetrics.registerFont(TTFont("TextFont", path))
        return "TextFont"

    # Fallback: CID font (built into reportlab, no file needed)
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    pdfmetrics.registerFont(UnicodeCIDFont("HYSMyeongJo-Medium"))
    return "HYSMyeongJo-Medium"


def build_pdf(layouts, images, texts, output_path, doc_info=None, font_name="Helvetica"):
    """Build a PDF with image backgrounds and invisible text overlay."""
    c = canvas.Canvas(str(output_path))

    if doc_info:
        c.setTitle(doc_info.get("Title", "") or doc_info.get("FileName", ""))
        c.setAuthor(doc_info.get("Author", ""))
        c.setSubject(doc_info.get("Subject", ""))
        c.setKeywords(doc_info.get("Keywords", ""))
        c.setCreator(doc_info.get("Creator", ""))
        c.setProducer(doc_info.get("Producer", ""))

    for i, layout in enumerate(layouts):
        w = layout["bbox"]["w"]
        h = layout["bbox"]["h"]
        c.setPageSize((w, h))

        if images[i]:
            img = ImageReader(BytesIO(images[i]))
            c.drawImage(img, 0, 0, width=w, height=h)

        if texts[i]:
            c.setFillAlpha(0)
            for block in texts[i]:
                text_str = block.get("text", "")
                rects = block.get("rect", [])
                if not text_str or not rects:
                    continue

                for ch, rect in zip(text_str, rects):
                    left = rect["left"]
                    bottom = rect["bottom"]
                    top = rect["top"]
                    font_size = max(top - bottom, 1)
                    c.setFont(font_name, font_size)
                    c.drawString(left, bottom, ch)

        c.showPage()

    c.save()


def compress_pdf(path: str, level: str = "ebook"):
    """Compress PDF using Ghostscript."""
    gs = shutil.which("gs")
    if not gs:
        print("Warning: Ghostscript (gs) not found, skipping compression")
        return

    tmp = path + ".tmp"
    cmd = [
        gs, "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.4",
        f"-dPDFSETTINGS=/{level}",
        "-dNOPAUSE", "-dBATCH", "-dQUIET",
        f"-sOutputFile={tmp}", path,
    ]
    subprocess.run(cmd, check=True)

    original = Path(path).stat().st_size
    compressed = Path(tmp).stat().st_size
    if compressed < original:
        Path(tmp).rename(path)
        print(f"Compressed: {original // 1024}K -> {compressed // 1024}K")
    else:
        Path(tmp).unlink()
        print("Compression would increase size, skipped")


def main():
    parser = argparse.ArgumentParser(description="Download PDF from StreamDocs viewer")
    parser.add_argument("url", help="StreamDocs viewer URL")
    parser.add_argument("-o", "--output", help="Output PDF path")
    parser.add_argument(
        "-z", "--zoom", type=int, default=300, help="Zoom level (default: 300)"
    )
    parser.add_argument(
        "-j", "--jobs", type=int, default=4, help="Concurrent downloads (default: 4)"
    )
    parser.add_argument(
        "--font", help="Path to TTF font for text layer (auto-detected if omitted)"
    )
    parser.add_argument(
        "--compress",
        nargs="?",
        const="ebook",
        metavar="LEVEL",
        help="Compress PDF with Ghostscript (levels: screen, ebook, printer, prepress; default: ebook)",
    )
    parser.add_argument(
        "--tor",
        action="store_true",
        help="Route traffic through Tor (SOCKS5 proxy on 127.0.0.1:9050)",
    )
    args = parser.parse_args()

    base_url, doc_id = parse_streamdocs_url(args.url)
    print(f"Base: {base_url}")
    print(f"Document ID: {doc_id}")

    session = requests.Session()
    if args.tor:
        proxy = "socks5h://127.0.0.1:9050"
        session.proxies = {"http": proxy, "https": proxy}
        print("Using Tor proxy")
    session.get(f"{base_url}/view/sd;streamdocsId={doc_id}")

    info = get_document_info(session, base_url, doc_id)
    page_count = info["page_count"]
    layouts = info["layouts"]
    filename = info.get("filename", "")
    print(f"Pages: {page_count}")
    if filename:
        print(f"Filename: {filename}")

    # Try direct download first
    if info.get("download"):
        print("Direct download allowed, trying /source...")
        pdf = try_direct_download(session, base_url, doc_id)
        if pdf:
            output = args.output or filename or f"{doc_id[:32]}.pdf"
            Path(output).write_bytes(pdf)
            print(f"Saved: {output}")
            return

    font_name = register_font(args.font)

    # Load cached pages
    cache_dir = Path(f".streamdoc-dl-cache/{doc_id}")
    images = [None] * page_count
    texts = [None] * page_count
    for i in range(page_count):
        img_path = cache_dir / f"{i}.img"
        txt_path = cache_dir / f"{i}.json"
        if img_path.exists():
            images[i] = img_path.read_bytes()
        if txt_path.exists():
            texts[i] = json.loads(txt_path.read_text())

    # Download missing pages
    missing = []
    for i in range(page_count):
        if images[i] is None:
            missing.append(("img", i))
        if texts[i] is None:
            missing.append(("txt", i))

    if missing:
        cached = page_count * 2 - len(missing)
        if cached:
            print(f"Resuming: {cached}/{page_count * 2} cached")
        cache_dir.mkdir(parents=True, exist_ok=True)
        done = 0
        total = len(missing)

        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = {}
            for kind, i in missing:
                if kind == "img":
                    futures[
                        pool.submit(download_page_image, session, base_url, doc_id, i, args.zoom)
                    ] = ("img", i)
                else:
                    futures[
                        pool.submit(download_page_text, session, base_url, doc_id, i)
                    ] = ("txt", i)

            for future in as_completed(futures):
                kind, _ = futures[future]
                idx, data = future.result()
                if kind == "img":
                    images[idx] = data
                    (cache_dir / f"{idx}.img").write_bytes(data)
                else:
                    texts[idx] = data
                    (cache_dir / f"{idx}.json").write_text(json.dumps(data))
                done += 1
                print(f"\rDownloading: {done}/{total}", end="", flush=True)

        print(" done.")
    else:
        print("All pages cached.")

    output = args.output
    if not output:
        output = filename if filename else f"{doc_id[:32]}.pdf"

    print("Building PDF...")
    build_pdf(layouts, images, texts, output, doc_info=info.get("info", {}), font_name=font_name)

    if args.compress:
        print(f"Compressing ({args.compress})...")
        compress_pdf(output, args.compress)

    # Clean up cache
    shutil.rmtree(cache_dir, ignore_errors=True)
    try:
        cache_dir.parent.rmdir()
    except OSError:
        pass

    print(f"Saved: {output}")


if __name__ == "__main__":
    main()
