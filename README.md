# streamdoc-dl

> Slop coded. PRs welcome.

Download PDFs from ePapyrus StreamDocs viewers.

StreamDocs is an HTML5 document viewer by ePapyrus used on Korean government sites
(standard.go.kr, e-book.scourt.go.kr, etc.) that renders PDFs as page images
in-browser with download disabled. This tool reconstructs the PDF by downloading
page images and text data, producing a searchable PDF with selectable text.

## Install

```
pip install streamdoc-dl
```

## Usage

```
streamdoc-dl 'https://www.standard.go.kr/streamdocs/view/sd;streamdocsId=1234567890'
```

Output filename is auto-detected from the server metadata.

### Options

```
streamdoc-dl URL [-o OUTPUT] [-z ZOOM] [-j JOBS] [--font FONT] [--compress [LEVEL]] [--tor]
```

| Option | Default | Description |
|--------|---------|-------------|
| `-o` | auto | Output PDF path |
| `-z` | max | Zoom level: `max` for highest quality, or a number (100=native, 200=2x, 300=3x) |
| `-j` | 4 | Concurrent download threads |
| `--font` | auto | Path to TTF font for text layer |
| `--compress` | off | Compress with Ghostscript (screen/ebook/printer/prepress) |
| `--tor` | off | Route traffic through Tor (SOCKS5 proxy on 127.0.0.1:9050) |

### Compression

Use `--compress` to reduce output size via Ghostscript (`gs` must be installed):

```
streamdoc-dl URL --compress          # default: ebook quality
streamdoc-dl URL --compress printer  # higher quality
```

### Font auto-detection

The invisible text layer needs a Korean-capable font for search/copy. The tool tries:

1. NanumGothic from common system paths
2. `fc-match :lang=ko` (Linux with fontconfig)
3. Fallback to reportlab built-in CID font (HYSMyeongJo-Medium)

To use a specific font: `--font /path/to/font.ttf`

### Tor

Use `--tor` to route all traffic through Tor (must be running on port 9050):

```
tor &
streamdoc-dl URL --tor
```

### Resume

Interrupted downloads are automatically resumed. Page images and text are cached
as they download. Re-run the same command to continue from where it left off.
The cache is cleaned up after the PDF is built.

Cache location:
- Linux: `$XDG_CACHE_HOME/streamdoc-dl/` or `~/.cache/streamdoc-dl/`
- macOS: `~/Library/Caches/streamdoc-dl/`
- Windows: `%LOCALAPPDATA%\streamdoc-dl\`

## How it works

1. Fetches document metadata from the StreamDocs v4 API
2. If the server allows direct download, fetches the original PDF
3. Otherwise, downloads page images and text data concurrently
4. Fixes the corrupted first byte on each image (server-side anti-scrape)
5. Builds a PDF with image backgrounds and invisible text overlay

## License

GPL-3.0-or-later
