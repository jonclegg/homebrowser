"""Save the photos of the item on a page: a listing, a product, an article.

The model points at the main photo (x, y). From that element Stealthbox climbs to
the nearest container holding several images, which is the gallery, so photos
from "similar items" carousels further down the page stay out. Without a point
it takes the page's structured data (JSON-LD `image`) and every image on the page.

Candidates come from everything a gallery uses to hold a photo before it is shown:
`src`, `srcset` (largest entry), lazy-load `data-*` attributes, `<source>`, and
links straight to image files. Each is downloaded through the browser context, so
it carries the session's cookies and fingerprint, and kept only if it decodes to a
real photo size. The same photo at several sizes is kept once, at its largest.

Some pages never put the real photos in the DOM. A car seller draws its gallery on a
canvas; the DOM holds only a blurred placeholder (`?blur=10`, 1024 px wide, so it
passes the size check) and 180 px thumbnails, while the full gallery sits in the
page's hydration data. So Stealthbox also reads the JSON in inline scripts
(including JSON nested in script strings, like Next.js `self.__next_f`), finds the
smallest object that holds every photo the page shows (matched on host and path,
since the DOM copy differs only in its size query), and takes the image URLs on
the same host inside it. URLs that ask the CDN to blur the image are skipped.
"""

from __future__ import annotations

import hashlib
import os
import re
import struct
from urllib.parse import urlparse

MIN_WIDTH = 400
MIN_HEIGHT = 250
MAX_CANDIDATES = 150
# A low-quality placeholder is usually the real photo with a blur requested from
# the image CDN: `?blur=10` (Fastly, imgix) or `e_blur` (Cloudinary).
PLACEHOLDER = re.compile(r"[?&]blur=|[/,]e_blur\b", re.IGNORECASE)

HARVEST = r"""([x, y, minImages]) => {
  const largest = (srcset) => {
    let best = null, bestSize = -1;
    for (const part of (srcset || '').split(',')) {
      const [url, size] = part.trim().split(/\s+/);
      if (!url) continue;
      const n = parseFloat(size || '1') || 1;
      if (n > bestSize) { best = url; bestSize = n; }
    }
    return best;
  };
  const absolute = (url) => {
    try { return new URL(url, location.href).href; } catch (e) { return null; }
  };
  const imgUrls = (img) => [
    largest(img.getAttribute('srcset')) || largest(img.dataset.srcset),
    ...['data-src', 'data-lazy-src', 'data-original', 'data-zoom-image', 'data-full'].map((name) => img.getAttribute(name)),
    img.currentSrc || img.getAttribute('src'),
  ];
  const collect = (scope) => {
    const urls = [];
    const add = (url) => {
      const abs = url && absolute(url);
      if (abs && abs.startsWith('http') && !urls.includes(abs)) urls.push(abs);
    };
    for (const img of scope.querySelectorAll('img')) imgUrls(img).forEach(add);
    for (const source of scope.querySelectorAll('source')) {
      add(largest(source.getAttribute('srcset')) || largest(source.dataset.srcset));
    }
    for (const link of scope.querySelectorAll('a[href]')) {
      if (/\.(jpe?g|png|webp|avif)(\?|$)/i.test(link.getAttribute('href'))) add(link.getAttribute('href'));
    }
    return urls;
  };

  // The photos the page shows (rendered at least 100 px each way, so not icons),
  // as host + path: the page's data holds the same photo without the size query.
  const photoPath = (url) => { const parsed = new URL(url); return parsed.host + parsed.pathname; };
  const shownPaths = (scope) => {
    const paths = new Set();
    for (const img of scope.querySelectorAll('img')) {
      const box = img.getBoundingClientRect();
      if (box.width < 100 || box.height < 100) continue;
      for (const url of imgUrls(img)) {
        const abs = url && absolute(url);
        if (abs && abs.startsWith('http')) paths.add(photoPath(abs));
      }
    }
    return paths;
  };
  const imageValue = /^(https?:)?\/?\/[^\s"'<>]+\.(jpe?g|png|webp|avif|gif)(\?[^\s"'<>]*)?$/i;
  const fromData = (paths) => {
    const urls = [];
    const names = [...paths].map((path) => path.slice(path.lastIndexOf('/') + 1)).filter(Boolean);
    const mentions = (text) => names.some((name) => text.includes(name));
    // Walk JSON-like text, tracking which containers enclose each image URL; a
    // string that is itself JSON (a script string, a flight-data chunk) is walked
    // on its own. Then take the URLs in the smallest container holding every shown photo.
    const scan = (text) => {
      const stack = [];
      const found = [];
      let next = 0;
      for (let i = 0; i < text.length; i++) {
        const c = text[i];
        if (c === '{' || c === '[') { stack.push(next++); continue; }
        if (c === '}' || c === ']') { stack.pop(); continue; }
        if (c !== '"' && c !== "'") continue;
        let end = i + 1;
        while (end < text.length && text[end] !== c) end += text[end] === '\\' ? 2 : 1;
        const literal = text.slice(i, end + 1);
        i = end;
        let value = literal.slice(1, -1);
        if (c === '"') { try { value = JSON.parse(literal); } catch (e) {} }
        if (imageValue.test(value)) {
          const abs = absolute(value);
          if (abs) found.push({url: abs, within: stack.slice()});
        } else if (value.length > 200 && /[{[]/.test(value) && mentions(value)) {
          scan(value);
        }
      }
      const anchors = found.filter((item) => paths.has(photoPath(item.url)));
      if (!anchors.length) return;
      let common = anchors[0].within;
      for (const anchor of anchors) {
        let depth = 0;
        while (depth < common.length && common[depth] === anchor.within[depth]) depth++;
        common = common.slice(0, depth);
      }
      // Sharing only the outermost container means the whole page's data, not one item's.
      if (common.length < 2) return;
      const container = common[common.length - 1];
      const hosts = new Set(anchors.map((anchor) => new URL(anchor.url).host));
      for (const item of found) {
        if (item.within.includes(container) && hosts.has(new URL(item.url).host) && !urls.includes(item.url)) urls.push(item.url);
      }
    };
    if (!names.length) return urls;
    for (const script of document.querySelectorAll('script:not([src])')) {
      if (mentions(script.textContent)) scan(script.textContent);
    }
    return urls;
  };
  const withData = (urls, scope) => urls.concat(fromData(shownPaths(scope)).filter((url) => !urls.includes(url)));

  if (x !== null && y !== null) {
    let el = document.elementFromPoint(x, y);
    while (el && el !== document.documentElement) {
      if (collect(el).length >= minImages) return {scope: el.tagName.toLowerCase(), urls: withData(collect(el), el)};
      el = el.parentElement;
    }
  }

  const structured = [];
  const walk = (node) => {
    if (!node || typeof node !== 'object') return;
    if (Array.isArray(node)) { node.forEach(walk); return; }
    for (const value of [].concat(node.image || [])) {
      const url = typeof value === 'string' ? value : (value && (value.contentUrl || value.url));
      const abs = url && absolute(url);
      if (abs && !structured.includes(abs)) structured.push(abs);
    }
    for (const key of Object.keys(node)) if (key !== 'image') walk(node[key]);
  };
  for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
    try { walk(JSON.parse(script.textContent)); } catch (e) {}
  }
  const page = collect(document);
  return {scope: 'page', urls: withData(structured.concat(page.filter((url) => !structured.includes(url))), document)};
}"""


