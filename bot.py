#!/usr/bin/env python3
"""
Binance Futures hacim botu (Telegram).

Seçilen USDT-M perpetual kontratlarının son fiyatını ve 24 saatlik hacmini (USDT)
ayarlanabilir aralıklarla Telegram'a raporlar. Coin listesi ve ayarlar Telegram
komutlarıyla yönetilir.

Çalışma ortamı GitHub Actions'tır. Ayarlar state.json dosyasında tutulur ve her
çalışmanın sonunda repoya kaydedilir.

Ortam değişkenleri:
    TELEGRAM_BOT_TOKEN  (zorunlu) BotFather token'ı
    TELEGRAM_CHAT_ID    botu kullanabilecek tek sohbet; boşsa kurulum modu
    RUN_SECONDS         >0 ise bot bu süre boyunca açık kalır (sürekli mod)
"""

from __future__ import annotations

import contextlib
import copy
import html
import json
import logging
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

log = logging.getLogger("hacim-bot")

# =============================================================== Yapılandırma

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
RUN_SECONDS = int(os.environ.get("RUN_SECONDS", "0") or 0)
STATE_FILE = Path(__file__).resolve().with_name("state.json")

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
# fapi.binance.com ABD IP'lerini (GitHub sunucuları) 451 ile engeller;
# aynı Futures verisi www.binance.com üzerinden de sunulur.
BINANCE_BASES = ("https://www.binance.com/fapi/v1", "https://fapi.binance.com/fapi/v1")

HTTP_TIMEOUT = 20  # sn
HTTP_RETRIES = 3  # geçici ağ hatalarında deneme sayısı
LONG_POLL = 25  # Telegram getUpdates bekleme süresi (sn)
ERROR_PAUSE = 3  # Telegram'a ulaşılamazsa döngünün bekleyeceği süre (sn)
SESSION_MAX = 240  # kısa mod: yazışma sürerken en fazla açık kalma süresi
SESSION_IDLE = 90  # kısa mod: son mesajdan sonra kapanma süresi
MAX_COINS = 40
HISTORY_LEN = 3  # raporda gösterilen son hacim sayısı
MAX_MESSAGE = 3800  # Telegram sınırı 4096 karakter; uzun raporlar bölünür
COMMANDS_VERSION = 3  # komut menüsü değiştiğinde artırılır

INTERVALS = {"30m": 30, "1h": 60, "4h": 240, "12h": 720}
INTERVAL_LABELS = {"30m": "30 dakika", "1h": "1 saat", "4h": "4 saat", "12h": "12 saat"}
# fmt: off
INTERVAL_ALIASES = {
    "30": "30m", "30m": "30m", "30dk": "30m",
    "1": "1h", "1h": "1h", "1s": "1h", "60": "1h",
    "4": "4h", "4h": "4h", "4s": "4h",
    "12": "12h", "12h": "12h", "12s": "12h",
}
# fmt: on
SAAT_PRESETS = (("Her zaman", "0-24"), ("08–24", "8-24"), ("09–23", "9-23"), ("09–18", "9-18"))
TZ_PRESETS = ("Europe/Istanbul", "UTC", "Europe/London", "Europe/Berlin", "Asia/Dubai", "America/New_York")
CANCEL_WORDS = {"iptal", "vazgeç", "vazgec"}
SYMBOL_PREFIXES = ("1000", "1000000", "1M")  # PEPE → 1000PEPE gibi futures adları

DEFAULT_STATE: dict[str, Any] = {
    "coins": ["BTC", "ETH"],
    "interval": "30m",
    "window": [0, 24],  # gönderim saat aralığı [başlangıç, bitiş); 0-24 = her zaman
    "tz": "Europe/Istanbul",
    "offset": 0,  # Telegram getUpdates konumu
    "last_slot": None,  # en son raporlanan zaman dilimi
    "history": {},  # coin -> [[ISO zaman, hacim], ...] (yeniden eskiye)
    "commands_set": 0,  # Telegram'a kaydedilen komut menüsü sürümü
}

MENU = (
    ("liste", "Takip edilen coinler"),
    ("ekle", "Coin ekle (sonra coin adını yaz)"),
    ("sil", "Coin sil (butondan seç)"),
    ("aralik", "Gönderim sıklığı (30dk / 1s / 4s / 12s)"),
    ("saat", "Gönderim saatleri"),
    ("tz", "Saat dilimi"),
    ("durum", "Ayarlar ve sonraki rapor"),
    ("simdi", "Raporu hemen gönder"),
    ("yardim", "Komut listesi"),
)

