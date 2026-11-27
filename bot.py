#!/usr/bin/env python3
"""
Copy-bot per Bitpanda Fusion.

Segue alcuni wallet Ethereum pubblici e replica le loro operazioni:
  - il wallet compra un token (lo riceve in una transazione avviata da lui) -> il bot compra
  - il wallet vende un token (lo invia in una transazione avviata da lui)   -> il bot vende tutta la sua posizione

Di default lavora in SIMULAZIONE (paper trading): nessun ordine reale.
Per il trading reale servono: "mode": "live" in config.json, il flag --live e una conferma.

Solo libreria standard di Python 3.9+. Nessun pacchetto da installare.

Comandi:
  python bot.py check     controlla le chiavi e quali token seguiti si possono comprare su Bitpanda
  python bot.py once      un solo giro di controllo
  python bot.py run       gira in continuo (Ctrl+C per fermarlo)
  python bot.py report    mostra il portafoglio simulato e il guadagno/perdita
"""
import argparse
import csv
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
FUSION_URL = "https://api.fusion.bitpanda.com/v1"
ETHERSCAN_URL = "https://api.etherscan.io/v2/api"
STOP_FILE = BASE_DIR / "STOP"


# ---------------------------------------------------------------- utilità

def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def _ssl_context():
    """Il Python di python.org su macOS spesso non trova i certificati: uso quelli di sistema."""
    ctx = ssl.create_default_context()
    for path in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt"):
        if os.path.exists(path):
            try:
                ctx.load_verify_locations(cafile=path)
                break
            except (ssl.SSLError, OSError):
                pass
    return ctx


SSL_CTX = _ssl_context()


def http_json(url, method="GET", headers=None, body=None, timeout=20):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise RuntimeError(f"HTTP {e.code} su {url.split('?')[0]}: {detail}") from None


def round_down(value, increment):
    """Arrotonda per difetto al multiplo di 'increment' (es. 0.00001)."""
    if not increment:
        return float(Decimal(str(value)).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN))
    inc = Decimal(str(increment))
    return float((Decimal(str(value)) / inc).to_integral_value(rounding=ROUND_DOWN) * inc)


def load_json(path, default):
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)


# ---------------------------------------------------------------- client API

class Etherscan:
    def __init__(self, key, chainid=1, fetch=http_json, pause=0.4):
        self.key, self.chainid, self.fetch, self.pause = key, chainid, fetch, pause

    def _get(self, **params):
        params.update(chainid=self.chainid, apikey=self.key)
        data = self.fetch(ETHERSCAN_URL + "?" + urllib.parse.urlencode(params))
        time.sleep(self.pause)  # limite gratuito: ~5 richieste al secondo
        if params.get("module") == "proxy":
            return data.get("result")
        if str(data.get("status")) == "1":
            return data["result"]
        if "No transactions found" in str(data.get("message", "")) or data.get("result") == []:
            return []
        raise RuntimeError(f"Etherscan: {data.get('message')} - {data.get('result')}")

    def latest_block(self):
        return int(self._get(module="proxy", action="eth_blockNumber"), 16)

    def token_transfers(self, address, startblock):
        return self._get(module="account", action="tokentx", address=address,
                         startblock=startblock, endblock=99999999, sort="asc")

    def internal_txs(self, address, startblock):
        return self._get(module="account", action="txlistinternal", address=address,
                         startblock=startblock, endblock=99999999, sort="asc")

    def normal_txs(self, address, startblock):
        return self._get(module="account", action="txlist", address=address,
                         startblock=startblock, endblock=99999999, sort="asc")


