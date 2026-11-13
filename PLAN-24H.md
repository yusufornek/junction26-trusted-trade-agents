# Trusted Trade Agents — 24 Saatlik Plan
**Valio Aimo / Junction 2026** — mock API canlı olarak incelendi, bulgular aşağıda.

---

## BÖLÜM 1 — API Gerçekliği (doğrulanmış)

Base: `https://junction26.valioaimo.fi` · Header: `X-API-Key: junct_tDnQn4VI_...`

### Var olan her şey (hepsi bu)
```
GET  /api/ping
POST /api/price           body: {customer, requestedDate, products: ["462626", ...]}
POST /api/availability    body: aynı
GET  /api/products/download
GET  /api/delivery-reliability/download
GET  /api/carbon-footprint/download
```

> **products dizisi düz string dizisidir.** `[{productId, quantity}]` gönderirseniz 422 alırsınız. Bu ilk 20 dakikanızı yiyecek tuzak — şimdi bilerek geçin.

### YOK olanlar — ve bu neden iyi haber

| Yok | Sonuç |
|---|---|
| **Sipariş endpoint'i** | Commitment katmanını **siz** tasarlıyorsunuz. Puanın %35'i (otonomi + istisna) burada ve herkes sıfırdan başlıyor. |
| **Arama endpoint'i** | Ürün keşfi tamamen lokal: 86 MB / **17.546 ürün** JSON. Asıl mühendislik işi bu. |
| **Müşteri/sözleşme API'si** | `customer` alanı fiyatı değiştirmiyor (test edildi: `1` vs `42` aynı). Müşteri profilini siz uyduruyorsunuz. |

### Veri setleri
| Dosya | Boyut | İçerik |
|---|---|---|
| product_data.json | **86 MB**, 17.546 ürün | id, category, salesUnit, allowedLotSize, temperatureCondition, vendorName, countryOfOrigin, **synkkaData** (çok dilli isim, pazarlama metni, besin/alerjen) |
| delivery_reliability.csv | küçük | ürün başına 0–1 güvenilirlik (hepsinde yok) |
| carbon_footprint.csv | küçük | kg CO2-eq/kg, **ürün grubu ortalaması** (bireysel ölçüm değil) |

---

## BÖLÜM 2 — Altın Madenleri (çoğu takımın göremeyeceği 5 şey)

Bunlar gerçek API yanıtlarından çıkarıldı. Demo senaryonuzun tamamı bunların üzerine kurulmalı.

### 1. `comparisonPrice` + `comparisonUnit` = birim dönüşümünün anahtarı
Brief "**1.000 litre** süt" diyor. Ürünler `KI`, `ST`, `PAK`, `PRK` gibi birimlerde satılıyor. Litre bilgisi hiçbir alanda doğrudan yok — **ama** fiyat yanıtı veriyor:

```
462626: catalogPrice 13.00 € · comparisonPrice 2.60 €/L  →  13.00 / 2.60 = 5 L/KI
```

→ **1000 L = 200 KI.** Bu tek satırlık hesap, "kullanıcı ihtiyacını çözme" (%20) kaleminin kalbidir. Litreyi birime çeviremeyen takım soruyu cevaplamamış olur.

### 2. `logisticModel` = kısmi stok motoru hazır geliyor
```
"Terminal" → availableAmount: 999999   (sipariş üzerine, pratikte sınırsız)
"Stock"    → availableAmount: 51       (gerçek stok, KISITLI)
```
462775 ürünü sadece **51 adet** stokta. Kısmi karşılama senaryosunu uydurmanıza gerek yok — **veride zaten var.**

### 3. `closingTime` = sipariş kesim saati (en güçlü istisna)
```
462775 → closingTime: 2026-11-15T17:00  (16 Kasım teslimi için)
```
API'nin `timeNow` alanı **canlı saat** döndürüyor. Yani demo anında "kesim saati geçti, bu ürün artık o tarihe yetişmez" senaryosu **gerçekten** tetiklenir. Sahte değil.

### 4. API tarihi sessizce kaydırıyor ⚠️
```
İstek: requestedDate = 2026-11-14 (yarın)
Yanıt: availableDate = 2026-11-16 (Pazartesi)
```
**Hata vermiyor, uyarı vermiyor — sessizce ötelenmiş tarih dönüyor.** Bunu kontrol etmeyen takımın ajanı "teslim yarın ✓" diyerek yanlış sipariş verir. Siz bunu yakalayıp "istediğiniz tarih karşılanamıyor, en erken Pazartesi — onaylıyor musunuz?" diye insana sorarsanız, "istisnalara dayanıklılık" (%15) kaleminin tamamını alırsınız.

