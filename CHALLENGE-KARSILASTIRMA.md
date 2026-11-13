# İki Challenge Karşılaştırması
**Valio Aimo — Agentic B2B Commerce** vs. **Espoo Belediyesi — Rethinking Recruitment**

## 1. Hızlı Tablo

| | **Valio Aimo** | **Espoo Belediyesi** |
|---|---|---|
| **Alan** | B2B toptan gıda ticareti, agentic commerce | İşe alım / yetenek eşleştirme etkinlikleri |
| **Soru tipi** | "Bu **arayüzü** inşa et" | "Bu **problemi** yeniden düşün" |
| **Çıktı** | Çalışan uçtan uca sistem (API + ajan) | Konsept + prototip + hikâye |
| **Verilen kaynak** | Sentetik katalog, **mock API'ler, API anahtarı**, dokümante portal | Veri **yok** — sentetik veriyi siz üreteceksiniz; uzman erişimi + örnek senaryolar |
| **Kapsam netliği** | Çok net: 3 zorunlu yetenek tanımlı | Çok açık uçlu: öncesi / sırası / sonrası, istediğiniz aşama |
| **Ödül** | **4.000 / 2.000 / 1.000 €** | 1.500 / 1.000 / 500 € + görünürlük, pilot ihtimali |
| **Ödül dışı kazanç** | — | Espoo + Helsinki + Vantaa liderlerine sunum, ulusal şehir ağı |
| **Zorlayıcı kısıt** | Sabit fiyat (pazarlık yok), açık standartlar şart | EU AI Act, otomatik birey kararı **yasak**, önyargı riski |

## 2. Puanlama Ağırlıkları — En Kritik Fark

**Valio Aimo → %60 teknik yürütme**
- Çalışan uçtan uca demo: **%30**
- İstisnalara dayanıklılık: %15
- Açıklık / üretime giden yol: %15
- Kullanıcı ihtiyacını çözme: %20
- Otonomi & kontroller: %20

**Espoo → %80 konsept ve deneyim**
- Etki & ilgililik: **%45**
- İnovasyon & UX: **%35**
- Sorumlu & uygulanabilir yürütme: %20

> **Sonuç:** Valio'da *kod çalışmazsa kaybedersiniz*. Espoo'da *hikâye ve içgörü zayıfsa kaybedersiniz* — mükemmel bir kod bile %20'lik kalemde yer bulur.

## 3. Gizli Ortak Nokta

İki challenge da aslında **aynı AI sorununu** soruyor:

> "İki taraf da artık makine üretimi temsiller üzerinden konuşuyor. Bu temsillere nasıl güveneceğiz ve insan onayını nereye koyacağız?"

- Valio: *"Ajan, stok ve fiyat iddiasına manuel kontrol olmadan nasıl güvenir?"* → **trust protocol**
- Espoo: *"AI ile yazılmış CV'yi AI okuyor; karar sentetik temsile dayanıyor."* → **aynı problem, insan tarafında**

Her ikisinde de ortak teknik omurga işe yarar:
1. **Yapılandırılmış ihtiyaç/yetenek beyanı** (serbest metin yerine makine-okunur şema — Valio'da ürün/fiyat, Espoo'da ESCO beceri kodu)
2. **Eşleştirme + gerekçe üretimi**
3. **Guardrail: neyin otomatik, neyin insan onaylı olduğu**
4. **Audit trail: insan tarafından okunabilir karar kaydı**

Bir hafta sonunda ikisini birden yapamazsınız, ama **birini seçip diğerinin dilini ödünç almak** güçlü bir konumlandırma: Valio'ya Espoo'nun "insan onayı ve adillik" söylemini taşımak, veya Espoo'ya Valio'nun "doğrulanabilir iddia / audit log" mimarisini taşımak.

## 4. Risk Profili

### Valio Aimo
| Artı | Eksi |
|---|---|
| Hazır mock API → ilk saatten kod yazarsınız | Net brief → **herkes benzer şeyi yapar**, ayrışmak zor |
| Başarı kriteri objektif: akış çalışıyor mu? | Demo çökerse %30 doğrudan gider |
| Büyük ödül | Büyük ödül → **daha çok ve daha güçlü rakip** |
| MCP / x402 gibi standartlar bilgiyle ayrışma alanı | Mock API'nin kalitesi/limitleri sizi bloke edebilir |

**Ayrışma noktası:** Çoğu takım "ürün ara → sipariş ver" happy path'ini yapacak. Fark yaratan: **kısmi stok, ikame sıralaması (iyi-daha iyi-en iyi), harcama eşiği üstü insan onayı akışı ve okunabilir audit log.** Puanlamanın %35'i (otonomi + istisnalar) tam burada.

### Espoo
| Artı | Eksi |
|---|---|
| Daha az rakip (küçük ödül caydırıyor) | Veri yok → her şeyi sıfırdan uydurmanız gerekir |
| Gerçek pilot ve şehir ağına sunum şansı | Jüri sübjektif; "etki" iddiasını kanıtlamak zor |
| Açık uçlu → yaratıcılığa yer var | Açık uçlu → kapsamı kontrol edemeyen takım dağılır |
| Kamu + etik açısı güçlü anlatı sağlar | EU AI Act kısıtı: "AI adayı sıralasın" fikri doğrudan yasak |

**Dikkat:** Espoo'nun kısıtı sandığınızdan ağır — *insan katılımı olmadan bireyler hakkında otomatik profilleme/karar yasak*. "AI aday skorlayıcı" yapan takım kuralı ihlal eder. Çözüm, **kararı değil keşfi ve hazırlığı** iyileştirmeli.

## 5. Hangisini Seçmeli?

**Valio Aimo'yu seçin eğer:**
- Takımda 2+ kişi backend/entegrasyon yazabiliyorsa
- Demo günü çalışan bir sistem göstermek istiyorsanız
- Ödül büyüklüğü önemliyse
- Agentic/MCP/tool-use konusunda deneyiminiz varsa

**Espoo'yu seçin eğer:**
- Takım tasarım/ürün ağırlıklıysa, güçlü bir sunum yapabiliyorsanız
- Hackathon sonrası gerçek bir pilot/ilişki sizin için paradan değerliyse
- Kamu sektörü, etik AI, kapsayıcılık konusu sizi gerçekten ilgilendiriyorsa
- Kazanma olasılığını (daha az rakip) ödül miktarına tercih ediyorsanız

### Benim önerim
Klasör adı ve getirilen kaynaklar (hazır mock API, API anahtarı) göz önüne alındığında **Valio Aimo daha düşük riskli bir bahis**: puanlamanın %60'ı "çalışıyor mu" sorusuna bakıyor ve bu, bir hafta sonunda kontrol edebileceğiniz tek şey. Espoo'da puanın %45'i jürinin "bu gerçekten önemli mi" yargısına bağlı ve bunu etkilemek çok daha zor.

Ancak takımınızda yalnızca 1 kişi sağlam backend yazabiliyorsa, Valio'da yarışacağınız takımlar sizi teknik olarak geçer — o durumda Espoo'nun açık uçluluğu lehinize çalışır.
