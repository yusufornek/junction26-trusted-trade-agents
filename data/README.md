# Challenge veri dosyaları

Valio Aimo resource portal'ından indirildi. Tamamı sentetiktir, challenge dışında kullanılamaz.

| Dosya | Boyut | İçerik |
|---|---|---|
| `product_data.json.gz` | 14,6 MB (açılınca **83 MB**) | 17.546 ürün — id, kategori, satış birimi, lot size, sıcaklık, tedarikçi, menşe, synkkaData (çok dilli isim, pazarlama metni, besin/alerjen) |
| `product_data_schema.json` | 30 KB | Yukarıdaki dosyanın JSON Schema'sı |
| `delivery_reliability.csv` | 50 KB | Ürün başına teslim güvenilirliği (0–1). Her üründe yok. |
| `carbon_footprint.csv` | 150 KB | kg CO2-eq/kg. **Ürün grubu ortalaması**, bireysel ölçüm değil. Her üründe yok. |

## Ürün verisini açma

```bash
gunzip -k data/product_data.json.gz
```

Ham JSON 83 MB olduğu için repoya sıkıştırılmış hâliyle eklendi (`.gitignore` ham dosyayı dışarıda tutuyor).

> 83 MB'ı runtime'da parse etmeyin — bir kerelik ön-işlemeyle slim index üretin. Gerekçe: [../PLAN-24H.md](../PLAN-24H.md), Bölüm 3.