### 5. `validFrom` / `validTo` = sabit fiyat taahhüdünün dayanağı
Her fiyat bir geçerlilik penceresiyle geliyor. Brief "pazarlıkla değil **sabit fiyatla** taahhüt" diyor. Demek ki `order` nesneniz şu olmalı: **fiyatın `validTo`suna kadar geçerli, imzalı bir commitment token'ı.** İstenen "trust protocol" tam olarak budur.

Ek bayraklar: `salesBlock` (satış kapalı), `netPriceFound:false` (müşteriye özel fiyat yok → catalogPrice'a düşüyor), `allowedLotSize: 0.01` (sipariş adım miktarı), `error` (ürün bazlı hata).

---

## BÖLÜM 3 — Mimari (en az kod, en çok puan)

Tek servis. Veritabanı yok. Mikroservis yok. Kuyruk yok.

```
┌─ MCP Server ──────────────────────────────┐   ← Açıklık %15
│  search_products / quote / place_order    │
│  + OpenAPI spec (aynı fonksiyonlar REST)  │
└──────────────┬────────────────────────────┘
               │
┌──────────────▼────────────────────────────┐
│  Core (tek dosya olabilir)                │
│  1. resolve()   isteği → aday ürünler     │  ← lokal index
│  2. enrich()    price + availability çağrısı│  ← SADECE adaylar için
│  3. evaluate()  birim çevir, kısmi/kesim  │  ← İstisna %15
│  4. policy()    guardrail YAML değerlendir│  ← Otonomi %20
│  5. commit()    sipariş VEYA onay talebi  │
│  6. journal()   insan-okur kayıt          │
└──────────────┬────────────────────────────┘
               │
        ┌──────▼──────┐
        │ Approval UI │  tek HTML sayfa: bekleyen onaylar + Onayla/Reddet
        └─────────────┘
```

### Tembel kararlar (tartışmayın, uygulayın)
- **86 MB'ı runtime'da parse etmeyin.** Bir kerelik ön-işleme scripti: `id, en-isim, category, salesUnit, allowedLotSize, allerjen bayrakları` → ~3 MB slim index. Başlangıçta RAM'e alın.
- **Vektör/embedding arama YOK.** Keyword + kategori filtresi yeterli. (`cat=10400` = süt içecekleri; 17.546 üründen 62'si "lactose free + milk".) Gerçek ürün isimleri zaten çok tanımlayıcı.
- **Veritabanı YOK.** Siparişler ve onaylar process içi dict + JSON dosyası. 24 saat için kalıcılık gereksiz.
- **Auth YOK.** Mock API anahtarı dışında kimlik doğrulama kurmayın.
- **LLM'i sadece iki yerde kullanın:** (a) serbest metin isteği → yapılandırılmış sorgu, (b) audit kaydını insan diline çevirme. Fiyat/stok/karar mantığı **deterministik kod** olsun. Jüri "LLM mi hesapladı" sorusunu soracak; "hayır, LLM sadece metni ayrıştırdı" cevabı sizi kurtarır.

### Guardrail dosyası (ilk gün yazın, 20 satır)
```yaml
# policy.yaml — müşterinin kendi yazdığı dosya
customer: hotel-chain-001
autonomous_limit_eur: 2000
require_approval_when:
  - total_value_eur > 2000
  - substitution_outside_preferences
  - delivery_date_shifted          # ← Bölüm 2.4
  - partial_fulfillment_below: 0.9
  - supplier_reliability_below: 0.85
never_autonomous:
  - allergen_profile_change
preferences:
  lactose_free: required
  max_co2_kg_per_kg: 2.5
```
Jüriye bu dosyayı gösterip "guardrail'ler kodda gömülü değil, müşteri kendi yazıyor" demek, slaytta anlatmaktan çok daha güçlü.

---

## BÖLÜM 4 — 24 Saatlik Takvim

Üç kişilik takım varsayımı: **A = backend/core**, **B = entegrasyon/MCP**, **C = UI/demo/sunum**. İki kişiyseniz C'nin işini A ve B son 6 saatte böler.

| Saat | A (core) | B (entegrasyon) | C (UI/sunum) |
|---|---|---|---|
| **0–2** | Veri indir, slim index scripti | API client + 3 endpoint sarmalayıcı, 422 tuzağı geçildi | Demo senaryosu metnini yaz, repo/README iskeleti |
| **2–5** | `resolve()` arama + **birim dönüşümü** (comparisonPrice) | `enrich()` batch price+availability | Onay ekranı statik mockup |
| **5–8** | `evaluate()`: kısmi stok, kesim saati, **tarih kayması** | delivery_reliability + carbon CSV'leri index'e bağla | Ekranı canlı veriye bağla |
| **8–11** | `policy()` YAML motoru + `commit()` | MCP server (3 tool) | Onayla/Reddet akışı çalışıyor |
| **11–13** | `journal()` insan-okur kayıt | OpenAPI spec + README'ye "açık standartlar" bölümü | Audit kaydını ekranda göster |
| **13–15** | **İKAME MOTORU** (aşağıya bak) | Uçtan uca duman testi | Slayt iskeleti |
| **15–18** | Hata ayıklama, edge case | Hata ayıklama | Demo provası #1 |
| **18–20** | Sadece bug fix | Sadece bug fix | Demo provası #2, kayıt al |
| **20–21** | 🔒 **KOD DONDU** | README son hâli | Slaytlar bitti |
| **21–24** | Tampon / uyku / son prova | | |

> **Saat 20'de kod donar.** İstisnasız. Hackathon'da projeler son 4 saatte eklenen özellikle ölür, eksik özellikle değil.

### İkame motoru (saat 13–15) — "iyi-daha iyi-en iyi"
Karmaşık bir şey değil, 30 satır: aynı kategorideki ürünleri al, aday filtrele (laktozsuz şartı korunmalı), üç eksende sırala ve **üçünü de göster**:
- **İyi** = en ucuz birim fiyat
- **Daha iyi** = en yüksek teslim güvenilirliği (CSV'den)
- **En iyi** = en düşük karbon ayak izi (CSV'den)

Jüriye "fiyattan fazlası" bonusunu ve ikame bonusunu **tek ekranda** verir.

---

## BÖLÜM 5 — Demo Senaryosu (bunu ezberleyin)

Happy path **göstermeyin**. 4 dakikalık akış:

1. **İstek:** *"Pazartesiye 1.000 litre laktozsuz süt lazım, bütçe 2.500 €."*
2. **Lookup:** Ajan 17.546 üründen 62 adayı lokal filtreler → **sadece 4'ü için** API çağırır.
   → Ekranda: *"17.546 üründen 4 ürün sorgulandı"* (flyer'daki "never for all products" kuralına uyduğunuzu kanıtlar)
3. **Birim dönüşümü:** *"1.000 L = 200 KI (5 L/birim, comparisonPrice'tan hesaplandı)"*
4. **İstisna #1 — kısmi stok:** En ucuz seçenekte sadece 51 birim var → %26 karşılanıyor.
5. **İstisna #2 — kesim saati:** İkinci seçeneğin kesim saati geçmiş → Pazartesiye yetişmez.
6. **İstisna #3 — tarih kayması:** Üçüncü seçenek için API sessizce 16 Kasım döndürüyor → ajan yakalıyor.
7. **Karar:** Toplam 2.700 € > 2.500 € limiti → **otonom karar verilmiyor.**
8. **Onay ekranı:** İnsan şunu görüyor:
   > *"3 seçenek bulundu. Önerilen: Valio Eila 10 L × 100. Toplam 2.700 € (limitin 200 € üstü).
   > Neden bu: tek başına tam miktarı karşılayan tek seçenek; teslim güvenilirliği 0,97.
   > Reddedilenler: Arla Milkdrink — sadece 51 adet stokta (%26). Valio coffee milk — kesim saati 15 Kas 17:00 geçti.
   > Karbon: 1,2 kg CO2-eq/kg (kategori ortalaması)."*
9. İnsan **Onayla**'ya basar → sipariş fiyatın `validTo` penceresine bağlı commitment olarak kaydedilir.
10. **Audit:** Tüm karar zinciri tek sayfada, zaman damgalı, okunabilir.

Bu tek akış rubric'in **5 kaleminin 5'ine** birden dokunuyor.

---

## BÖLÜM 6 — Yapmayın

| Yapmayın | Neden |
|---|---|
| Chatbot arayüzü | Flyer açıkça *"It is **not** a chatbot"* diyor |
| Tüm ürünler için API çağrısı | Flyer açıkça yasaklıyor — ve yavaş |
| Pazarlık/teklif mantığı | Brief "sabit fiyat, pazarlık değil" diyor |
| Gerçek ödeme / x402 entegrasyonu | "ilgili bağlam, zorunlu değil" — 24 saatte lükstür |
| Mikroservis, Docker Compose, k8s | Jüri çalışan demo görmek istiyor, mimari şeması değil |
| Güzel landing page | Puanlamada karşılığı yok |
| Son 4 saatte yeni özellik | Projeleri bu öldürür |

---

## İlk 30 Dakika — şimdi yapın

1. Veri dosyalarını indirin (portal cookie'si gerekir — tarayıcıdan giriş yapıp indirmek en hızlısı).
2. `POST /api/price` ile tek bir ürün sorgulayıp 200 alın. **products düz string dizisi.**
3. `policy.yaml`'ı yazın. 20 satır. Bitti.
4. Demo senaryosunu (Bölüm 5) bir metin dosyasına yapıştırın — bu sizin spec'iniz.
