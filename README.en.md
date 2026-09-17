<p align="center">
  <img src="docs/assets/quire.svg" width="88" height="88" alt="Quire icon">
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

Desktop 1.0 is complete; the installer `quire-1.0.0.dmg` will be available on [Releases](https://github.com/b1mango/Quire/releases) (the first release has not been published yet). Open the `.dmg` and drag `quire.app` into Applications.

The app is not notarized by Apple: on first launch of a browser-downloaded copy, macOS will say the developer cannot be verified. Either:

- Right-click `quire.app` → **Open** → **Open** again;
- or run `xattr -dr com.apple.quarantine /Applications/quire.app` in Terminal.

<a id="license"></a>

## License

A license has not yet been selected. It will be published in this repository once decided.
