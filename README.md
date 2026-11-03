# SKAI for LibreOffice

AI extensions for LibreOffice Writer, Calc and Math, part of [Skorpion OS](https://github.com/Endrx2008/skorpion_os).

## Install
Download the `.oxt` files from the latest release, then in LibreOffice open
**Tools > Extension Manager > Add** and select them.

## SKAI_ Memory
Each extension uses the shared SKAI memory (`/var/lib/skai/SKAI_ Memory`, library `skai_memory`, installed with
the `sk-notes` package): before answering it looks up what other SKAI apps know, afterwards it keeps a short
record in its own notes (`plugins/sk_libre_writer`, `_calc`, `_math`). Without the memory nothing changes.

## Build
`python3 tools/build_oxt.py` writes the three `.oxt` files to `dist/`.
