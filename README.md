# Trusted Trade Agents — Junction 2026

Valio Aimo challenge'ı için hazırlık dokümanları ve 24 saatlik uygulama planı.

> ⚠️ **Private repo.** Dosyalarda challenge portal şifresi ve mock API anahtarı düz metin olarak bulunuyor. Public yapmadan önce temizleyin.

## Dosyalar

| Dosya | İçerik |
|---|---|
| [PLAN-24H.md](PLAN-24H.md) | **Ana doküman.** Canlı test edilmiş API bulguları, mimari, saat saat takvim, demo senaryosu |
| [VALIO-AIMO-CHALLENGE-TR.md](VALIO-AIMO-CHALLENGE-TR.md) | Valio Aimo challenge brief'i — Türkçe |
| [ESPOO-CHALLENGE-TR.md](ESPOO-CHALLENGE-TR.md) | Espoo Belediyesi challenge brief'i — Türkçe (seçilmedi) |
| [CHALLENGE-KARSILASTIRMA.md](CHALLENGE-KARSILASTIRMA.md) | İki challenge'ın karşılaştırması ve seçim gerekçesi |

## Seçilen challenge

**Valio Aimo — Trusted Trade Agents.** Bir AI satın alma ajanının toptancıdan güvenli, açık ve doğru insan onayıyla gıda satın almasını sağlayan prototip.

### Puanlama
| Kalem | Ağırlık |
|---|---|
| Çalışan uçtan uca demo | %30 |
| Kullanıcı ihtiyacını çözme | %20 |
| Otonomi ve kontroller (guardrails) | %20 |
| İstisnalara dayanıklılık | %15 |
| Açıklık ve üretime giden yol | %15 |

## Mock API — hızlı referans

Base: `https://junction26.valioaimo.fi` · Header: `X-API-Key: <anahtar>`

```
POST /api/price         {customer, requestedDate, products: ["462626"]}
POST /api/availability  aynı gövde
GET  /api/products/download
GET  /api/delivery-reliability/download
GET  /api/carbon-footprint/download
```

**Sipariş endpoint'i yok, arama endpoint'i yok.** İkisi de sizin tarafınızda.
`products` düz string dizisidir — nesne gönderirseniz 422 alırsınız.

Detaylı bulgular ve tuzaklar için [PLAN-24H.md](PLAN-24H.md) Bölüm 1–2.