class Fusion:
    def __init__(self, key, fetch=http_json):
        self.key, self.fetch = key, fetch

    def _h(self):
        return {"x-api-key": self.key, "Accept": "application/json"}

    def pairs(self):
        return {p["pair"]: p for p in self.fetch(f"{FUSION_URL}/pairs", headers=self._h())}

    def prices(self, pairs):
        if not pairs:
            return {}
        q = urllib.parse.urlencode({"pair": ",".join(sorted(pairs))})
        data = self.fetch(f"{FUSION_URL}/tickers?{q}", headers=self._h())
        return {t["pair"]: float(t["price"]) for t in data}

    def candles(self, pair, interval="1h", limit=60):
        q = urllib.parse.urlencode({"interval": interval, "limit": limit})
        data = self.fetch(f"{FUSION_URL}/candles/{pair}?{q}", headers=self._h())
        return sorted(data, key=lambda c: int(c["timestamp"]))

    def balances(self):
        data = self.fetch(f"{FUSION_URL}/account/balances", headers=self._h())
        return {b["symbol"]: float(b["available"]) for b in data}

    def create_order(self, body):
        return self.fetch(f"{FUSION_URL}/account/orders", method="POST", headers=self._h(), body=body)

    def get_order(self, order_id):
        return self.fetch(f"{FUSION_URL}/account/orders/{order_id}", headers=self._h())


# ---------------------------------------------------------------- segnali

def extract_signals(wallet, token_txs, normal_txs, tokens, require_initiated=True,
                    internal_txs=None, trusted=None, eth_asset="ETH"):
    """
    Trasforma i movimenti di un wallet in segnali Buy/Sell.

    tokens: {indirizzo_contratto_minuscolo: simbolo_bitpanda}. Si usano gli indirizzi dei
    contratti e non i simboli, perché esistono token-truffa con lo stesso simbolo.

    require_initiated: conta solo le transazioni avviate dal wallet stesso (uno swap fatto da lui),
    e ignora i token ricevuti passivamente (airdrop, spam, invii da altri).

    trusted: indirizzi di exchange decentralizzati (es. CoW Protocol) i cui scambi contano anche
    se la transazione l'ha inviata un altro (con CoW la manda un "solver", non il wallet).
    internal_txs: ETH nativo ricevuto da un indirizzo fidato = acquisto di ETH.
    """
    w = wallet.lower()
    trusted = {a.lower() for a in (trusted or [])}
    initiated = {t["hash"] for t in normal_txs if t.get("from", "").lower() == w}
    merged = {}

    def add(h, block, ts, asset, side, qty):
        key = (h, asset, side)
        if key in merged:
            merged[key]["wallet_qty"] += qty
        else:
            merged[key] = {"wallet": w, "hash": h, "block": int(block), "time": int(ts),
                           "asset": asset, "side": side, "wallet_qty": qty}

    for t in token_txs:
        contract = t.get("contractAddress", "").lower()
        if contract not in tokens:
            continue
        frm, to = t.get("from", "").lower(), t.get("to", "").lower()
        if to == w:
            side, other = "Buy", frm
        elif frm == w:
            side, other = "Sell", to
        else:
            continue
        if require_initiated and t["hash"] not in initiated and other not in trusted:
            continue
        qty = int(t["value"]) / 10 ** int(t.get("tokenDecimal") or 18)
        add(t["hash"], t["blockNumber"], t["timeStamp"], tokens[contract], side, qty)

    for it in internal_txs or []:
        if str(it.get("isError", "0")) != "0":
            continue
        if it.get("to", "").lower() == w and it.get("from", "").lower() in trusted and int(it["value"]) > 0:
            add(it["hash"], it["blockNumber"], it["timeStamp"], eth_asset, "Buy", int(it["value"]) / 1e18)

    # se nella stessa transazione lo stesso token entra ed esce, non è un segnale chiaro
    out = []
    for (h, asset, side), sig in merged.items():
        opposite = (h, asset, "Sell" if side == "Buy" else "Buy")
        if opposite not in merged:
            out.append(sig)
    return sorted(out, key=lambda x: (x["block"], x["side"] != "Sell"))


INTERVAL_SECONDS = {"1m": 60, "5m": 300, "10m": 600, "15m": 900, "30m": 1800,
                    "1h": 3600, "4h": 14400, "1d": 86400}


