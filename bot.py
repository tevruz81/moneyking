#!/usr/bin/env python3
"""
Binance 24 saatlik hacim (USDT) Telegram bildirim botu.
GitHub Actions ile birkaç dakikada bir kısa süreli çalışır; sunucu gerektirmez.
Ayarlar ve coin listesi state.json içinde tutulur, Telegram komutlarıyla yönetilir.
"""
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

# Binance USDⓈ-M Futures (perpetual) piyasa verisi.
# fapi.binance.com ABD IP'lerini (GitHub sunucuları) engeller; aynı veri www.binance.com üzerinden de sunulur.
BINANCE_BASES = ["https://www.binance.com/fapi/v1", "https://fapi.binance.com/fapi/v1"]

INTERVALS = {"30m": 30, "1h": 60, "4h": 240, "12h": 720}
INTERVAL_ALIASES = {
    "30": "30m", "30m": "30m", "30dk": "30m",
    "1": "1h", "1h": "1h", "1s": "1h", "60": "1h",
    "4": "4h", "4h": "4h", "4s": "4h",
    "12": "12h", "12h": "12h", "12s": "12h",
}
INTERVAL_LABELS = {"30m": "30 dakika", "1h": "1 saat", "4h": "4 saat", "12h": "12 saat"}
MAX_COINS = 40

DEFAULT_STATE = {
    "coins": ["BTC", "ETH"],
    "interval": "30m",
    "window": [0, 24],          # gönderim saat aralığı [başlangıç, bitiş); 0-24 = her zaman
    "tz": "Europe/Istanbul",
    "offset": 0,                # Telegram getUpdates konumu
    "last_slot": None,          # en son gönderilen zaman dilimi
    "last_volumes": {},         # Δ hesaplamak için önceki hacimler
    "commands_set": False,
}

COMMANDS = [
    ("liste", "Takip edilen coinler"),
    ("ekle", "Coin ekle (sonra coin adını yaz)"),
    ("sil", "Coin sil (butondan seç)"),
    ("aralik", "Gönderim sıklığı (30dk / 1s / 4s / 12s)"),
    ("saat", "Gönderim saatleri"),
    ("tz", "Saat dilimi"),
    ("durum", "Ayarlar ve sonraki rapor"),
    ("simdi", "Raporu hemen gönder"),
    ("yardim", "Komut listesi"),
]

HELP_TEXT = """🤖 <b>Binance Hacim Botu</b>

<b>Coin yönetimi</b>
/ekle — coin ekle (bot sorar, sen yazarsın; ya da <code>/ekle BTC ETH</code>)
/sil — coin sil (butondan seç)
/liste — takip edilen coinler

<b>Gönderim ayarları</b>
/aralik — sıklık: 30 dk, 1, 4, 12 saat (butonla)
/saat — gönderim saatleri, ör. 09-23 (butonla ya da yazarak)
/tz — saat dilimi

<b>Diğer</b>
/durum — ayarlar ve sonraki rapor
/simdi — raporu hemen gönder
iptal — bekleyen soruyu iptal et

ℹ️ Bot 5 dakikada bir uyanır: ilk komutun yanıtı birkaç dakika sürebilir,
sonrakiler yazışma devam ettikçe anında gelir."""


# ---------------------------------------------------------------- HTTP

