# Binance Futures Hacim Botu

Seçilen Binance **Futures (USDT-M perpetual)** coinlerinin **son fiyatını** ve
**24 saatlik hacmini (USDT)** Telegram'a raporlar. Ücretsizdir; sunucu gerekmez,
tamamen GitHub Actions üzerinde çalışır.

## Rapor örneği

```
📊 Binance Futures · 24 Saatlik Hacim
🗓 18.10.2026  🕒 19:30  (Europe/Istanbul)
💰 Toplam hacim: 25,52 Mr $
📈 En çok artan: BTC +8,82%
━━━━━━━━━━━━━━━━━━
🟢 BTC  ·  81.162,80 $          ← son fiyat
Hacim ▲ +8,82%  ·  15,23 Mr $   ← bir önceki rapora göre
19:30    15.234.567.890         ← son 3 hacim (USDT)
19:00    14.000.000.000
18:30    13.500.000.000
```

## Komutlar

Komutlara Telegram'daki **Menü** düğmesinden ulaşılır. Bir komuta uzun basarsan
gönderilmeden yazı kutusuna gelir.

| Komut | Açıklama |
|---|---|
| `/ekle` | Coin ekle. Bot sorar, coin adlarını yazarsın: `BTC ETH SOL` (`/ekle BTC ETH` da olur) |
| `/sil` | Coin sil (butondan seç) · `/sil hepsi` listeyi temizler |
| `/liste` | Takip edilen coinler |
| `/aralik` | Gönderim sıklığı: 30 dk · 1 · 4 · 12 saat |
| `/saat` | Gönderim saatleri, ör. `09-23`, `0-24` (her zaman), `22-02` (gece yarısını aşar) |
| `/tz` | Saat dilimi, ör. `Europe/Istanbul` |
| `/durum` | Ayarlar ve sonraki rapor zamanı |
| `/simdi` | Raporu hemen gönder |
| `iptal` | Botun beklediği soruyu iptal eder |

Raporlar saat dilimine göre hizalıdır: 30 dk → :00 ve :30, 4 saat → 00/04/08…, 12 saat → 00/12.

## Nasıl çalışır

- `.github/workflows/hacim-bot.yml` botu her seferinde ~9 dakika çalıştırır; bot bu sürede
  mesajlara anında yanıt verir ve zamanı gelen raporu gönderir. Bitince bir sonraki
  çalışmayı kendisi başlatır, böylece bot kesintisiz açık kalır. 5 dakikalık zamanlayıcı
  sadece yedektir.
- Coin listesi ve ayarlar `state.json` dosyasında tutulur ve her çalışmadan sonra repoya kaydedilir.
- Veri kaynağı Binance Futures API'sidir (`/fapi/v1/exchangeInfo`, `/fapi/v1/ticker/24hr`).
  GitHub sunucuları ABD'de olduğu için `fapi.binance.com` erişimi engellenir; bot aynı veriyi
  `www.binance.com/fapi` üzerinden alır.

## Güvenlik

- Bot token'ı ve Chat ID yalnızca **GitHub Secrets**'ta durur; kodda, `state.json`'da ve
  Actions kayıtlarında görünmez.
- Bot sadece `TELEGRAM_CHAT_ID` sohbetinden gelen komutları uygular, diğerlerini reddeder.

## Kurulum (sıfırdan)

1. Telegram'da **@BotFather** → `/newbot` ile bot oluştur, token'ı al. Kendi botuna `/start` gönder.
2. GitHub'da **Public** repo aç (Actions dakikaları public repolarda ücretsizdir) ve dosyaları yükle:
   `bot.py`, `ruff.toml`, `tests/test_bot.py`, `.github/workflows/hacim-bot.yml`,
   `.github/workflows/tests.yml`.
3. Repo → **Settings → Secrets and variables → Actions → New repository secret**:
   `TELEGRAM_BOT_TOKEN` = BotFather token'ı.
4. **Actions → hacim-bot → Run workflow**. Bot Telegram'da Chat ID'ni yazar;
   onu `TELEGRAM_CHAT_ID` secret'ı olarak ekle ve workflow'u bir kez daha çalıştır.

## Yönetim

- **Durdurmak:** Actions → hacim-bot → sağ üstteki `…` → **Disable workflow**.
  Tekrar başlatmak için **Enable workflow**, ardından **Run workflow**.
- **Testler:** `python3 -m unittest discover -s tests` (kod değişince GitHub'da otomatik çalışır).
- **Token yenileme:** BotFather → `/revoke` → yeni token'ı `TELEGRAM_BOT_TOKEN` secret'ına yaz.