def sma_cross(candles, fast, slow, interval="1h", now=None, mode="cross"):
    """
    Incrocio di medie mobili sui prezzi di chiusura delle candele CHIUSE.
    Restituisce (segnale, timestamp_ultima_candela_chiusa) con segnale "Buy", "Sell" o None.
    """
    now = time.time() if now is None else now
    step = INTERVAL_SECONDS.get(interval, 3600)
    closed = [c for c in candles if int(c["timestamp"]) + step <= now]
    if len(closed) < slow + 1:
        return None, None
    closes = [float(c["close"]) for c in closed]

    def sma(values, n):
        return sum(values[-n:]) / n

    fast_now, slow_now = sma(closes, fast), sma(closes, slow)
    fast_prev, slow_prev = sma(closes[:-1], fast), sma(closes[:-1], slow)
    last_ts = int(closed[-1]["timestamp"])
    sma_cross.last = (fast_now, slow_now, closes[-1])  # per il registro
    if mode == "state":
        # posizione: dentro quando la veloce è sopra la lenta, fuori quando è sotto
        return ("Buy" if fast_now > slow_now else "Sell"), last_ts
    if fast_prev <= slow_prev and fast_now > slow_now:
        return "Buy", last_ts
    if fast_prev >= slow_prev and fast_now < slow_now:
        return "Sell", last_ts
    return None, last_ts


# ---------------------------------------------------------------- bot

