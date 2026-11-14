# Quire

<p align="center">
  <img src="docs/assets/quire-icon.png" alt="Quire" width="112" height="112" />
</p>

<p align="center">Collect comics and novels in your own local library for organized offline reading.</p>

<p align="center">
  <a href="https://github.com/b1mango/Quire/releases/latest"><img src="https://img.shields.io/github/v/release/b1mango/Quire?color=e3aa43" alt="latest release" /></a>
  <img src="https://img.shields.io/badge/platform-macOS-1f6feb" alt="macOS" />
  <a href="#license"><img src="https://img.shields.io/badge/license-pending-777777" alt="license pending" /></a>
</p>

<p align="center"><a href="README.md">简体中文</a> · <strong>English</strong></p>

## Features

- Collect public pages, dynamically loaded images, and local image folders as comics or novels; recognizes common site templates (Biquge-style, MacCMS, MCCMS, Madara), paginated catalogues, and "all chapters" links.
- Import local files: TXT is re-encoded, split into chapters, cleaned of ads, unwrapped from hard line breaks, and converted to EPUB; EPUB ad paragraphs are removed; PDF, CBZ, ZIP (including books inside archives), and image folders (packed as CBZ) are supported, with title and author confirmed before import and written into EPUB metadata.
- Built-in reader: paged or scrolling novels with custom typography; single-page, two-page, or continuous webtoon comics; image-only PDFs read as comics.
- Export comics to PDF, CBZ, or image ZIP; export novels to TXT, EPUB, or PDF.
- Image compression, long-image splitting, missing-page placeholders, and interrupted-download recovery.
- Optional local OCR for scanned novel pages.
- Follows the macOS system proxy and picks proxy or direct per site automatically.
- Local library management with light / dark modes and three accent colors.
- Split series by volume, chapter count, or actual artifact size as each volume completes.
- Rebuild offline from retained originals without fetching missing or damaged source images.

## Installation

Download `quire-<version>.dmg` from [Releases](https://github.com/b1mango/Quire/releases/latest), open it, and drag **quire** into **Applications**. Requires macOS 13 or later; universal for Intel and Apple silicon; the Python runtime is bundled.

The build is ad-hoc signed and not notarized: on first launch, right-click the app in Finder and choose **Open**, or choose **Open Anyway** in **System Settings → Privacy & Security**; alternatively run `xattr -dr com.apple.quarantine /Applications/quire.app`.

See [CHANGELOG.md](CHANGELOG.md) for release notes (in Chinese).

Running from source requires Python 3.12 or newer:

```bash
python -m pip install -e '.[core,dev]'
quire ui
```

## Development

```bash
quire sites new example.com
quire sites test example.com https://example.com/book
quire inspect https://example.com/book --explain --dump-html debug.html
```

<details>
<summary>Series, offline rebuilds, and profile commands</summary>

```bash
quire series https://example.com/book --site example.com --split-by volume -o ./books
quire series https://example.com/book --split-by chapters 20 --from 1 --to 60 -o ./books
quire series https://example.com/book --split-by size 50MB -o ./books

quire manga https://example.com/chapter --core --keep-images --workdir ./cache -o book.pdf
quire reassemble TASK_ID --workdir ./cache --compress small --format pdf,cbz -o rebuilt.pdf

quire profile export profile.json
quire profile import profile.json
quire doctor
```

`reassemble` uses only retained, hash-verified originals. Profile transfer does not include the destination machine's output directory. Size-based splitting measures final artifacts and splits at chapter boundaries; increase the limit or lower image quality when one chapter exceeds it. Large images, multiple formats, and retained originals require additional disk space.

</details>

## License

<a id="license"></a>

No license has been selected yet. It will be published in this repository once decided.
