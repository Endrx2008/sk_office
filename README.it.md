# sk_office — SKAI per LibreOffice

Tre estensioni di LibreOffice per [Skorpion OS](https://github.com/Endrx2008/skorpion_os): **SKAI** nella
barra laterale di Writer, Calc e Math. *English: [README.md](README.md).*

## Installazione

Scarica i file `.oxt` dall'[ultima release](https://github.com/Endrx2008/sk_office/releases/latest), poi in
LibreOffice apri **Strumenti → Gestione estensioni → Aggiungi** e selezionali. Riavvia LibreOffice; il
pannello si chiama **Assistente IA** nella barra laterale (Visualizza → Barra laterale).

| File | Per |
|---|---|
| `sk_office-writer-ai.oxt` | Writer: chat che modifica e formatta il testo, riassunto del documento |
| `sk_office-calc-ai.oxt` | Calc: chat che scrive e formatta le celle, spiegazione delle celle selezionate, un pannello delle formule |
| `sk_office-math-ai.oxt` | Math: suggerisce formule e funzioni in StarMath (converte anche il LaTeX in StarMath) |

I pannelli seguono la lingua di LibreOffice: inglese, italiano, spagnolo, francese o tedesco.

## Come funziona

SKAI non tocca mai direttamente il documento: risponde con azioni JSON, l'estensione le controlla e le
esegue tramite LibreOffice, poi una seconda risposta ti dice cosa è stato fatto davvero. Prima che SKAI
rimuova o cambi testo, celle o righe di formula esistenti, una finestra mostra le modifiche e chiede
conferma (Annulla è la scelta predefinita). Se il documento è cambiato mentre SKAI pensava, una finestra ti fa scegliere cosa tenere.

## SKAI

Le estensioni parlano con Ollama in locale (`http://localhost:11434`) e usano il modello installato con
più parametri. Senza Ollama o senza un modello il pannello mostra **IA non disponibile**, lo stesso messaggio
di tutti gli strumenti SKAI. In Skorpion OS Ollama e il modello arrivano dalla procedura guidata di SKAI, e SKAI Manager
(Alt + F9) li cambia.

Se SKAI_ Memory è installata (arriva con il pacchetto [`sk-notes`](https://github.com/Endrx2008/sk_notes);
poi `sudo usermod -aG skai <utente>`), ogni estensione prima cerca cosa sanno le altre app SKAI, poi tiene
una breve nota nelle sue pagine (`plugins/sk_office_writer`, `_calc`, `_math`). Senza, non cambia nulla.

## Compilazione

```sh
python3 tools/build_oxt.py          # → dist/sk_office-writer-ai.oxt, -calc-ai.oxt, -math-ai.oxt
```

`writer/`, `calc/` e `math/` contengono ciascuna una copia del pacchetto comune `pythonpath/sk_office_ai/`
(un'estensione non può importare da un'altra): modificale tutte e tre insieme. `pythonpath/skai_memory.py` e
`sk_office_ai/skai_memory_bridge.py` sono copie da [sk_notes](https://github.com/Endrx2008/sk_notes).

## Licenza

GPL-3.0-or-later, vedi [LICENSE](LICENSE). © 2026 Enrico Miot (EndrX), fondatore e manutentore di Skorpion OS.