def safe_folder(name):
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    return cleaned[:60] or "images"


def image_size(blob):
    """(width, height) of a JPEG, PNG, GIF or WebP, else None."""
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", blob[16:24])
    if blob[:6] in (b"GIF87a", b"GIF89a"):
        return struct.unpack("<HH", blob[6:10])
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        chunk = blob[12:16]
        if chunk == b"VP8 ":
            width, height = struct.unpack("<HH", blob[26:30])
            return width & 0x3FFF, height & 0x3FFF
        if chunk == b"VP8L":
            bits = int.from_bytes(blob[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if chunk == b"VP8X":
            return int.from_bytes(blob[24:27], "little") + 1, int.from_bytes(blob[27:30], "little") + 1
        return None
    if blob[:2] == b"\xff\xd8":
        index = 2
        while index + 9 < len(blob):
            if blob[index] != 0xFF:
                index += 1
                continue
            marker = blob[index + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                height, width = struct.unpack(">HH", blob[index + 5:index + 9])
                return width, height
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                index += 2
                continue
            index += 2 + struct.unpack(">H", blob[index + 2:index + 4])[0]
    return None


EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}


def photo_key(url):
    """Same photo, different size: CDNs put the size in the path or query and keep
    the file name, so the file name without its extension identifies the photo."""
    name = os.path.basename(urlparse(url).path).lower()
    return os.path.splitext(name)[0] or url


def save_images(page, folder, x=None, y=None):
    """Download the item's photos on `page` into `folder`. Returns the file names."""
    found = page.evaluate(HARVEST, [x, y, 3])
    best = {}
    order = []
    seen_content = set()
    for url in found["urls"][:MAX_CANDIDATES]:
        if url.startswith("data:") or PLACEHOLDER.search(url):
            continue
        try:
            response = page.context.request.get(url, headers={"Referer": page.url}, timeout=30000)
        except Exception:  # noqa: BLE001 - one dead image URL is not a failure
            continue
        if not response.ok:
            continue
        content_type = (response.headers.get("content-type") or "").split(";")[0].strip()
        if content_type not in EXTENSIONS:
            continue
        blob = response.body()
        size = image_size(blob)
        if size is None or size[0] < MIN_WIDTH or size[1] < MIN_HEIGHT:
            continue
        digest = hashlib.sha256(blob).hexdigest()
        if digest in seen_content:
            continue
        seen_content.add(digest)
        key = photo_key(url)
        if key not in best:
            order.append(key)
        elif best[key]["area"] >= size[0] * size[1]:
            continue
        best[key] = {"blob": blob, "ext": EXTENSIONS[content_type], "area": size[0] * size[1]}

    os.makedirs(folder, exist_ok=True)
    names = []
    for number, key in enumerate(order, start=1):
        name = f"{number:02d}{best[key]['ext']}"
        with open(os.path.join(folder, name), "wb") as fh:
            fh.write(best[key]["blob"])
        names.append(name)
    return names
