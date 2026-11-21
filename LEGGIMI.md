# Copy-bot Bitpanda Fusion (simulazione su GitHub)

Ogni 30 minuti GitHub Actions avvia `bot.py once`: legge le nuove operazioni del wallet seguito e le copia
in **simulazione** (1000 € finti, prezzi veri di Bitpanda Fusion). Qui non vengono mai eseguiti ordini reali:
il workflow si blocca se in `config.json` c'è `"mode"` diverso da `"paper"`.

- `state.json`: ultimo blocco letto e portafoglio simulato
- `trades.csv`: tutte le operazioni simulate
- `bot.log`: registro di ogni giro

## Chiavi
Le chiavi stanno nei *secrets* del repository (Settings → Secrets and variables → Actions):
`ETHERSCAN_API_KEY` e `BITPANDA_API_KEY` (chiave Fusion con sola lettura).

## Comandi utili
- Avvio manuale: tab **Actions** → **copybot** → **Run workflow**
- Mettere in pausa: tab **Actions** → **copybot** → **…** → **Disable workflow**
- Vedere il portafoglio: scarica il repository e lancia `python3 bot.py report` (servono le chiavi)
