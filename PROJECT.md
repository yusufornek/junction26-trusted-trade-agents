# Trusted Trade Agents — Proje Referansı

Valio Aimo / Junction 2026 challenge'ının teknik referansı. Bu dosya yalnızca doğrulanmış olguları içerir.

---

## 1. Challenge Tanımı

Bir AI satın alma ajanının bir toptancıdan gıda satın almasını sağlayan prototip. Akış:

```
Request  →  Lookup  →  Order  →  Approval
istek       ürün,      sabit     ajan karar verir
            fiyat,     fiyatla   veya insan
            stok       taahhüt   imzalar
```

Örnek istek: *"1.000 litre laktozsuz süt lazım, Pazartesiye. Fiyat ve stoğa göre gereksinimlerimizi karşılayan bir seçenek bul."*

### Zorunlu kapsam (core)
1. **Lookup** — ürün, fiyat, stok için yapılandırılmış arama
2. **Request and order** — sorgudan sabit fiyatlı taahhüde geçiş
3. **Permissions and approval** — ajanın tek başına neye karar verdiği, neyin insan imzası gerektirdiği

### Opsiyonel kapsam (bonus)
- Fiyattan fazlası: teslim güvenilirliği ve sürdürülebilirlik etiketleri
- İstisna yönetimi: kısmi stok ve ikameler
- Açıklanabilirlik: ajanın ne yaptığına ve neden yaptığına dair insan-okur kayıt

### Puanlama
| Kalem | Ağırlık |
|---|---|
| Çalışan uçtan uca demo | %30 |
| Kullanıcı ihtiyacını çözme | %20 |
| Otonomi ve kontroller (guardrails) | %20 |
| İstisnalara dayanıklılık | %15 |
| Açıklık ve üretime giden yol | %15 |

### Challenge metninde belirtilen kısıtlar
- Mocklanan şey bugünkü webshop ve backend sistemleridir, marketplace değil.
- Çözüm bir chatbot değildir.
- API'ler yalnızca gerektiğinde sorgulanır, tüm ürünler için değil.
- Fiyatlar sabittir; pazarlıkla belirlenen teklif mekanizması yoktur.
- Ödüller: 1. 4.000 € · 2. 2.000 € · 3. 1.000 €

---

## 2. Mock API

Base URL: `https://junction26.valioaimo.fi`
Kimlik doğrulama: `X-API-Key: <anahtar>` — `/api` altındaki her şey için zorunlu.

### Endpoint'ler (tamamı)

| Method | Path | Gövde |
|---|---|---|
| GET | `/api/ping` | — |
| POST | `/api/availability` | `ProductsRequest` |
| POST | `/api/price` | `ProductsRequest` |
| GET | `/api/products/download` | — (307 → dosya) |
| GET | `/api/delivery-reliability/download` | — (307 → dosya) |
| GET | `/api/carbon-footprint/download` | — (307 → dosya) |

Dokümantasyon: `/docs` (Swagger), `/redoc`, `/openapi.json` — portal oturumu gerektirir.

### İstek gövdesi

```json
{
  "customer": "1",
  "requestedDate": "2026-11-16",
  "products": ["462626", "462775"]
}
```

`products` **düz string dizisidir**. `[{"productId": "...", "quantity": 100}]` biçimi `422 Unprocessable Entity` döndürür.

### Mevcut olmayan endpoint'ler
- **Sipariş / commitment endpoint'i yok.** Taahhüt katmanı API tarafında bulunmuyor.
- **Arama / filtreleme endpoint'i yok.** Ürün keşfi yalnızca indirilen katalog dosyası üzerinden yapılabilir.
- **Müşteri veya sözleşme endpoint'i yok.**

---

## 3. Yanıt Şemaları

### `/api/price` → `PriceResponse`

