"""bot.py birim testleri. Çalıştırmak için: python -m unittest discover -s tests"""

from __future__ import annotations

import copy
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest import mock
from zoneinfo import ZoneInfo

os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
os.environ["TELEGRAM_CHAT_ID"] = "42"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot

IST = ZoneInfo("Europe/Istanbul")

EXCHANGE_INFO = {
    "symbols": [
        {"symbol": "BTCUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
        {"symbol": "ETHUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
        {"symbol": "1000PEPEUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
        {"symbol": "OLDUSDT", "status": "SETTLING", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
        {"symbol": "BTCUSDT_261225", "status": "TRADING", "contractType": "CURRENT_QUARTER", "quoteAsset": "USDT"},
        {"symbol": "SOLUSDC", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDC"},
    ]
}
TICKERS = [
    {"symbol": "BTCUSDT", "quoteVolume": "15234567890.12", "lastPrice": "81162.80"},
    {"symbol": "ETHUSDT", "quoteVolume": "9876543210.5", "lastPrice": "2650.37"},
    {"symbol": "1000PEPEUSDT", "quoteVolume": "412345678", "lastPrice": "0.012345"},
    {"symbol": "XRPUSDT", "quoteVolume": "1", "lastPrice": "1"},
]


def fake_fetch(path: str, **params: Any) -> Any:
    return {"exchangeInfo": EXCHANGE_INFO, "ticker/24hr": TICKERS}[path]


def new_state(**overrides: Any) -> dict[str, Any]:
    state = copy.deepcopy(bot.DEFAULT_STATE)
    state.update(overrides)
    return state


class FormatTests(unittest.TestCase):
    def test_tr_number(self) -> None:
        self.assertEqual(bot.tr_number(1234567.891, 2), "1.234.567,89")
        self.assertEqual(bot.fmt_usdt(15234567890.12), "15.234.567.890")

    def test_fmt_short(self) -> None:
        self.assertEqual(bot.fmt_short(15_234_567_890), "15,23 Mr")
        self.assertEqual(bot.fmt_short(412_345_678), "412,35 Mn")
        self.assertEqual(bot.fmt_short(1_500), "1,50 B")
        self.assertEqual(bot.fmt_short(999), "999")

    def test_fmt_pct(self) -> None:
        self.assertEqual(bot.fmt_pct(2.345), "+2,35%")
        self.assertEqual(bot.fmt_pct(-0.5), "-0,50%")

    def test_fmt_price(self) -> None:
        cases = {
            81162.8: "81.162,80",
            2650.37: "2.650,37",
            1.2345: "1,2345",
            12.5: "12,50",
            0.5: "0,50",
            0.01235: "0,01235",
            0.2: "0,20",
            0.00001234: "0,00001234",
            0: "0,00",
        }
        for price, expected in cases.items():
            with self.subTest(price=price):
                self.assertEqual(bot.fmt_price(price), expected)

    def test_window_text(self) -> None:
        self.assertEqual(bot.window_text([0, 24]), "her zaman")
        self.assertEqual(bot.window_text([22, 2]), "22:00 – 02:00")
        self.assertEqual(bot.window_text([18, 24]), "18:00 – 00:00")


class TimingTests(unittest.TestCase):
    def test_in_window(self) -> None:
        self.assertTrue(bot.in_window(9, [9, 23]))
        self.assertFalse(bot.in_window(23, [9, 23]))
        self.assertTrue(bot.in_window(1, [22, 2]))
        self.assertFalse(bot.in_window(12, [22, 2]))
        self.assertTrue(all(bot.in_window(h, [0, 24]) for h in range(24)))

    def test_slot_id(self) -> None:
        now = datetime(2026, 10, 18, 19, 47, tzinfo=IST)
        self.assertEqual(bot.slot_id(now, 30), "30@2026-10-18T19:30")
        self.assertEqual(bot.slot_id(now, 240), "240@2026-10-18T16:00")

    def test_report_due_once_per_slot(self) -> None:
        state = new_state()
        now = datetime(2026, 10, 18, 19, 47, tzinfo=IST)
        self.assertTrue(bot.report_due(state, now))
        state["last_slot"] = bot.slot_id(now, 30)
        self.assertFalse(bot.report_due(state, now))
        self.assertTrue(bot.report_due(state, now.replace(hour=20, minute=1)))

    def test_following_report(self) -> None:
        state = new_state(interval="1h", window=[9, 23])
        now = datetime(2026, 10, 18, 22, 15, tzinfo=IST)
        self.assertEqual(bot.following_report(state, now), datetime(2026, 10, 19, 9, 0, tzinfo=IST))
        now = datetime(2026, 10, 18, 10, 15, tzinfo=IST)
        self.assertEqual(bot.following_report(state, now), datetime(2026, 10, 18, 11, 0, tzinfo=IST))

    def test_history_label(self) -> None:
        now = datetime(2026, 10, 18, 19, 47, tzinfo=IST)
        self.assertEqual(bot.history_label("2026-10-18T19:30+03:00", now), "19:30")
        self.assertEqual(bot.history_label("2026-10-17T23:30+03:00", now), "17.10 23:30")
        self.assertEqual(bot.history_label("18:30", now), "18:30")  # eski sürüm kaydı
        self.assertEqual(bot.history_label("önceki", now), "önceki")


class MarketTests(unittest.TestCase):
    def setUp(self) -> None:
        self.market = bot.FuturesMarket(fake_fetch)

    def test_only_usdt_perpetuals(self) -> None:
        self.assertEqual(set(self.market.symbols()), {"BTCUSDT", "ETHUSDT", "1000PEPEUSDT", "OLDUSDT"})

    def test_resolve(self) -> None:
        self.assertEqual(self.market.resolve("BTC"), ("BTC", "TRADING"))
        self.assertEqual(self.market.resolve("PEPE"), ("1000PEPE", "TRADING"))
        self.assertEqual(self.market.resolve("OLD"), ("OLD", "SETTLING"))
        self.assertEqual(self.market.resolve("NOPE"), ("NOPE", None))

    def test_tickers(self) -> None:
        tickers = self.market.tickers(["BTC", "1000PEPE", "NOPE"])
        self.assertEqual(set(tickers), {"BTC", "1000PEPE"})

    def test_binance_falls_back_on_geo_block(self) -> None:
        blocked = urllib.error.HTTPError("u", 451, "Unavailable", None, None)  # type: ignore[arg-type]
        with (
            mock.patch.object(bot, "http_json", side_effect=[blocked, {"ok": 1}]) as http,
            self.assertLogs("hacim-bot", "WARNING"),
        ):
            self.assertEqual(bot.binance("ping"), {"ok": 1})
        self.assertEqual(http.call_count, 2)
        self.assertTrue(http.call_args_list[1].args[0].startswith(bot.BINANCE_BASES[1]))

    def test_binance_raises_client_errors(self) -> None:
        bad = urllib.error.HTTPError("u", 400, "Bad Request", None, None)  # type: ignore[arg-type]
        with mock.patch.object(bot, "http_json", side_effect=bad), self.assertRaises(urllib.error.HTTPError):
            bot.binance("ping")


class HttpTests(unittest.TestCase):
    def test_retries_transient_errors(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = io.BytesIO(b'{"ok": true}')
        flaky = [urllib.error.URLError("reset"), response]
        with mock.patch("urllib.request.urlopen", side_effect=flaky), mock.patch("time.sleep") as sleep:
            self.assertEqual(bot.http_json("https://example.com"), {"ok": True})
        sleep.assert_called_once_with(1)

    def test_does_not_retry_client_errors(self) -> None:
        error = urllib.error.HTTPError("u", 400, "Bad Request", None, None)  # type: ignore[arg-type]
        with (
            mock.patch("urllib.request.urlopen", side_effect=error) as urlopen,
            self.assertRaises(urllib.error.HTTPError),
        ):
            bot.http_json("https://example.com")
        self.assertEqual(urlopen.call_count, 1)


class ReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 10, 18, 19, 30, tzinfo=IST)
        self.tickers = bot.FuturesMarket(fake_fetch).tickers(["BTC", "ETH", "1000PEPE"])

    def test_first_report(self) -> None:
        state = new_state(coins=["ETH", "BTC", "NOPE"])
        texts, history = bot.build_report(state, self.now, self.tickers)
        text = "\n".join(texts)
        self.assertIn("<b>BTC</b>  ·  81.162,80 $", text)
        self.assertIn("<b>ETH</b>  ·  2.650,37 $", text)
        self.assertIn("ilk kayıt", text)
        self.assertIn("⚠️ <b>NOPE</b> — veri alınamadı", text)
        self.assertLess(text.index("<b>BTC</b>"), text.index("<b>ETH</b>"))  # hacme göre sıralı
        self.assertEqual(history["BTC"], [["2026-10-18T19:30+03:00", 15234567890.12]])
        self.assertNotIn("NOPE", history)

    def test_change_and_history_limit(self) -> None:
        old = [["2026-10-18T19:00+03:00", 14_000_000_000.0], ["18:30", 13e9], ["18:00", 12e9]]
        state = new_state(coins=["BTC"], history={"BTC": old})
        texts, history = bot.build_report(state, self.now, self.tickers)
        self.assertEqual(len(history["BTC"]), bot.HISTORY_LEN)
        self.assertEqual(history["BTC"][1], old[0])
        self.assertIn("▲ <b>+8,82%</b>", texts[0])
        self.assertIn("En çok artan: <b>BTC</b> +8,82%", texts[0])
        self.assertIn("15.234.567.890", texts[0])
        self.assertIn("14.000.000.000", texts[0])

    def test_build_report_is_pure(self) -> None:
        state = new_state(coins=["BTC"])
        snapshot = json.dumps(state)
        bot.build_report(state, self.now, self.tickers)
        self.assertEqual(json.dumps(state), snapshot)

    def test_no_data_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            bot.build_report(new_state(coins=["NOPE"]), self.now, self.tickers)

    def test_long_reports_are_split(self) -> None:
        coins = [f"C{i}" for i in range(bot.MAX_COINS)]
        tickers = {c: {"quoteVolume": "123456789", "lastPrice": "1.5"} for c in coins}
        old = [["2026-10-18T19:00+03:00", 1e8], ["2026-10-18T18:30+03:00", 1e8]]
        state = new_state(coins=coins, history={c: old for c in coins})
        texts, _ = bot.build_report(state, self.now, tickers)
        self.assertGreater(len(texts), 1)
        self.assertTrue(all(len(t) <= 4096 for t in texts))
        self.assertIn("Binance Futures", texts[0])
        self.assertIn("Sonraki rapor", texts[-1])


class StateTests(unittest.TestCase):
    def test_migrates_old_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(json.dumps({"coins": ["BTC"], "last_volumes": {"BTC": 1.0}, "commands_set": True}))
            state = bot.load_state(path)
        self.assertNotIn("last_volumes", state)
        self.assertEqual(state["history"], {"BTC": [["önceki", 1.0]]})
        self.assertEqual(state["commands_set"], 0)

    def test_corrupt_file_uses_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{bozuk")
            with self.assertLogs("hacim-bot", "ERROR"):
                state = bot.load_state(path)
        self.assertEqual(state["coins"], bot.DEFAULT_STATE["coins"])

    def test_save_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state = new_state(coins=["SOL"])
            bot.save_state(state, path)
            self.assertEqual(bot.load_state(path)["coins"], ["SOL"])
            self.assertFalse(path.with_suffix(".tmp").exists())


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(bot, "market", bot.FuturesMarket(fake_fetch))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = new_state(coins=["BTC"])

    def say(self, text: str) -> bot.Reply:
        return bot.handle_message(self.state, "42", text)

    def test_add_with_args(self) -> None:
        reply = self.say("/ekle eth, pepe sol/usdt btc old")
        assert isinstance(reply, str)
        self.assertEqual(self.state["coins"], ["BTC", "ETH", "1000PEPE"])
        self.assertIn("Eklendi: ETH, 1000PEPE", reply)
        self.assertIn("Zaten listede: BTC", reply)
        self.assertIn("SOL (Binance Futures'ta SOLUSDT perpetual yok)", reply)
        self.assertIn("OLD (işlem durumu: SETTLING)", reply)

    def test_add_asks_then_accepts_plain_text(self) -> None:
        reply = self.say("/ekle")
        assert isinstance(reply, tuple)
        self.assertTrue(reply[1]["force_reply"])
        self.assertEqual(self.state["pending"], "ekle")
        self.say("eth")
        self.assertEqual(self.state["coins"], ["BTC", "ETH"])
        self.assertNotIn("pending", self.state)

    def test_cancel_pending(self) -> None:
        self.say("/ekle")
        self.assertEqual(self.say("iptal"), "İptal edildi.")
        self.assertNotIn("pending", self.state)
        self.say("eth")
        self.assertEqual(self.state["coins"], ["BTC"])

    def test_remove(self) -> None:
        self.state["coins"] = ["BTC", "ETH"]
        self.state["history"] = {"BTC": [], "ETH": []}
        reply = self.say("/sil")
        assert isinstance(reply, tuple)
        callbacks = [b["callback_data"] for row in reply[1]["inline_keyboard"] for b in row]
        self.assertEqual(callbacks, ["sil:BTC", "sil:ETH", "sil:hepsi"])
        self.say("/sil eth")
        self.assertEqual(self.state["coins"], ["BTC"])
        self.assertNotIn("ETH", self.state["history"])
        self.say("/sil hepsi")
        self.assertEqual(self.state["coins"], [])

    def test_interval(self) -> None:
        self.assertIsInstance(self.say("/aralik"), tuple)
        self.say("/aralik 4s")
        self.assertEqual(self.state["interval"], "4h")

    def test_hours(self) -> None:
        self.say("/saat 9-23")
        self.assertEqual(self.state["window"], [9, 23])
        self.say("/saat 22:00-02:00")
        self.assertEqual(self.state["window"], [22, 2])
        self.say("/saat 18-0")
        self.assertEqual(self.state["window"], [18, 24])
        self.say("/saat herzaman")
        self.assertEqual(self.state["window"], [0, 24])
        for bad in ("/saat 9-9", "/saat 25-3", "/saat sabah"):
            with self.subTest(bad=bad):
                self.assertTrue(str(self.say(bad)).startswith("❌"))
        self.assertEqual(self.state["window"], [0, 24])

    def test_timezone(self) -> None:
        self.say("/tz europe/london")
        self.assertEqual(self.state["tz"], "Europe/London")
        self.assertTrue(str(self.say("/tz Mars/Base")).startswith("❌"))
        self.assertEqual(self.state["tz"], "Europe/London")

    def test_status_and_help(self) -> None:
        self.assertIn("Bot Ayarları", str(self.say("/durum")))
        self.assertIn("/ekle", str(self.say("/start")))
        self.assertIn("Bilinmeyen komut", str(self.say("/foo")))
        self.assertIn("/yardim", str(self.say("merhaba")))

    def test_command_with_bot_suffix(self) -> None:
        self.say("/aralik@Faith81bot 12")
        self.assertEqual(self.state["interval"], "12h")


class UpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = new_state(offset=10)
        self.sent: list[tuple[str, str]] = []
        self.updates: list[dict[str, Any]] = []

        def fake_tg(method: str, **params: Any) -> Any:
            if method == "getUpdates":
                return {"ok": True, "result": self.updates}
            if method == "sendMessage":
                self.sent.append((str(params["chat_id"]), params["text"]))
            return {"ok": True}

        patcher = mock.patch.object(bot, "tg", side_effect=fake_tg)
        self.tg = patcher.start()
        self.addCleanup(patcher.stop)

    def test_parse_callback(self) -> None:
        update = {"callback_query": {"id": "c1", "data": "aralik:4h", "message": {"chat": {"id": 42}}}}
        self.assertEqual(bot.parse_update(update), ("42", "/aralik 4h", "c1"))

    def test_authorized_and_unauthorized(self) -> None:
        self.updates = [
            {"update_id": 10, "message": {"chat": {"id": 42}, "text": "/aralik 1"}},
            {"update_id": 11, "message": {"chat": {"id": 7}, "text": "/sil hepsi"}},
            {"update_id": 12, "callback_query": {"id": "c", "data": "tz:UTC", "message": {"chat": {"id": 42}}}},
        ]
        self.assertEqual(bot.process_updates(self.state), 3)
        self.assertEqual(self.state["offset"], 13)
        self.assertEqual(self.state["interval"], "1h")
        self.assertEqual(self.state["tz"], "UTC")
        self.assertEqual(self.state["coins"], bot.DEFAULT_STATE["coins"])  # yetkisiz sohbet silemedi
        self.assertIn(("7", "⛔ Bu bot özel kullanım içindir."), self.sent)
        self.tg.assert_any_call("answerCallbackQuery", callback_query_id="c")

    def test_telegram_outage_pauses(self) -> None:
        self.tg.side_effect = urllib.error.URLError("down")
        with mock.patch("time.sleep") as sleep, self.assertLogs("hacim-bot", "ERROR"):
            self.assertEqual(bot.process_updates(self.state), 0)
        sleep.assert_called_once_with(bot.ERROR_PAUSE)

    def test_report_error_is_announced_once_per_slot(self) -> None:
        self.state["coins"] = ["BTC"]
        failing = mock.Mock(side_effect=RuntimeError("Binance kapalı"))
        with mock.patch.object(bot, "send_report", failing), self.assertLogs("hacim-bot", "ERROR"):
            bot.maybe_report(self.state)
            bot.maybe_report(self.state)
        self.assertEqual(failing.call_count, 2)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Rapor alınamadı", self.sent[0][1])


class ScheduleTests(unittest.TestCase):
    def test_reports_follow_slot_grid(self) -> None:
        state = new_state(interval="4h", window=[9, 23])
        self.assertFalse(bot.report_due(state, datetime(2026, 10, 18, 9, 30, tzinfo=IST)))  # 08:00 dilimi
        self.assertTrue(bot.report_due(state, datetime(2026, 10, 18, 12, 5, tzinfo=IST)))
        nxt = bot.following_report(state, datetime(2026, 10, 18, 9, 30, tzinfo=IST))
        self.assertEqual(nxt, datetime(2026, 10, 18, 12, 0, tzinfo=IST))

    def test_warns_when_no_report_time(self) -> None:
        self.assertEqual(bot.schedule_warning(new_state(interval="4h", window=[9, 23])), "")
        self.assertIn("hiç rapor zamanı yok", bot.schedule_warning(new_state(interval="12h", window=[9, 11])))
        reply = bot.handle_message(new_state(interval="12h"), "42", "/saat 9-11")
        self.assertIn("hiç rapor zamanı yok", str(reply))

    def test_setting_change_does_not_trigger_extra_report(self) -> None:
        now = datetime(2026, 10, 18, 19, 47, tzinfo=IST)
        state = new_state(last_slot=bot.slot_id(now, 30))
        with mock.patch.object(bot, "local_now", return_value=now):
            bot.handle_message(state, "42", "/aralik 4s")
        self.assertFalse(bot.report_due(state, now))
        self.assertTrue(bot.report_due(state, now.replace(hour=20, minute=0)))

    def test_setting_change_keeps_pending_report(self) -> None:
        now = datetime(2026, 10, 18, 19, 47, tzinfo=IST)
        state = new_state(last_slot="30@2026-10-18T19:00")  # 19:30 raporu henüz gitmedi
        with mock.patch.object(bot, "local_now", return_value=now):
            bot.handle_message(state, "42", "/aralik 1")
        self.assertTrue(bot.report_due(state, now))

    def test_partial_report_is_not_resent(self) -> None:
        coins = [f"C{i}" for i in range(bot.MAX_COINS)]
        old = [["2026-10-18T19:00+03:00", 1e8], ["2026-10-18T18:30+03:00", 1e8]]
        state = new_state(coins=coins, history={c: old for c in coins})
        tickers = {c: {"quoteVolume": "123456789", "lastPrice": "1.5"} for c in coins}
        sent: list[str] = []

        def flaky_send(text: str, *args: Any, **kwargs: Any) -> None:
            if sent:
                raise urllib.error.URLError("timeout")
            sent.append(text)

        market = mock.Mock(tickers=mock.Mock(return_value=tickers))
        with (
            mock.patch.object(bot, "market", market),
            mock.patch.object(bot, "send", side_effect=flaky_send),
            self.assertLogs("hacim-bot", "ERROR"),
        ):
            bot.maybe_report(state)
            bot.maybe_report(state)
        self.assertEqual(len(sent), 1)
        self.assertIsNotNone(state["last_slot"])


if __name__ == "__main__":
    unittest.main()