class CopyBot:
    def __init__(self, cfg, etherscan, fusion, live=False, state_path=None, trades_path=None):
        self.cfg, self.es, self.fu, self.live = cfg, etherscan, fusion, live
        self.state_path = state_path or BASE_DIR / "state.json"
        self.trades_path = trades_path or BASE_DIR / "trades.csv"
        self.tokens = {k.lower(): v for k, v in cfg["tokens"].items()}
        self.quote = cfg.get("quote", "EUR")
        self.state = load_json(self.state_path, {
            "wallets": {}, "seen": [],
            "paper": {"cash": cfg["paper_start_cash"], "positions": {}, "realized_pnl": 0.0},
            "live_positions": {}, "day": {"date": "", "trades": 0},
        })
        self._pairs = None

    # ---- dati di mercato
    @property
    def pairs(self):
        if self._pairs is None:
            self._pairs = self.fu.pairs()
        return self._pairs

    def pair_of(self, asset):
        return f"{asset}-{self.quote}"

    def positions(self):
        return self.state["live_positions"] if self.live else self.state["paper"]["positions"]

    # ---- registro operazioni
    def record(self, sig, pair, side, qty, price, eur, note):
        new = not self.trades_path.exists()
        with open(self.trades_path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["data", "modalita", "wallet", "tx", "coppia", "lato", "quantita",
                            "prezzo", "eur", "nota"])
            w.writerow([datetime.now().isoformat(timespec="seconds"),
                        "REALE" if self.live else "SIMULAZIONE", sig["wallet"], sig["hash"],
                        pair, side, f"{qty:.8f}", f"{price:.6f}", f"{eur:.2f}", note])

    # ---- regole di rischio
    def can_buy(self, asset):
        today = date.today().isoformat()
        if self.state["day"]["date"] != today:
            self.state["day"] = {"date": today, "trades": 0}
        if self.state["day"]["trades"] >= self.cfg["max_trades_per_day"]:
            return "raggiunto il limite di operazioni giornaliere"
        slack = 1 + self.cfg.get("fee_rate", 0) + 1e-9  # i limiti valgono sull'importo, non sulle commissioni
        pos = self.positions().get(asset, {"cost": 0})
        if pos["cost"] + self.cfg["trade_eur"] > self.cfg["max_eur_per_asset"] * slack:
            return f"limite di {self.cfg['max_eur_per_asset']} EUR su {asset}"
        invested = sum(p["cost"] for p in self.positions().values())
        if invested + self.cfg["trade_eur"] > self.cfg["max_total_eur"] * slack:
            return f"limite totale investito di {self.cfg['max_total_eur']} EUR"
        return None

    # ---- esecuzione
    def handle(self, sig):
        asset, side = sig["asset"], sig["side"]
        sig.setdefault("pos_key", asset)
        pair = self.pair_of(asset)
        if pair not in self.pairs:
            log(f"  {asset}: coppia {pair} non disponibile su Bitpanda Fusion, salto")
            return
        if side == "Buy":
            reason = self.can_buy(sig["pos_key"])
            if reason:
                log(f"  Buy {asset} saltato: {reason}")
                return
            self.buy(sig, pair)
        else:
            if self.positions().get(sig["pos_key"], {}).get("qty", 0) <= 0:
                log(f"  Sell {asset}: non ne ho, niente da vendere")
                return
            self.sell(sig, pair)

    def buy(self, sig, pair):
        asset, eur = sig["asset"], float(self.cfg["trade_eur"])
        info = self.pairs[pair]
        min_amt = float(info.get("minOrderAmount") or 0)
        if eur < min_amt:
            log(f"  Buy {asset} saltato: minimo per ordine {min_amt} EUR")
            return
        price = self.fu.prices([pair])[pair]
        fee = self.cfg["fee_rate"]
        if self.live:
            amount = round_down(eur, info.get("amountIncrement"))
            order = self.fu.create_order({"pair": pair, "side": "Buy", "type": "Market", "amount": amount})
            qty, price, eur = self.wait_fill(order, fallback_price=price, fallback_eur=amount)
            if qty <= 0:
                log(f"  Buy {asset}: ordine {order.get('id')} non eseguito ({order.get('status')})")
                return
        else:
            cash = self.state["paper"]["cash"]
            if cash < eur * (1 + fee):
                log(f"  Buy {asset} saltato: liquidità simulata insufficiente ({cash:.2f} EUR)")
                return
            qty = eur / price
            self.state["paper"]["cash"] = cash - eur * (1 + fee)
        pos = self.positions().setdefault(sig["pos_key"], {"qty": 0.0, "cost": 0.0})
        pos["qty"] += qty
        # in simulazione il costo include la commissione d'acquisto, così il risultato è netto
        pos["cost"] += eur if self.live else eur * (1 + fee)
        self.state["day"]["trades"] += 1
        self.record(sig, pair, "Buy", qty, price, eur, "strategia acquisto" if sig["wallet"].startswith("strategia") else "copia acquisto")
        log(f"  {'REALE' if self.live else 'SIM'} Buy {qty:.6f} {asset} a {price:.4f} ({eur:.2f} EUR)")

    def sell(self, sig, pair):
        asset = sig["asset"]
        info = self.pairs[pair]
        pos = self.positions()[sig["pos_key"]]
        price = self.fu.prices([pair])[pair]
        fee = self.cfg["fee_rate"]
        qty = pos["qty"]
        if self.live:
            qty = min(qty, self.fu.balances().get(asset, 0.0))
            qty = round_down(qty, info.get("sizeIncrement"))
            if qty <= 0:
                log(f"  Sell {asset}: saldo disponibile su Bitpanda nullo")
                return
            order = self.fu.create_order({"pair": pair, "side": "Sell", "type": "Market", "quantity": qty})
            qty, price, proceeds = self.wait_fill(order, fallback_price=price, fallback_eur=qty * price)
            if qty <= 0:
                log(f"  Sell {asset}: ordine {order.get('id')} non eseguito ({order.get('status')})")
                return
        else:
            proceeds = qty * price
            self.state["paper"]["cash"] += proceeds * (1 - fee)
        cost_part = pos["cost"] * min(1.0, qty / pos["qty"])
        pnl = proceeds * (1 - fee) - cost_part
        if not self.live:
            self.state["paper"]["realized_pnl"] += pnl
        pos["qty"] -= qty
        pos["cost"] -= cost_part
        # i residui troppo piccoli (arrotondamenti, commissioni) non si possono vendere: li tolgo
        if pos["qty"] <= 1e-12 or pos["qty"] * price < float(info.get("minOrderAmount") or 0):
            del self.positions()[sig["pos_key"]]
        self.state["day"]["trades"] += 1
        self.record(sig, pair, "Sell", qty, price, proceeds, ("strategia" if sig["wallet"].startswith("strategia") else "copia") + f" vendita, risultato {pnl:+.2f} EUR")
        log(f"  {'REALE' if self.live else 'SIM'} Sell {qty:.6f} {asset} a {price:.4f} -> {pnl:+.2f} EUR")

    def wait_fill(self, order, fallback_price, fallback_eur):
        """Attende che un ordine a mercato venga eseguito e restituisce (quantità, prezzo medio, eur)."""
        oid = order.get("id")
        for _ in range(5):
            time.sleep(2)
            o = self.fu.get_order(oid)
            if o.get("status") in ("filled", "filled-and-canceled", "canceled", "rejected", "done-for-day"):
                order = o
                break
        qty = float(order.get("filledQuantity") or 0)
        price = float(order.get("filledAveragePrice") or fallback_price)
        eur = float(order.get("filledAmount") or fallback_eur)
        return qty, price, eur

    # ---- giro di controllo
    def tick(self):
        if STOP_FILE.exists():
            log("File STOP presente: il bot non fa nulla. Cancellalo per ripartire.")
            return
        seen = set(self.state["seen"])
        for wallet in self.cfg["wallets"]:
            addr = wallet["address"].lower()
            name = wallet.get("name", addr[:10])
            wstate = self.state["wallets"].get(addr)
            try:
                if wstate is None:
                    # primo avvio: parto da adesso, non copio lo storico
                    block = self.es.latest_block()
                    self.state["wallets"][addr] = {"last_block": block}
                    log(f"{name}: inizio a seguirlo dal blocco {block}")
                    continue
                start = wstate["last_block"] + 1
                req_init = wallet.get("require_initiated", self.cfg.get("require_initiated", True))
                trusted = self.cfg.get("trusted_counterparties", [])
                ttx = self.es.token_transfers(addr, start)
                ntx = self.es.normal_txs(addr, start) if req_init else []
                itx = self.es.internal_txs(addr, start) if trusted else []
            except Exception as e:  # rete o API: riprovo al prossimo giro
                log(f"{name}: errore lettura dati ({e})")
                continue
            signals = extract_signals(addr, ttx, ntx, self.tokens, req_init,
                                      internal_txs=itx, trusted=trusted)
            for s in signals:
                key = f"{s['hash']}:{s['asset']}:{s['side']}"
                if key in seen:
                    continue
                seen.add(key)
                log(f"{name}: {s['side']} {s['wallet_qty']:.4f} {s['asset']} (tx {s['hash'][:12]}...)")
                try:
                    self.handle(s)
                except Exception as e:
                    log(f"  errore esecuzione: {e}")
            blocks = [int(t["blockNumber"]) for t in ttx + ntx + itx]
            if blocks:
                wstate["last_block"] = max(max(blocks), wstate["last_block"])
        self.strategy_tick()
        self.state["seen"] = list(seen)[-5000:]
        self.state["last_check"] = datetime.now().isoformat(timespec="seconds")
        save_json(self.state_path, self.state)
        log(f"Giro completato. Liquidità simulata: {self.state['paper']['cash']:.2f} EUR, "
            f"posizioni aperte: {len(self.state['paper']['positions'])}")

    # ---- strategia sul prezzo
    def strategy_tick(self):
        st = self.cfg.get("strategy") or {}
        if not st.get("enabled"):
            return
        fast, slow, interval = int(st.get("fast", 20)), int(st.get("slow", 50)), st.get("interval", "1h")
        source = st.get("name", f"SMA{fast}/{slow}")
        mem = self.state.setdefault("strategy", {})
        for pair in st.get("pairs", []):
            asset = pair.split("-")[0]
            try:
                candles = self.fu.candles(pair, interval, slow + 5)
            except Exception as e:
                log(f"Strategia {pair}: errore lettura candele ({e})")
                continue
            sma_cross.last = None
            signal, bar = sma_cross(candles, fast, slow, interval, mode=st.get("mode", "cross"))
            if bar is None or mem.get(pair) == bar:
                continue  # dati insufficienti o candela già valutata
            mem[pair] = bar
            if sma_cross.last:
                f, sl, last = sma_cross.last
                trend = "sopra" if f > sl else "sotto"
                log(f"Strategia {pair}: prezzo {last:.2f}, media{fast} {f:.2f} {trend} media{slow} {sl:.2f} "
                    f"(distanza {abs(f - sl) / sl * 100:.2f}%)")
            if not signal:
                continue
            key = f"{asset}#{source}"
            holding = self.positions().get(key, {}).get("qty", 0) > 0
            if (signal == "Buy" and holding) or (signal == "Sell" and not holding):
                continue
            log(f"Strategia {source}: {signal} {asset} (medie {fast}/{slow} su candele {interval})")
            sig = {"wallet": f"strategia:{source}", "hash": f"{source}-{pair}-{bar}",
                   "asset": asset, "side": signal, "wallet_qty": 0, "pos_key": key}
            try:
                self.handle(sig)
            except Exception as e:
                log(f"  errore esecuzione: {e}")

    # ---- report
    def report(self):
        p = self.state["paper"]
        pos = p["positions"]
        prices = self.fu.prices({self.pair_of(a.split("#")[0]) for a in pos}) if pos else {}
        value = 0.0
        print("\nPORTAFOGLIO SIMULATO")
        print(f"{'posizione':<22}{'quantità':>16}{'costo EUR':>12}{'valore EUR':>12}{'risultato':>12}")
        for a, x in pos.items():
            v = x["qty"] * prices.get(self.pair_of(a.split("#")[0]), 0)
            value += v
            print(f"{a:<22}{x['qty']:>16.6f}{x['cost']:>12.2f}{v:>12.2f}{v - x['cost']:>+12.2f}")
        total = p["cash"] + value
        start = self.cfg["paper_start_cash"]
        print(f"\nLiquidità:            {p['cash']:.2f} EUR")
        print(f"Valore posizioni:     {value:.2f} EUR")
        print(f"Totale:               {total:.2f} EUR  (partenza {start:.2f})")
        print(f"Risultato complessivo: {total - start:+.2f} EUR ({(total / start - 1) * 100:+.2f}%)")
        print(f"di cui già realizzato: {p['realized_pnl']:+.2f} EUR\n")