```
customerId, currency, pricingDate, products[]
  └─ ProductPrice:
       productId
       quantity           (her zaman 1.0 döner)
       catalogPrice       {value, valueWithTax, validFrom, validTo, currency}
       priceWithTax       {value, validFrom, validTo, currency}
       comparisonPrice    {value, valueWithTax, ...}
       comparisonUnit     "L", "KG", ...
       tax                {taxRate, taxValue}
       listPrice          {value, valueWithTax, ...}
       netPriceFound      boolean
       error              boolean
```

Örnek yanıt (462626, customer=1):
```json
{"productId":"462626","quantity":1.0,
 "catalogPrice":{"value":13.0,"valueWithTax":14.76,
                 "validFrom":"2026-09-01T00:00:00+0300",
                 "validTo":"9999-12-31T00:00:00+0200","currency":"EUR"},
 "comparisonPrice":{"value":2.6,...},"comparisonUnit":"L",
 "tax":{"taxRate":13.5,"taxValue":1.76},
 "listPrice":{"value":15.29,"valueWithTax":17.35,...},
 "netPriceFound":false,"error":false}
```

### `/api/availability` → `AvailabilityResponse`

```
timeNow, requestedDate, products[]
  └─ ProductAvailability:
       productId
       closingTime        sipariş kesim zamanı (null olabilir)
       salesBlock         boolean
       logisticModel      "Terminal" | "Stock" | null
       scheduleLines[]
         └─ ScheduleLine: {salesBlock, availableAmount, availableDate, salesUnit}
```

Örnek yanıt:
```json
{"timeNow":"2026-11-13T21:28:48+0200","requestedDate":"2026-11-16T00:00:00+0200",
 "products":[
  {"productId":"462626","closingTime":"2026-11-12T15:00:00+0200","salesBlock":false,
   "logisticModel":"Terminal",
   "scheduleLines":[{"salesBlock":false,"availableAmount":999999.0,
                     "availableDate":"2026-11-16","salesUnit":"KI"}]},
  {"productId":"462775","closingTime":"2026-11-15T17:00:00+0200","salesBlock":false,
   "logisticModel":"Stock",
   "scheduleLines":[{"salesBlock":false,"availableAmount":51.0,
                     "availableDate":"2026-11-16","salesUnit":"ST"}]}]}
```

---

## 4. Doğrulanmış API Davranışları

Aşağıdakilerin tamamı canlı istekle gözlemlendi.

### 4.1 `comparisonPrice` birim dönüşümü sağlar
Ürün verisinde litre/kilogram cinsinden içerik miktarı alanı yoktur. `catalogPrice.value / comparisonPrice.value` oranı, satış biriminin `comparisonUnit` cinsinden karşılığını verir.

```
462626: catalogPrice 13.00 €  ·  comparisonPrice 2.60 €/L  →  5 L / KI
```

### 4.2 `logisticModel` stok tipini belirler
| Değer | Gözlemlenen `availableAmount` |
|---|---|
| `Terminal` | 999999.0 — pratikte sınırsız, sipariş üzerine |
| `Stock` | Sonlu sayı (ör. 51.0) — gerçek stok miktarı |

### 4.3 `timeNow` canlı sunucu saatidir
Yanıt, isteğin yapıldığı andaki gerçek zamanı döndürür. `closingTime` ile karşılaştırma gerçek zamanlı sonuç üretir.

### 4.4 İstenen tarih sessizce ötelenebilir
```
İstek:  requestedDate = 2026-11-14
Yanıt:  scheduleLines[0].availableDate = 2026-11-16
```
HTTP durumu 200'dür; hata, uyarı veya ayrı bir alan üretilmez. Fark yalnızca `requestedDate` ile `availableDate` karşılaştırılarak tespit edilebilir.

### 4.5 Bilinmeyen ürün kodu hata üretmez
`999999` kodu için yanıt `scheduleLines: []` ve `salesBlock: false` içerir; `error` bayrağı set edilmez.

### 4.6 `customer` alanı gözlemlenen fiyatı değiştirmedi
`customer: "1"` ve `customer: "42"` aynı `catalogPrice` değerlerini döndürdü. `netPriceFound` her iki durumda da `false`.