HELP_TEXT = """🤖 <b>Binance Futures Hacim Botu</b>

<b>Coin yönetimi</b>
/ekle — coin ekle (bot sorar, sen yazarsın; ya da <code>/ekle BTC ETH</code>)
/sil — coin sil (butondan seç)
/liste — takip edilen coinler

<b>Gönderim ayarları</b>
/aralik — sıklık: 30 dk, 1, 4, 12 saat
/saat — gönderim saatleri, ör. 09-23
/tz — saat dilimi

<b>Diğer</b>
/durum — ayarlar ve sonraki rapor
/simdi — raporu hemen gönder
iptal — bekleyen soruyu iptal et

ℹ️ Menüde bir komuta uzun basarsan komut gönderilmeden yazı kutusuna gelir."""

Markup = dict[str, Any]
Reply = str | tuple[str, Markup] | None

# ======================================================================= HTTP


def http_json(
    url: str,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    timeout: float = HTTP_TIMEOUT,
    retries: int = HTTP_RETRIES,
) -> Any:
    """JSON döndüren GET/POST isteği; geçici hatalarda (ağ, 429, 5xx) yeniden dener.

    Not: Telegram URL'si token içerdiği için URL hiçbir zaman loglanmaz.
    """
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    request = urllib.request.Request(url, data=body, headers={"User-Agent": "hacim-bot/2.0"})
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            if attempt == retries or not (exc.code == 429 or exc.code >= 500):
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries:
                raise
        time.sleep(attempt)  # 1 sn, 2 sn ... bekleyip yeniden dene
    raise ValueError("retries en az 1 olmalı")


# =================================================================== Telegram


def tg(method: str, *, http_timeout: float = 30, **params: Any) -> Any:
    return http_json(TELEGRAM_API.format(token=TOKEN, method=method), data=params, timeout=http_timeout)