# ---------------------------------------------------------------- avvio

def main():
    ap = argparse.ArgumentParser(description="Copy-bot Bitpanda Fusion")
    ap.add_argument("command", choices=["check", "once", "run", "report"])
    ap.add_argument("--config", default=str(BASE_DIR / "config.json"))
    ap.add_argument("--live", action="store_true", help="ordini REALI (serve anche mode=live nel config)")
    ap.add_argument("--yes", action="store_true", help="salta la conferma iniziale del trading reale")
    args = ap.parse_args()

    cfg = load_json(Path(args.config), None)
    if cfg is None:
        sys.exit("Manca config.json: copia config.example.json in config.json e modificalo.")
    # chiavi in chiavi.txt (o .env) nella cartella del bot: righe CHIAVE=valore
    for env_file in (BASE_DIR / "chiavi.txt", BASE_DIR / ".env"):
        if not env_file.exists():
            continue
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    es_key, bp_key = os.environ.get("ETHERSCAN_API_KEY"), os.environ.get("BITPANDA_API_KEY")
    if not es_key or not bp_key:
        sys.exit("Imposta le variabili d'ambiente ETHERSCAN_API_KEY e BITPANDA_API_KEY.")

    live = args.live and cfg.get("mode") == "live"
    if args.live and not live:
        sys.exit('Hai usato --live ma nel config c\'è "mode": "paper". Cambialo in "live" se sei sicuro.')
    if live and args.command in ("once", "run") and not args.yes:
        print(f"ATTENZIONE: ordini REALI su Bitpanda, {cfg['trade_eur']} EUR per operazione, "
              f"massimo {cfg['max_total_eur']} EUR in totale.")
        if input('Scrivi SI per continuare: ').strip() != "SI":
            sys.exit("Annullato.")

    bot = CopyBot(cfg, Etherscan(es_key, cfg.get("chainid", 1)), Fusion(bp_key), live=live)

    if args.command == "check":
        print(f"Coppie attive su Bitpanda Fusion: {len(bot.pairs)}")
        for contract, asset in bot.tokens.items():
            pair = bot.pair_of(asset)
            if pair in bot.pairs:
                info = bot.pairs[pair]
                print(f"  OK   {pair:<12} minimo ordine {info.get('minOrderAmount')} {bot.quote}")
            else:
                print(f"  NO   {pair:<12} non disponibile: i segnali su {asset} verranno ignorati")
        print(f"Blocco Ethereum attuale: {bot.es.latest_block()}")
        print(f"Wallet seguiti: {len(cfg['wallets'])}. Modalità: {'REALE' if live else 'SIMULAZIONE'}")
    elif args.command == "once":
        bot.tick()
    elif args.command == "run":
        log(f"Avvio in modalità {'REALE' if live else 'SIMULAZIONE'}, controllo ogni {cfg['poll_seconds']}s")
        try:
            while True:
                bot.tick()
                time.sleep(cfg["poll_seconds"])
        except KeyboardInterrupt:
            log("Fermato.")
    elif args.command == "report":
        bot.report()


if __name__ == "__main__":
    main()