### 4.7 Fiyatların geçerlilik penceresi vardır
Her fiyat nesnesi `validFrom` ve `validTo` taşır. Gözlemlenen örneklerde `validTo` = `9999-12-31`.

### Diğer alanlar
| Alan | Anlamı |
|---|---|
| `salesBlock` | Ürün satışa kapalı |
| `netPriceFound: false` | Müşteriye özel net fiyat bulunamadı, `catalogPrice` geçerli |
| `allowedLotSize` | Sipariş miktarı adımı (gözlemlenen: 0.01) |
| `error` | Ürün bazlı hata bayrağı |

---

## 5. Veri Dosyaları

Dosyalar `data/` klasöründedir. Ayrıntı: [data/README.md](data/README.md)

| Dosya | Boyut | İçerik |
|---|---|---|
| `product_data.json` | 83 MB · **17.546 ürün** | Aşağıdaki alanlar |
| `product_data_schema.json` | 30 KB | JSON Schema |
| `delivery_reliability.csv` | 50 KB | Ürün başına 0–1 teslim güvenilirliği |
| `carbon_footprint.csv` | 150 KB | kg CO2-eq/kg |

### Ürün kaydı alanları
```
id, salesUnitGtin, salesUnit, baseUnit, category, allowedLotSize,
deleted, temperatureCondition, vendorName, countryOfOrigin,
synkkaData, units, deposits
```

`synkkaData` içinde: `gtin`, `lastUpdated`, `countriesOfOrigin`, `names[]` (fi/sv/en), `marketingTexts[]`, `materialAdditionalDescriptions[]`.

Ürün adı yalnızca `synkkaData.names[]` içindedir; üst seviyede ad alanı yoktur. `vendorName` tedarikçi adıdır, ürün adı değildir.

### Veri kapsamı notları
- Teslim güvenilirliği ve karbon ayak izi değerleri **her üründe bulunmaz**.
- Karbon ayak izi değeri ürünün ait olduğu **ürün grubunun ortalamasıdır**, bireysel ölçüm değildir.
- Ürün verisinin kalitesi değişkendir: GTIN kodlu hazır gıdalarda besin ve alerjen bilgisi ayrıntılıyken meyve-sebzede çoğunlukla yoktur.
- Veri sentetiktir ve challenge dışında kullanılamaz.

### Gözlemlenen örnek değerler
| Ürün | Ad | Kategori | Birim |
|---|---|---|---|
| 462626 | Valio coffee milk 5 dl lactose free | 10400 | KI |
| 462775 | Arla Milkdrink 1L lactose free fat free UHT | 10400 | ST |
| 462921 | Valio Eila Luomu semi skimmed milk drink 1 l lactose free, organic | 10400 | KI |
| 463038 | Valio Eila semi skimmed milk drink 10 l novobox lactose free | 10400 | KI |

Kategori `10400` süt içecekleridir. Katalogda adı hem "milk" hem "lactose free" içeren 62 ürün bulunmaktadır.

---

## 6. Portal

`https://junction26.valioaimo.fi` — form tabanlı oturum (`POST /login`, alan adı `password`). Oturum çerezi `/files/...`, `/docs`, `/redoc` ve `/openapi.json` için gereklidir; `/api` altındaki endpoint'ler çerez değil API anahtarı kullanır.

Portal şifresi ve API anahtarı challenge materyalinde verilmiştir. Bu repo private'tır; public yapılması hâlinde her iki değerin de dosyalardan çıkarılması gerekir.

---

## 7. Mentorlar

| Alan | Kişi |
|---|---|
| Marketplace / Ticari | Hanna Forsström, Pirjo Kaartinen |
| Online Satış | Minni Ruhtinas |
| IT / Entegrasyon / Veri yönetişimi | Tommi Nurmi-Aro, Ville Takanen, Riku Tapper |
