# sk_office — SKAI for LibreOffice

Three LibreOffice extensions of [Skorpion OS](https://github.com/Endrx2008/skorpion_os): **SKAI** in the
sidebar of Writer, Calc and Math. *Italiano: [README.it.md](README.it.md).*

## Install

Download the `.oxt` files from the [latest release](https://github.com/Endrx2008/sk_office/releases/latest),
then in LibreOffice open **Tools → Extension Manager → Add** and select them. Restart LibreOffice; the
panel is **AI Assistant** in the sidebar (View → Sidebar).

| File | For |
|---|---|
| `sk_office-writer-ai.oxt` | Writer: chat that edits and formats the text, summary of the document |
| `sk_office-calc-ai.oxt` | Calc: chat that writes and formats cells, explanation of the selected cells, a formula panel |
| `sk_office-math-ai.oxt` | Math: suggests formulas and functions in StarMath (it also turns LaTeX into StarMath) |

The panels follow the language of LibreOffice: English, Italian, Spanish, French or German.

## How it works

SKAI never touches the document directly: it answers with JSON actions, the extension checks them and runs
them through LibreOffice, then a second answer tells you what was really done. Before SKAI removes or
changes existing text, cells or formula rows, a window shows the changes and asks (Cancel is the
default). If the document changed while SKAI was thinking, a dialog lets you choose what to keep.

## SKAI

The extensions talk to the local Ollama (`http://localhost:11434`) and use the installed model with the most parameters.
Without Ollama or a model the panel shows **AI unavailable**, the same message as every SKAI tool. In
Skorpion OS Ollama and its model come from the SKAI setup wizard, and SKAI Manager (Alt + F9) changes them.

When SKAI_ Memory is installed (it comes with the [`sk-notes`](https://github.com/Endrx2008/sk_notes)
package; then `sudo usermod -aG skai <user>`), each extension first looks up what the other SKAI apps
know, then keeps a short record in its own notes (`plugins/sk_office_writer`, `_calc`, `_math`).
Without it nothing changes.

## Build

```sh
python3 tools/build_oxt.py          # → dist/sk_office-writer-ai.oxt, -calc-ai.oxt, -math-ai.oxt
```

`writer/`, `calc/` and `math/` each hold a copy of the shared package `pythonpath/sk_office_ai/` (an
extension cannot import from another one): change all three together. `pythonpath/skai_memory.py` and
`sk_office_ai/skai_memory_bridge.py` are copies from [sk_notes](https://github.com/Endrx2008/sk_notes).

## License

GPL-3.0-or-later, see [LICENSE](LICENSE). © 2026 Enrico Miot (EndrX), founder and maintainer of Skorpion OS.
