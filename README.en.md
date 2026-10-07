# Quire

<p align="center">
  <img src="docs/assets/quire-icon.png" alt="Quire" width="112" height="112" />
</p>

<p align="center">Collect comics and novels in your own local library for organized offline reading.</p>

<p align="center">
  <img src="https://img.shields.io/badge/status-unreleased-e3aa43" alt="status unreleased" />
  <img src="https://img.shields.io/badge/platform-macOS-1f6feb" alt="macOS" />
  <a href="#license"><img src="https://img.shields.io/badge/license-pending-777777" alt="license pending" /></a>
</p>

<p align="center"><a href="README.md">简体中文</a> · <strong>English</strong></p>

## Features

- Collect public pages, dynamically loaded images, and local image folders as comics or novels.
- Export comics to PDF, CBZ, or image ZIP; export novels to TXT, EPUB, or PDF.
- Image compression, long-image splitting, missing-page placeholders, and interrupted-download recovery.
- Optional local OCR for scanned novel pages.
- Local library management with four interface themes.
- Split series by volume, chapter count, or actual artifact size as each volume completes.
- Rebuild offline from retained originals without fetching missing or damaged source images.

## Installation

No GitHub Release is published yet. Python 3.12 or newer is required:

```bash
python -m pip install -e '.[core,dev]'
quire ui
```

An unsigned local macOS build may require **Open Anyway** in **System Settings → Privacy & Security** on first launch, or `xattr -dr com.apple.quarantine /Applications/quire.app` (adjust the path to the actual installation).

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