def http_json(url, params=None, data=None, timeout=20):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={"User-Agent": "hacim-bot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def tg(method, _timeout=30, **params):
    return http_json(f"https://api.telegram.org/bot{TOKEN}/{method}", data=params, timeout=_timeout)


def send(text, chat_id=None, markup=None):
    params = dict(chat_id=chat_id or CHAT_ID, text=text,
                  parse_mode="HTML", disable_web_page_preview="true")
    if markup:
        params["reply_markup"] = json.dumps(markup, ensure_ascii=False)
    tg("sendMessage", **params)


def buttons(items, per_row=3):
    """items: [(etiket, callback_data)] -> inline klavye"""
    rows = [items[i:i + per_row] for i in range(0, len(items), per_row)]
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


def ask(placeholder):
    """Kullanıcıdan yanıt bekleyen klavye (Telegram yanıt kutusunu açar)."""
    return {"force_reply": True, "input_field_placeholder": placeholder}


def binance(path, **params):
    last = None
    for base in BINANCE_BASES:
        try:
            return http_json(f"{base}/{path}", params=params or None)
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in (403, 451) and e.code < 500:
                raise
    raise last


# ---------------------------------------------------------------- State

def load_state():
    state = json.loads(json.dumps(DEFAULT_STATE))
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            state.update(json.load(f))
    return state


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


# ---------------------------------------------------------------- Yardımcılar

def norm_coin(raw):
    c = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
    if c.endswith("USDT") and len(c) > 4:
        c = c[:-4]
    return c


_EXCHANGE_INFO = None


def futures_symbols():
    """USDT-M perpetual kontratlar: {sembol: durum}"""
    global _EXCHANGE_INFO
    if _EXCHANGE_INFO is None:
        info = binance("exchangeInfo")
        _EXCHANGE_INFO = {s["symbol"]: s["status"] for s in info["symbols"]
                          if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT"}
    return _EXCHANGE_INFO


def symbol_status(symbol):
    return futures_symbols().get(symbol)


def fetch_tickers(symbols):
    """Futures 24s ticker verisi (tek istekte tüm kontratlar, sonra filtre)."""
    wanted = set(symbols)
    return {d["symbol"]: d for d in binance("ticker/24hr") if d["symbol"] in wanted}


def fmt_vol(v):
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= div:
            return f"{v / div:.2f}{suf}"
    return f"{v:.0f}"


def in_window(hour, window):
    s, e = window
    if s < e:
        return s <= hour < e
    return hour >= s or hour < e          # gece yarısını aşan aralık, ör. 22-02


def slot_start(now, minutes):
    total = now.hour * 60 + now.minute
    start = total - total % minutes
    return now.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)


def slot_id(now, minutes):
    return f"{minutes}@{slot_start(now, minutes):%Y-%m-%dT%H:%M}"


def next_send(state, now):
    minutes = INTERVALS[state["interval"]]
    t = slot_start(now, minutes)
    if slot_id(now, minutes) != state.get("last_slot") and in_window(now.hour, state["window"]):
        return None  # bu dilim henüz gönderilmedi; bir sonraki çalışmada gider
    for _ in range(500):
        t = t + timedelta(minutes=minutes)
        if in_window(t.hour, state["window"]):
            return t
    return None


def window_text(window):
    s, e = window
    return "her zaman" if (s, e) == (0, 24) else f"{s:02d}:00 – {e % 24:02d}:00"


# ---------------------------------------------------------------- Rapor

def build_report(state, now):
    coins = state["coins"]
    data = fetch_tickers([c + "USDT" for c in coins])
    rows, new_vols = [], {}
    for c in coins:
        d = data.get(c + "USDT")
        if not d:
            rows.append((c, None, None))
            continue
        vol = float(d["quoteVolume"])
        prev = state["last_volumes"].get(c)
        delta = (vol / prev - 1) * 100 if prev else None
        rows.append((c, vol, delta))
        new_vols[c] = vol
    if not new_vols:
        raise RuntimeError("Binance'ten veri alınamadı")

    rows.sort(key=lambda r: r[1] if r[1] is not None else -1, reverse=True)
    lines = [f"{'COIN':<8}{'HACIM':>10}{'Δ':>9}", "-" * 27]
    for c, v, dl in rows:
        vs = fmt_vol(v) if v is not None else "veri yok"
        ds = f"{dl:+.1f}%" if dl is not None else "-"
        lines.append(f"{c:<8}{vs:>10}{ds:>9}")
    total = sum(new_vols.values())
    lines += ["-" * 27, f"{'TOPLAM':<8}{fmt_vol(total):>10}"]

    text = (f"📊 <b>Binance Futures 24s Hacim (USDT)</b>\n"
            f"🕒 {now:%d.%m.%Y %H:%M} ({html.escape(state['tz'])})\n"
            f"<pre>{html.escape(chr(10).join(lines))}</pre>\n"
            f"<i>Δ: bir önceki bildirime göre değişim</i>")
    return text, new_vols


def send_report(state, now):
    text, new_vols = build_report(state, now)
    send(text)
    state["last_volumes"] = new_vols


# ---------------------------------------------------------------- Komutlar
# Komut fonksiyonları ya metin ya da (metin, klavye) döndürür.

def cmd_ekle(state, args):
    if not args:
        state["pending"] = "ekle"
        return ("➕ Eklemek istediğin coin(ler)i yaz.\nÖrnek: <code>BTC ETH SOL</code>",
                ask("BTC ETH SOL"))
    added, exists, bad = [], [], []
    for a in args:
        c = norm_coin(a)
        if not c:
            continue
        if c in state["coins"]:
            exists.append(c)
            continue
        if len(state["coins"]) >= MAX_COINS:
            bad.append(f"{c} (liste dolu, en fazla {MAX_COINS})")
            continue
        st = symbol_status(c + "USDT")
        if st is None:  # PEPE, SHIB gibi coinler futures'ta 1000PEPE vb. olarak işlem görür
            for pre in ("1000", "1000000", "1M"):
                if symbol_status(pre + c + "USDT"):
                    c = pre + c
                    st = symbol_status(c + "USDT")
                    break
            if c in state["coins"]:
                exists.append(c)
                continue
        if st == "TRADING":
            state["coins"].append(c)
            added.append(c)
        elif st:
            bad.append(f"{c} (işlem durumu: {st})")
        else:
            bad.append(f"{c} (Binance Futures'ta {c}USDT perpetual yok)")
    out = []
    if added:
        out.append("✅ Eklendi: " + ", ".join(added))
    if exists:
        out.append("ℹ️ Zaten listede: " + ", ".join(exists))
    if bad:
        out.append("❌ Eklenmedi: " + "; ".join(bad))
    out.append("\n" + cmd_liste(state, []))
    return "\n".join(out)


def cmd_sil(state, args):
    if not args:
        if not state["coins"]:
            return "Liste zaten boş. Eklemek için /ekle"
        state["pending"] = "sil"
        items = [(f"❌ {c}", f"sil:{c}") for c in state["coins"]] + [("🗑 Hepsini sil", "sil:hepsi")]
        return ("Silmek istediğin coine dokun (ya da adını yaz):", buttons(items))
    if args[0].lower() in ("hepsi", "tumu", "tümü", "all"):
        state["coins"], state["last_volumes"] = [], {}
        return "🗑 Liste temizlendi."
    removed, missing = [], []
    for a in args:
        c = norm_coin(a)
        if c in state["coins"]:
            state["coins"].remove(c)
            state["last_volumes"].pop(c, None)
            removed.append(c)
        elif c:
            missing.append(c)
    out = []
    if removed:
        out.append("🗑 Silindi: " + ", ".join(removed))
    if missing:
        out.append("ℹ️ Listede yok: " + ", ".join(missing))
    out.append("\n" + cmd_liste(state, []))
    return "\n".join(out)


def cmd_liste(state, _):
    if not state["coins"]:
        return "📋 Liste boş. Eklemek için /ekle"
    return f"📋 Takip edilen coinler ({len(state['coins'])}):\n" + ", ".join(state["coins"])


def cmd_aralik(state, args):
    key = INTERVAL_ALIASES.get(args[0].lower()) if args else None
    if not key:
        items = [(("✅ " if k == state["interval"] else "") + INTERVAL_LABELS[k], f"aralik:{k}")
                 for k in INTERVALS]
        return (f"⏱ Gönderim sıklığını seç (şu an: {INTERVAL_LABELS[state['interval']]}):",
                buttons(items, per_row=2))
    state["interval"] = key
    return f"✅ Gönderim sıklığı: {INTERVAL_LABELS[key]}"


SAAT_PRESETS = [("Her zaman", "0-24"), ("08–24", "8-24"), ("09–23", "9-23"), ("09–18", "9-18")]


def cmd_saat(state, args):
    usage = ("Örnek: <code>09-23</code> (09:00–23:00), <code>22-02</code> (gece yarısını aşar), "
             "<code>0-24</code> (her zaman)")
    if not args:
        state["pending"] = "saat"
        items = [(t, f"saat:{v}") for t, v in SAAT_PRESETS]
        return (f"🕒 Gönderim saatlerini seç ya da yaz (şu an: {window_text(state['window'])}).\n{usage}",
                buttons(items, per_row=2))
    arg = "".join(args)
    if arg.lower() in ("hepsi", "her", "herzaman"):
        arg = "0-24"
    m = re.fullmatch(r"(\d{1,2})(?::00)?[-–](\d{1,2})(?::00)?", arg)
    if not m:
        return "❌ Anlaşılamadı. " + usage
    s, e = int(m.group(1)), int(m.group(2))
    if not (0 <= s <= 23 and 0 <= e <= 24) or s == e % 24 and (s, e) != (0, 24):
        return "❌ Geçersiz aralık. " + usage
    if e == 0:
        e = 24
    state["window"] = [s, e]
    return f"✅ Gönderim saatleri: {window_text(state['window'])} ({state['tz']})"


TZ_PRESETS = ["Europe/Istanbul", "UTC", "Europe/London", "Europe/Berlin", "Asia/Dubai", "America/New_York"]


def cmd_tz(state, args):
    if not args:
        state["pending"] = "tz"
        items = [(("✅ " if z == state["tz"] else "") + z, f"tz:{z}") for z in TZ_PRESETS]
        return (f"🌍 Saat dilimini seç ya da yaz (şu an: {state['tz']}):", buttons(items, per_row=2))
    try:
        ZoneInfo(args[0])
    except (ZoneInfoNotFoundError, ValueError):
        return f"❌ Tanınmayan saat dilimi: {html.escape(args[0])}\nÖrnek: Europe/Istanbul"
    state["tz"] = args[0]
    return f"✅ Saat dilimi: {args[0]}"


def cmd_durum(state, _):
    now = datetime.now(ZoneInfo(state["tz"]))
    nxt = next_send(state, now)
    nxt_txt = "birkaç dakika içinde" if nxt is None else f"{nxt:%d.%m %H:%M} civarı"
    return (f"⚙️ <b>Durum</b>\n"
            f"Coinler: {', '.join(state['coins']) or '-'}\n"
            f"Sıklık: {INTERVAL_LABELS[state['interval']]}\n"
            f"Gönderim saatleri: {window_text(state['window'])}\n"
            f"Saat dilimi: {state['tz']}\n"
            f"Şu an: {now:%d.%m.%Y %H:%M}\n"
            f"Sonraki rapor: {nxt_txt}")


def cmd_simdi(state, _):
    if not state["coins"]:
        return "Liste boş. Önce /ekle ile coin ekle."
    now = datetime.now(ZoneInfo(state["tz"]))
    send_report(state, now)
    state["last_slot"] = slot_id(now, INTERVALS[state["interval"]])  # aynı dilimde tekrar gönderme
    return None


COMMAND_MAP = {
    "ekle": cmd_ekle, "sil": cmd_sil, "liste": cmd_liste, "aralik": cmd_aralik,
    "saat": cmd_saat, "tz": cmd_tz, "durum": cmd_durum, "simdi": cmd_simdi,
}
COMMAND_ALIASES = {
    "listele": "liste", "list": "liste", "aralık": "aralik",
    "saatler": "saat", "zaman": "tz", "kaldir": "sil", "kaldır": "sil",
    "şimdi": "simdi", "yardım": "yardim", "help": "yardim", "start": "yardim",
}


def handle(state, text):
    parts = text.split(maxsplit=1)
    cmd = parts[0][1:].split("@")[0].lower()
    cmd = COMMAND_ALIASES.get(cmd, cmd)
    args = [a for a in re.split(r"[\s,;]+", parts[1] if len(parts) > 1 else "") if a]
    state.pop("pending", None)
    if cmd == "yardim":
        return HELP_TEXT
    fn = COMMAND_MAP.get(cmd)
    return fn(state, args) if fn else "Bilinmeyen komut. /yardim yazabilirsin."


def reply(result, chat):
    if not result:
        return
    text, markup = result if isinstance(result, tuple) else (result, None)
    try:
        send(text, chat, markup)
    except Exception as e:
        print(f"Yanıt gönderilemedi: {e}", file=sys.stderr)


def process_updates(state, wait=0):
    """Gelen mesaj ve buton tıklamalarını işler; işlenen güncelleme sayısını döndürür."""
    try:
        res = tg("getUpdates", _timeout=wait + 15, offset=state["offset"], timeout=wait,
                 allowed_updates=json.dumps(["message", "callback_query"]))
    except Exception as e:
        print(f"getUpdates hatası: {e}", file=sys.stderr)
        return 0
    updates = res.get("result", [])
    for u in updates:
        state["offset"] = u["update_id"] + 1
        cq = u.get("callback_query")
        if cq:
            msg = cq.get("message") or {}
            text = "/" + (cq.get("data") or "").replace(":", " ", 1)
            try:
                tg("answerCallbackQuery", callback_query_id=cq["id"])
            except Exception:
                pass  # eski butonlarda süre dolmuş olabilir
        else:
            msg = u.get("message") or {}
            text = (msg.get("text") or "").strip()
        chat = str(msg.get("chat", {}).get("id", ""))
        if not text or not chat:
            continue
        try:
            if not CHAT_ID:
                # Kurulum modu: chat ID'yi kullanıcıya bildir.
                send(f"👋 Kurulum modu.\nChat ID: <code>{chat}</code>\n"
                     "Bunu GitHub'da TELEGRAM_CHAT_ID secret'ı olarak ekle.", chat)
                continue
            if chat != CHAT_ID:
                send("⛔ Bu bot özel kullanım içindir.", chat)
                continue
            if not text.startswith("/"):
                pending = state.get("pending")
                if text.lower() in ("iptal", "vazgeç", "vazgec"):
                    state.pop("pending", None)
                    reply("İptal edildi.", chat)
                    continue
                if not pending:
                    reply("Komut listesi için /yardim yaz.", chat)
                    continue
                text = f"/{pending} {text}"
            result = handle(state, text)
        except Exception as e:
            result = f"⚠️ Hata: {html.escape(str(e))}"
        reply(result, chat)
    return len(updates)


def maybe_report(state):
    if not (CHAT_ID and state["coins"]):
        return
    now = datetime.now(ZoneInfo(state["tz"]))
    sid = slot_id(now, INTERVALS[state["interval"]])
    if in_window(now.hour, state["window"]) and sid != state.get("last_slot"):
        try:
            send_report(state, now)
            state["last_slot"] = sid
        except Exception as e:
            print(f"Rapor gönderilemedi: {e}", file=sys.stderr)
            if state.get("last_error_slot") != sid:
                state["last_error_slot"] = sid
                reply(f"⚠️ Rapor alınamadı: {html.escape(str(e))[:300]}", CHAT_ID)


# ---------------------------------------------------------------- Ana akış

SESSION_MAX = 240   # biri yazışıyorsa bot en fazla bu kadar saniye açık kalır
SESSION_IDLE = 90   # son mesajdan bu kadar saniye sonra kapanır
COMMANDS_VERSION = 2


def main():
    if not TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN secret'ı tanımlı değil.")
    state = load_state()

    if state.get("commands_set") is not COMMANDS_VERSION:
        try:
            tg("setMyCommands", commands=json.dumps(
                [{"command": c, "description": d} for c, d in COMMANDS], ensure_ascii=False))
            state["commands_set"] = COMMANDS_VERSION
        except Exception as e:
            print(f"Komut menüsü ayarlanamadı: {e}", file=sys.stderr)

    # İlk kontrol beklemesizdir. Mesaj geldiyse kullanıcı aktif demektir:
    # bot kısa bir süre açık kalıp sonraki komutlara anında yanıt verir.
    start = time.time()
    last_activity = start if process_updates(state) else None
    maybe_report(state)
    while last_activity is not None:
        remaining = SESSION_MAX - (time.time() - start)
        if remaining < 5 or time.time() - last_activity > SESSION_IDLE:
            break
        if process_updates(state, wait=int(min(25, remaining))):
            last_activity = time.time()
        maybe_report(state)

    save_state(state)


if __name__ == "__main__":
    main()
