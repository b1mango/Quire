<p align="center">
  <img src="docs/assets/quire-icon.png" width="88" height="88" alt="Quire icon">
</p>

<h1 align="center">Quire / 卷帙</h1>

<p align="center">Comics and novels, collected in your own library.</p>

<p align="center">
  <img src="https://img.shields.io/badge/platform-macOS-252525?style=flat-square" alt="Platform: macOS">
  <img src="https://img.shields.io/badge/status-v1.0_unreleased-e3aa43?style=flat-square" alt="Status: v1.0 unreleased">
  <img src="https://img.shields.io/badge/storage-local-397565?style=flat-square" alt="Local data storage">
  <a href="#license"><img src="https://img.shields.io/badge/license-pending-777777?style=flat-square" alt="License: pending"></a>
</p>

<p align="center"><a href="README.md">简体中文</a> · <strong>English</strong></p>

## About

Quire is a macOS desktop tool in development for collecting comics and novels into files for offline reading. A simple graphical interface will handle collection and export, with your books and data stored on your own computer.

## Features

- **Comic collection**: Public web pages, dynamically loaded images, and local image folders.
- **Export formats**: PDF, CBZ, and image ZIP for comics; TXT, EPUB, and PDF for novels.
- **Reading preparation**: Image compression, long-image splitting, missing-page placeholders, and interrupted download recovery.
- **Local OCR**: Optional on-device recognition for scanned novel pages.
- **Library and themes**: A local library with four interface themes.

## Get the App

Desktop 1.1 is built locally; the installer `quire-1.1.0.dmg` will be available on [Releases](https://github.com/b1mango/Quire/releases) (the first release has not been published yet). Open the `.dmg` and drag `quire.app` into Applications.

The app is not notarized by Apple: on first launch of a browser-downloaded copy, recent macOS shows "quire.app was not opened — Apple could not verify quire.app is free of malware", with only **Done** and **Move to Trash** (right-click → **Open** leads to the same dialog). Either (both verified; only needed once):

- Click **Done**, then open **System Settings → Privacy & Security**, scroll to the Security section, click **Open Anyway** next to quire, confirm, and open the app again;
- or run `xattr -dr com.apple.quarantine /Applications/quire.app` in Terminal (adjust the path if installed elsewhere), then open the app normally.

Once allowed, the app clears its own quarantine flag on first launch and the embedded core runs normally.

<a id="license"></a>

## License

A license has not yet been selected. It will be published in this repository once decided.

### Site rules, series and offline rebuilds (1.1)

Use `quire sites new example.com` to generate a TOML rule, edit `catalogue.chapter_links` and `chapter.image_selector`, then run `quire sites test example.com URL`. `quire inspect URL --explain --dump-html debug.html` reports matches and selector suggestions.

`quire series URL --split-by volume -o ./books` delivers each volume as it completes. `--split-by chapters 20` groups chapters; `--split-by size 50MB` measures actual artifacts and splits at chapter boundaries. A chapter that exceeds the size limit fails clearly with cached sources preserved. The desktop UI supports volume selection and Command-K / Control-K navigation.

`quire reassemble TASK_ID --workdir ./cache --compress small --format pdf,cbz -o rebuilt.pdf` rebuilds from retained, hash-verified manga sources without network access. Capture with `--keep-images` first. `quire profile export profile.json` and `quire profile import profile.json` transfer settings while preserving the destination machine's output directory.

The existing 200-page repeated workload now peaks at about 300MB of disk usage, down from 654MB; the 250MB optimization target is still unmet. Measuring formats separately trades additional encoding time for lower disk use. No new runtime dependencies were added.
