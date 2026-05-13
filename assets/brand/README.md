# sembr brand assets

Canonical brand assets for the **sembr** open-source project.

## Files

| File | Use |
| --- | --- |
| `logo-mark.svg` | Icon-only mark, delicate stroke. Use at any size ≥ 64px (README, docs site, slides). |
| `favicon.svg` | Icon-only mark, chunkier stroke for legibility at 16×16 / 32×32. Wire as `<link rel="icon" type="image/svg+xml">`. |
| `logo-mark.png` | 1024×1024 raster of the mark on warm cream `#FBF7EE`. Used where SVG isn't accepted (PyPI, some social platforms). |
| `logo-lockup.png` | 1376×768 horizontal lockup: mark + `sembr` wordmark. Use for README header, email signature, talk slides. |
| `hero.png` | 1376×768 wide hero banner: lockup left + editorial hairline right. Use as README hero image and GitHub social preview. |

## Brand specs

| Token | Hex | Role |
| --- | --- | --- |
| Primary (ink) | `#1A1A1A` | mark strokes, wordmark, body text |
| Accent (gold) | `#C2A03A` | center dot — only color element other than ink (inherited from Peakstone Labs parent brand) |
| Background (cream) | `#FBF7EE` | canvas |
| Hairline | `#C8C0A8` | editorial dividers, subtle separators |

Wordmark typeface: **IBM Plex Sans** (SIL OFL 1.1) — `Medium` weight for the lockup, `Regular` for body. Load via Google Fonts.

## Visual concept

The mark is a single hourglass formed by two opposing **thin-stroke triangle outlines** meeting at a center point, with a single **gold dot** at the meeting point. The form encodes the "reverse RAG" concept (intent at the narrow center, content flowing through over time) while staying minimal — visual language inspired by Vercel, Anthropic, Linear, shadcn/ui, and Resend.

## Sibling brand context

sembr is published by **Peakstone Labs**. The shared visual lineage is the **ink + gold** palette; the gold dot at the mark's center is a deliberate quiet reference to the gold accent stripe on the Peakstone Labs corporate logo. sembr's form (thin outlines, breathing room, single hourglass) is its own — appropriate for an open-source developer tool rather than a corporate identity.

## Source

Mark design generated and iterated 2026-05-13 (Nano Banana / Gemini 2.5 Flash Image), then hand-coded to SVG. Iteration history and prompt records live in the private `sembr-dev-docs/opensource/01-proposal/` workspace.