def send(text: str, chat_id: str | None = None, markup: Markup | None = None) -> None:
    params: dict[str, Any] = {
        "chat_id": chat_id or CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    if markup:
        params["reply_markup"] = json.dumps(markup, ensure_ascii=False)
    tg("sendMessage", **params)


def reply(result: Reply, chat_id: str) -> None:
    """Komut sonucunu gönderir; gönderim hatası botu durdurmaz."""
    if not result:
        return
    text, markup = result if isinstance(result, tuple) else (result, None)
    try:
        send(text, chat_id, markup)
    except Exception:
        log.exception("Yanıt gönderilemedi")


def buttons(items: list[tuple[str, str]], per_row: int = 3) -> Markup:
    """[(etiket, callback_data), ...] -> satır satır inline klavye."""
    rows = [items[i : i + per_row] for i in range(0, len(items), per_row)]
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


def ask(placeholder: str) -> Markup:
    """Kullanıcıdan yazılı yanıt bekleyen klavye (Telegram yanıt kutusunu açar)."""
    return {"force_reply": True, "input_field_placeholder": placeholder}


# ==================================================================== Binance


def binance(path: str, **params: Any) -> Any:
    """Futures API isteği; bölgesel engelde (403/451) ve sunucu hatasında sıradaki adresi dener."""
    *fallbacks, last = BINANCE_BASES
    for base in fallbacks:
        try:
            return http_json(f"{base}/{path}", params=params or None)
        except urllib.error.HTTPError as exc:
            if exc.code not in (403, 451) and exc.code < 500:
                raise
            log.warning("%s yanıt vermedi (HTTP %s), sıradaki adres deneniyor", base, exc.code)
    return http_json(f"{last}/{path}", params=params or None)


class FuturesMarket:
    """Binance USDT-M perpetual piyasa verisi. Kontrat listesi çalışma boyunca önbelleklenir."""

    def __init__(self, fetch: Callable[..., Any] = binance) -> None:
        self._fetch = fetch
        self._symbols: dict[str, str] | None = None

    def symbols(self) -> dict[str, str]:
        """{sembol: durum} — yalnızca USDT kotasyonlu perpetual kontratlar."""
        if self._symbols is None:
            info = self._fetch("exchangeInfo")
            self._symbols = {
                s["symbol"]: s["status"]
                for s in info["symbols"]
                if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT"
            }
        return self._symbols

    def resolve(self, coin: str) -> tuple[str, str | None]:
        """Kullanıcının yazdığı coini futures adına çevirir: ('PEPE') -> ('1000PEPE', 'TRADING')."""
        symbols = self.symbols()
        for name in (coin, *(prefix + coin for prefix in SYMBOL_PREFIXES)):
            status = symbols.get(name + "USDT")
            if status:
                return name, status
        return coin, None

    def tickers(self, coins: list[str]) -> dict[str, dict[str, Any]]:
        """{coin: ticker} — tek istekte tüm kontratlar çekilir, istenenler süzülür."""
        wanted = {c + "USDT": c for c in coins}
        return {wanted[t["symbol"]]: t for t in self._fetch("ticker/24hr") if t["symbol"] in wanted}


market = FuturesMarket()

# ===================================================================== Durum


def load_state(path: Path = STATE_FILE) -> dict[str, Any]:
    state = copy.deepcopy(DEFAULT_STATE)
    if path.exists():
        try:
            state.update(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            log.exception("state.json okunamadı, varsayılan ayarlarla devam ediliyor")
    _migrate(state)
    return state


def _migrate(state: dict[str, Any]) -> None:
    """Eski sürümlerin state.json biçimini günceller."""
    old = state.pop("last_volumes", None)
    if old:
        for coin, volume in old.items():
            state["history"].setdefault(coin, [["önceki", volume]])
    if not isinstance(state.get("commands_set"), int) or isinstance(state["commands_set"], bool):
        state["commands_set"] = 0


def save_state(state: dict[str, Any], path: Path = STATE_FILE) -> None:
    """Önce geçici dosyaya yazar, sonra yer değiştirir; yarım kalmış dosya oluşmaz."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


# =================================================================== Biçimler


def tr_number(value: float, decimals: int = 0) -> str:
    """1234567.891 -> '1.234.567,89' (Türkçe sayı biçimi)."""
    return f"{value:,.{decimals}f}".translate(str.maketrans(",.", ".,"))


def fmt_usdt(value: float) -> str:
    return tr_number(value, 0)


def fmt_short(value: float) -> str:
    """15234567890 -> '15,23 Mr'."""
    for div, suffix in ((1e9, "Mr"), (1e6, "Mn"), (1e3, "B")):
        if value >= div:
            return f"{tr_number(value / div, 2)} {suffix}"
    return tr_number(value, 0)


def fmt_pct(pct: float) -> str:
    return f"{pct:+.2f}".replace(".", ",") + "%"


def fmt_price(price: float) -> str:
    """Fiyatı büyüklüğüne göre okunur hassasiyette yazar: 81.162,8 · 1,2345 · 0,01235."""
    if price >= 1000:
        decimals = 2
    elif price >= 1:
        decimals = 4
    elif price > 0:
        decimals = min(10, 3 - math.floor(math.log10(price)))  # ~4 anlamlı basamak
    else:
        decimals = 2
    whole, _, frac = tr_number(price, decimals).partition(",")
    frac = frac.rstrip("0").ljust(2, "0")
    return f"{whole},{frac}"


def window_text(window: list[int]) -> str:
    start, end = window
    return "her zaman" if (start, end) == (0, 24) else f"{start:02d}:00 – {end % 24:02d}:00"


# ============================================================ Zaman dilimleri


def in_window(hour: int, window: list[int]) -> bool:
    start, end = window
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # gece yarısını aşan aralık, ör. 22-02


def slot_start(now: datetime, minutes: int) -> datetime:
    total = now.hour * 60 + now.minute
    start = total - total % minutes
    return now.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)


def slot_id(now: datetime, minutes: int) -> str:
    return f"{minutes}@{slot_start(now, minutes):%Y-%m-%dT%H:%M}"


def local_now(state: dict[str, Any]) -> datetime:
    return datetime.now(ZoneInfo(state["tz"]))


def current_slot(state: dict[str, Any], now: datetime) -> str:
    return slot_id(now, INTERVALS[state["interval"]])


def report_due(state: dict[str, Any], now: datetime) -> bool:
    """Bu dilim gönderim saatleri içinde başlıyor ve raporu henüz gönderilmedi mi?

    Raporlar dilim başlangıcına hizalıdır (ör. 4 saatte bir: 00, 04, 08...). Başlangıcı gönderim
    saatleri dışında kalan dilim atlanır; böylece raporlar /durum'da gösterilen saatlerde gider.
    """
    minutes = INTERVALS[state["interval"]]
    starts_in_window = in_window(slot_start(now, minutes).hour, state["window"])
    return starts_in_window and slot_id(now, minutes) != state.get("last_slot")


def following_report(state: dict[str, Any], now: datetime) -> datetime | None:
    """Şu anki dilimden sonraki ilk rapor zamanı."""
    minutes = INTERVALS[state["interval"]]
    t = slot_start(now, minutes)
    for _ in range(2 * 24 * 60 // minutes + 1):
        t += timedelta(minutes=minutes)
        if in_window(t.hour, state["window"]):
            return t
    return None


def schedule_warning(state: dict[str, Any]) -> str:
    """Seçilen sıklık ve saatlerle hiç rapor zamanı oluşmuyorsa kullanıcıya uyarı döndürür."""
    minutes = INTERVALS[state["interval"]]
    if any(in_window(m // 60, state["window"]) for m in range(0, 24 * 60, minutes)):
        return ""
    return "\n⚠️ Bu saatlerde hiç rapor zamanı yok; saat aralığını genişlet ya da sıklığı azalt."


def change_schedule(state: dict[str, Any], key: str, value: str) -> None:
    """Sıklığı veya saat dilimini değiştirir.

    Mevcut dilimin raporu zaten gönderildiyse yeni dilim de gönderilmiş sayılır; aksi halde
    ayar değişikliği hemen fazladan bir rapor gönderilmesine yol açardı.
    """
    already_sent = state.get("last_slot") == current_slot(state, local_now(state))
    state[key] = value
    if already_sent:
        state["last_slot"] = current_slot(state, local_now(state))


def history_label(stamp: str, now: datetime) -> str:
    """Kayıt zamanını kısa etikete çevirir: bugünse '19:30', değilse '17.10 19:30'."""
    try:
        t = datetime.fromisoformat(stamp)
    except ValueError:
        return stamp  # eski sürüm kayıtları ("18:30", "önceki")
    if t.tzinfo and now.tzinfo:
        t = t.astimezone(now.tzinfo)
    return f"{t:%H:%M}" if t.date() == now.date() else f"{t:%d.%m %H:%M}"


# ===================================================================== Rapor


def build_report(
    state: dict[str, Any], now: datetime, tickers: dict[str, dict[str, Any]]
) -> tuple[list[str], dict[str, list[Any]]]:
    """Rapor mesajlarını ve güncellenmiş hacim geçmişini üretir (yan etkisiz)."""
    stamp = now.isoformat(timespec="minutes")
    rows, new_history = [], {}
    for coin in state["coins"]:
        old = state["history"].get(coin, [])
        ticker = tickers.get(coin)
        if not ticker:
            rows.append({"coin": coin, "volume": None})
            continue
        volume = float(ticker["quoteVolume"])
        hist = [[stamp, volume], *old][:HISTORY_LEN]
        change = (volume / hist[1][1] - 1) * 100 if len(hist) > 1 and hist[1][1] else None
        new_history[coin] = hist
        rows.append(
            {"coin": coin, "volume": volume, "price": float(ticker["lastPrice"]), "change": change, "history": hist}
        )
    if not new_history:
        raise RuntimeError("Binance'ten veri alınamadı")
    rows.sort(key=lambda r: r["volume"] if r["volume"] is not None else -1, reverse=True)

    blocks = [_coin_block(row, now) for row in rows]
    title = _report_title(state, now, rows)
    footer = _report_footer(state, now)

    messages: list[list[str]] = [[]]
    for block in blocks:
        if messages[-1] and len("\n\n".join(messages[-1] + [block])) > MAX_MESSAGE:
            messages.append([])
        messages[-1].append(block)
    texts = []
    for i, chunk in enumerate(messages):
        parts = ([title] if i == 0 else []) + ["\n\n".join(chunk)]
        if i == len(messages) - 1:
            parts.append(footer)
        texts.append("\n\n".join(parts))
    return texts, new_history


def _coin_block(row: dict[str, Any], now: datetime) -> str:
    coin = row["coin"]
    if row["volume"] is None:
        return f"⚠️ <b>{coin}</b> — veri alınamadı"
    change = row["change"]
    if change is None:
        icon, trend = "⚪", "ilk kayıt"
    else:
        icon, arrow = ("🟢", "▲") if change > 0 else ("🔴", "▼") if change < 0 else ("⚪", "•")
        trend = f"{arrow} <b>{fmt_pct(change)}</b>"
    labels = [history_label(t, now) for t, _ in row["history"]]
    width = max(len(label) for label in labels)
    lines = [
        f"{icon} <b>{coin}</b>  ·  {fmt_price(row['price'])} $",
        f"Hacim {trend}  ·  {fmt_short(row['volume'])} $",
        *(
            f"<code>{html.escape(label):>{width}}  {fmt_usdt(v):>16}</code>"
            for label, (_, v) in zip(labels, row["history"], strict=True)
        ),
    ]
    return "\n".join(lines)


def _report_title(state: dict[str, Any], now: datetime, rows: list[dict[str, Any]]) -> str:
    total = sum(r["volume"] for r in rows if r["volume"] is not None)
    lines = [
        "📊 <b>Binance Futures · 24 Saatlik Hacim</b>",
        f"🗓 {now:%d.%m.%Y}  🕒 {now:%H:%M}  ({html.escape(state['tz'])})",
        f"💰 Toplam hacim: <b>{fmt_short(total)} $</b>",
    ]
    movers = [r for r in rows if r.get("change") is not None]
    if movers:
        up = max(movers, key=lambda r: r["change"])
        down = min(movers, key=lambda r: r["change"])
        lines.append(f"📈 En çok artan: <b>{up['coin']}</b> {fmt_pct(up['change'])}")
        if down is not up:
            lines.append(f"📉 En çok düşen: <b>{down['coin']}</b> {fmt_pct(down['change'])}")
    lines.append("━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def _report_footer(state: dict[str, Any], now: datetime) -> str:
    footer = (
        "━━━━━━━━━━━━━━━━━━\n"
        "<i>Coin adının yanında son fiyat (USDT) yer alır.\n"
        "Hacimler USDT cinsinden 24 saatliktir.\n"
        "% → bir önceki rapora göre hacim değişimi.</i>"
    )
    nxt = following_report(state, now)
    if nxt:
        footer += f"\n⏭ Sonraki rapor: {nxt:%H:%M}"
    return footer


def send_report(state: dict[str, Any], now: datetime) -> None:
    texts, new_history = build_report(state, now, market.tickers(state["coins"]))
    send(texts[0])
    # İlk mesaj gittiyse rapor gönderilmiş sayılır. Kalan parçalarda hata olsa bile
    # rapor baştan tekrar gönderilmez (aynı mesajın defalarca gelmesini önler).
    state["history"].update(new_history)
    state["last_slot"] = current_slot(state, now)
    for text in texts[1:]:
        try:
            send(text)
        except Exception:
            log.exception("Raporun devamı gönderilemedi")


def maybe_report(state: dict[str, Any]) -> None:
    """Zamanı gelen raporu gönderir; hata olursa dilim başına bir kez haber verir."""
    if not (CHAT_ID and state["coins"]):
        return
    now = local_now(state)
    if not report_due(state, now):
        return
    try:
        send_report(state, now)
    except Exception as exc:
        log.exception("Rapor gönderilemedi")
        sid = current_slot(state, now)
        if state.get("last_error_slot") != sid:
            state["last_error_slot"] = sid
            reply(f"⚠️ Rapor alınamadı: {html.escape(str(exc))[:300]}", CHAT_ID)


# ==================================================================== Komutlar

CommandFn = Callable[[dict[str, Any], list[str]], Reply]
COMMANDS: dict[str, CommandFn] = {}


def command(*names: str) -> Callable[[CommandFn], CommandFn]:
    """Fonksiyonu verilen komut adları (ve eş anlamlıları) için kaydeder."""

    def register(fn: CommandFn) -> CommandFn:
        for name in names:
            COMMANDS[name] = fn
        return fn

    return register


def normalize_coin(raw: str) -> str:
    """' sol/usdt ' -> 'SOL'."""
    coin = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
    return coin[:-4] if coin.endswith("USDT") and len(coin) > 4 else coin


@command("yardim", "yardım", "help", "start")
def cmd_help(state: dict[str, Any], args: list[str]) -> Reply:
    return HELP_TEXT


def coin_list_text(state: dict[str, Any]) -> str:
    if not state["coins"]:
        return "📋 Liste boş. Eklemek için /ekle"
    items = "\n".join(f"{i}. <b>{c}</b>" for i, c in enumerate(state["coins"], 1))
    return f"📋 <b>Takip edilen coinler</b> ({len(state['coins'])})\n\n{items}\n\n➕ /ekle   ➖ /sil"


@command("liste", "listele", "list")
def cmd_list(state: dict[str, Any], args: list[str]) -> Reply:
    return coin_list_text(state)


@command("ekle", "add")
def cmd_add(state: dict[str, Any], args: list[str]) -> Reply:
    if not args:
        state["pending"] = "ekle"
        return "➕ Eklemek istediğin coin(ler)i yaz.\nÖrnek: <code>BTC ETH SOL</code>", ask("BTC ETH SOL")
    added, existing, rejected = [], [], []
    for raw in args:
        coin = normalize_coin(raw)
        if not coin:
            continue
        if coin not in state["coins"]:
            coin, status = market.resolve(coin)
        else:
            status = "TRADING"
        if coin in state["coins"]:
            existing.append(coin)
        elif len(state["coins"]) >= MAX_COINS:
            rejected.append(f"{coin} (liste dolu, en fazla {MAX_COINS})")
        elif status == "TRADING":
            state["coins"].append(coin)
            added.append(coin)
        elif status:
            rejected.append(f"{coin} (işlem durumu: {status})")
        else:
            rejected.append(f"{coin} (Binance Futures'ta {coin}USDT perpetual yok)")
    lines = []
    if added:
        lines.append("✅ Eklendi: " + ", ".join(added))
    if existing:
        lines.append("ℹ️ Zaten listede: " + ", ".join(existing))
    if rejected:
        lines.append("❌ Eklenmedi: " + "; ".join(rejected))
    lines.append("\n" + coin_list_text(state))
    return "\n".join(lines)


@command("sil", "kaldir", "kaldır", "remove")
def cmd_remove(state: dict[str, Any], args: list[str]) -> Reply:
    if not args:
        if not state["coins"]:
            return "Liste zaten boş. Eklemek için /ekle"
        state["pending"] = "sil"
        items = [(f"❌ {c}", f"sil:{c}") for c in state["coins"]] + [("🗑 Hepsini sil", "sil:hepsi")]
        return "Silmek istediğin coine dokun (ya da adını yaz):", buttons(items)
    if args[0].lower() in ("hepsi", "tumu", "tümü", "all"):
        state["coins"], state["history"] = [], {}
        return "🗑 Liste temizlendi."
    removed, missing = [], []
    for raw in args:
        coin = normalize_coin(raw)
        if coin in state["coins"]:
            state["coins"].remove(coin)
            state["history"].pop(coin, None)
            removed.append(coin)
        elif coin:
            missing.append(coin)
    lines = []
    if removed:
        lines.append("🗑 Silindi: " + ", ".join(removed))
    if missing:
        lines.append("ℹ️ Listede yok: " + ", ".join(missing))
    lines.append("\n" + coin_list_text(state))
    return "\n".join(lines)


@command("aralik", "aralık")
def cmd_interval(state: dict[str, Any], args: list[str]) -> Reply:
    key = INTERVAL_ALIASES.get(args[0].lower()) if args else None
    if not key:
        items = [(("✅ " if k == state["interval"] else "") + INTERVAL_LABELS[k], f"aralik:{k}") for k in INTERVALS]
        return (f"⏱ Gönderim sıklığını seç (şu an: {INTERVAL_LABELS[state['interval']]}):", buttons(items, per_row=2))
    change_schedule(state, "interval", key)
    return f"✅ Gönderim sıklığı: {INTERVAL_LABELS[key]}" + schedule_warning(state)


@command("saat", "saatler")
def cmd_hours(state: dict[str, Any], args: list[str]) -> Reply:
    usage = (
        "Örnek: <code>09-23</code> (09:00–23:00), <code>22-02</code> (gece yarısını aşar), "
        "<code>0-24</code> (her zaman)"
    )
    if not args:
        state["pending"] = "saat"
        items = [(label, f"saat:{value}") for label, value in SAAT_PRESETS]
        return (
            f"🕒 Gönderim saatlerini seç ya da yaz (şu an: {window_text(state['window'])}).\n{usage}",
            buttons(items, per_row=2),
        )
    text = "".join(args)
    if text.lower() in ("hepsi", "her", "herzaman"):
        text = "0-24"
    match = re.fullmatch(r"(\d{1,2})(?::00)?[-–](\d{1,2})(?::00)?", text)
    if not match:
        return "❌ Anlaşılamadı. " + usage
    start, end = int(match.group(1)), int(match.group(2))
    valid = 0 <= start <= 23 and 0 <= end <= 24 and (start != end % 24 or (start, end) == (0, 24))
    if not valid:
        return "❌ Geçersiz aralık. " + usage
    state["window"] = [start, end or 24]
    reply_text = f"✅ Gönderim saatleri: {window_text(state['window'])} ({html.escape(state['tz'])})"
    return reply_text + schedule_warning(state)


def resolve_timezone(name: str) -> str | None:
    """'europe/istanbul' -> 'Europe/Istanbul'; tanınmazsa None."""
    try:
        ZoneInfo(name)
        return name
    except (ZoneInfoNotFoundError, ValueError):
        pass
    return {z.lower(): z for z in available_timezones()}.get(name.lower())


@command("tz", "zaman")
def cmd_timezone(state: dict[str, Any], args: list[str]) -> Reply:
    if not args:
        state["pending"] = "tz"
        items = [(("✅ " if z == state["tz"] else "") + z, f"tz:{z}") for z in TZ_PRESETS]
        return f"🌍 Saat dilimini seç ya da yaz (şu an: {state['tz']}):", buttons(items, per_row=2)
    zone = resolve_timezone(args[0])
    if not zone:
        return f"❌ Tanınmayan saat dilimi: {html.escape(args[0])}\nÖrnek: Europe/Istanbul"
    change_schedule(state, "tz", zone)
    return f"✅ Saat dilimi: {zone}"


@command("durum")
def cmd_status(state: dict[str, Any], args: list[str]) -> Reply:
    now = local_now(state)
    if not state["coins"]:
        next_text = "liste boş (/ekle)"
    elif report_due(state, now):
        next_text = "birkaç saniye içinde"
    else:
        nxt = following_report(state, now)
        next_text = f"{nxt:%d.%m %H:%M}" if nxt else "-"
    return (
        f"⚙️ <b>Bot Ayarları</b>\n\n"
        f"🪙 Coinler: {', '.join(state['coins']) or '-'}\n"
        f"⏱ Sıklık: {INTERVAL_LABELS[state['interval']]}  (/aralik)\n"
        f"🕒 Gönderim saatleri: {window_text(state['window'])}  (/saat)\n"
        f"🌍 Saat dilimi: {html.escape(state['tz'])}  (/tz)\n\n"
        f"Şu an: {now:%d.%m.%Y %H:%M}\n"
        f"⏭ Sonraki rapor: {next_text}"
    )


@command("simdi", "şimdi")
def cmd_now(state: dict[str, Any], args: list[str]) -> Reply:
    if not state["coins"]:
        return "Liste boş. Önce /ekle ile coin ekle."
    send_report(state, local_now(state))
    return None


def handle_command(state: dict[str, Any], text: str) -> Reply:
    name, _, rest = text.partition(" ")
    name = name[1:].split("@")[0].lower()
    args = [a for a in re.split(r"[\s,;]+", rest) if a]
    state.pop("pending", None)  # yeni komut bekleyen soruyu iptal eder
    fn = COMMANDS.get(name)
    return fn(state, args) if fn else "Bilinmeyen komut. /yardim yazabilirsin."


def handle_message(state: dict[str, Any], chat: str, text: str) -> Reply:
    """Yetkili sohbetten gelen bir mesaja (veya buton tıklamasına) yanıt üretir."""
    if text.startswith("/"):
        return handle_command(state, text)
    if text.lower() in CANCEL_WORDS:
        state.pop("pending", None)
        return "İptal edildi."
    pending = state.get("pending")
    if not pending:
        return "Komut listesi için /yardim yaz."
    return handle_command(state, f"/{pending} {text}")


def parse_update(update: dict[str, Any]) -> tuple[str, str, str | None]:
    """Telegram güncellemesinden (sohbet, metin, callback_id) çıkarır.

    Buton tıklamaları 'aralik:4h' biçimindeki veriden '/aralik 4h' komutuna çevrilir.
    """
    query = update.get("callback_query")
    if query:
        message = query.get("message") or {}
        text = "/" + (query.get("data") or "").replace(":", " ", 1)
        callback_id = query.get("id")
    else:
        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        callback_id = None
    chat = str(message.get("chat", {}).get("id", ""))
    return chat, text, callback_id


def process_updates(state: dict[str, Any], wait: int = 0) -> int:
    """Yeni mesajları ve buton tıklamalarını işler; işlenen güncelleme sayısını döndürür."""
    try:
        result = tg(
            "getUpdates",
            http_timeout=wait + 15,
            offset=state["offset"],
            timeout=wait,
            allowed_updates=json.dumps(["message", "callback_query"]),
        )
    except Exception:
        log.exception("Telegram güncellemeleri alınamadı")
        time.sleep(ERROR_PAUSE)
        return 0
    updates = result.get("result", [])
    for update in updates:
        state["offset"] = update["update_id"] + 1
        chat, text, callback_id = parse_update(update)
        if callback_id:
            # Eski butonlarda yanıt süresi dolmuş olabilir; bu hata önemsiz.
            with contextlib.suppress(Exception):
                tg("answerCallbackQuery", callback_query_id=callback_id)
        if not chat or not text:
            continue
        if not CHAT_ID:
            reply(
                f"👋 Kurulum modu.\nChat ID: <code>{html.escape(chat)}</code>\n"
                "Bunu GitHub'da TELEGRAM_CHAT_ID secret'ı olarak ekle.",
                chat,
            )
            continue
        if chat != CHAT_ID:
            reply("⛔ Bu bot özel kullanım içindir.", chat)
            continue
        try:
            result_reply = handle_message(state, chat, text)
        except Exception as exc:
            log.exception("Komut işlenemedi")
            result_reply = f"⚠️ Hata: {html.escape(str(exc))[:300]}"
        reply(result_reply, chat)
    return len(updates)


# ================================================================== Ana akış


def register_menu(state: dict[str, Any]) -> None:
    if state.get("commands_set") == COMMANDS_VERSION:
        return
    try:
        tg(
            "setMyCommands",
            commands=json.dumps([{"command": c, "description": d} for c, d in MENU], ensure_ascii=False),
        )
        state["commands_set"] = COMMANDS_VERSION
    except Exception:
        log.exception("Komut menüsü ayarlanamadı")


def confirm_updates(state: dict[str, Any]) -> None:
    """İşlenen mesajları Telegram'a onaylatır; state kaydedilemese bile komutlar tekrar çalışmaz."""
    with contextlib.suppress(Exception):
        tg("getUpdates", offset=state["offset"], timeout=0, limit=1)


def run_continuous(state: dict[str, Any], seconds: int) -> None:
    """Belirtilen süre boyunca mesajları dinler ve zamanı gelen raporları gönderir."""
    deadline = time.monotonic() + seconds
    process_updates(state)
    maybe_report(state)
    while (remaining := deadline - time.monotonic()) > 3:
        process_updates(state, wait=int(min(LONG_POLL, remaining - 2)))
        maybe_report(state)


def run_short(state: dict[str, Any]) -> None:
    """Tek kontrol; mesaj gelmişse kullanıcı aktif sayılır ve bot kısa süre açık kalır."""
    started = time.monotonic()
    last_activity = started if process_updates(state) else None
    maybe_report(state)
    while last_activity is not None:
        now = time.monotonic()
        remaining = SESSION_MAX - (now - started)
        if remaining < 5 or now - last_activity > SESSION_IDLE:
            break
        if process_updates(state, wait=int(min(LONG_POLL, remaining))):
            last_activity = time.monotonic()
        maybe_report(state)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not TOKEN:
        log.error("TELEGRAM_BOT_TOKEN secret'ı tanımlı değil.")
        return 1
    state = load_state()
    register_menu(state)
    try:
        if RUN_SECONDS > 0:
            run_continuous(state, RUN_SECONDS)
        else:
            run_short(state)
    finally:
        confirm_updates(state)
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
