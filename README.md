# Binance Hacim Botu — Kurulum

Seçtiğin Binance coinlerinin 24 saatlik hacmini (USDT) Telegram'a gönderir.
Ücretsizdir; GitHub Actions üzerinde çalışır, sunucu gerekmez.

## 1. Telegram botunu oluştur
1. Telegram'da **@BotFather**'a `/newbot` yaz, bota bir ad ve kullanıcı adı ver.
2. Verdiği **token**'ı not al (ör. `123456:ABC...`). Kimseyle paylaşma.
3. Kendi botunu bul ve **/start** gönder.

## 2. GitHub reposunu hazırla
1. GitHub'da yeni bir **Public** repo oluştur (ör. `hacim-bot`).
   Public olması şart: Actions dakikaları sadece public repolarda sınırsızdır.
   Token gizli kalır; repoda sadece coin listesi ve ayarlar görünür.
2. **Add file → Upload files** ile `bot.py` ve `README.md` dosyalarını yükle.
3. **Add file → Create new file** de, dosya adına tam olarak
   `.github/workflows/hacim-bot.yml` yaz ve `hacim-bot.yml` içeriğini yapıştır.

## 3. Token'ı ekle ve Chat ID'yi öğren
1. Repo → **Settings → Secrets and variables → Actions → New repository secret**
   - Ad: `TELEGRAM_BOT_TOKEN`, değer: BotFather'ın verdiği token
2. **Actions** sekmesi → (istenirse workflow'ları etkinleştir) → **hacim-bot → Run workflow**.
3. Bot Telegram'da sana **Chat ID**'ni yazar. (Gelmezse bota tekrar `/start` gönderip workflow'u yeniden çalıştır.)
4. Aynı yerden ikinci secret'ı ekle:
   - Ad: `TELEGRAM_CHAT_ID`, değer: botun verdiği sayı

Gruba göndermek istersen: botu gruba ekle, grupta `/start` yaz; Chat ID eksi ile başlayan sayı olur.

## 4. Kullanım
Workflow'u bir kez daha elle çalıştır; sonrasında 5 dakikada bir kendiliğinden çalışır.

| Komut | Açıklama |
|---|---|
| `/ekle BTC ETH SOL` | Coin ekle (Binance'te USDT paritesi olmalı) |
| `/sil BTC` · `/sil hepsi` | Coin sil / listeyi temizle |
| `/liste` | Takip edilen coinler |
| `/aralik 30m` · `1h` · `4h` · `12h` | Gönderim sıklığı |
| `/saat 09-23` · `/saat 0-24` | Gönderim saatleri (gece yarısını aşabilir: `22-02`) |
| `/tz Europe/Istanbul` | Saat dilimi |
| `/durum` | Ayarlar ve sonraki rapor |
| `/simdi` | Raporu hemen gönder |

## Bilinmesi gerekenler
- GitHub zamanlanmış görevleri bazen 5–15 dk gecikir; komut yanıtları ve raporlar buna göre birkaç dakika kayabilir.
- Raporlar saat dilimine göre hizalıdır: 30m → :00 ve :30, 4h → 00/04/08..., 12h → 00/12.
- Δ sütunu, bir önceki bildirime göre hacim değişimidir.
- Veri kaynağı: `data-api.binance.vision` (Binance'in resmi, herkese açık piyasa verisi adresi).
