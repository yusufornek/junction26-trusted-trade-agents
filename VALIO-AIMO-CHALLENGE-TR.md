# Valio Aimo® — Junction 2026 Challenge (Türkçe Çeviri)

## Mücadele

Bir gıda hizmetleri (foodservice) toptancısı, otonom satın alma ajanlarının sipariş vermesine — ürün bulma, teklifleri karşılaştırma ve doğru onayı bugün gerekenin çok küçük bir kısmı kadar sürede alma — gerçek sistemleri, gizli verileri veya pazar yeri (marketplace) yol haritasını ifşa etmeden nasıl izin verebilir?

## Giriş

Valio Aimo®, 20.000'den fazla profesyonel mutfağa hizmet veriyor ve foodservice alıcılarıyla tedarikçileri birbirine bağlayan dijital bir B2B pazar yeri kurmayı planlıyor. Sipariş verme süreci, insanların bir kataloğa tıklamasından AI ajanlarının otonom satın alma kararları vermesine doğru kayarken, bir toptancının kendisini bu ajanlara nasıl açtığı — ajanların neyi görebileceği, neye karar verebileceği ve nelerle güvenilebileceği — veri sahipliği, güven ve müşteri ilişkisinin kime ait olduğu açısından varoluşsal bir soru hâline geliyor.

Dijital B2B Pazar Yeri bu yönde gelecekteki bir adım; Junction 2026 için **pazar yerinin kendisini değil, bugünkü web mağazasını ve arka uç (backend) sistemlerini mockluyoruz**. Sizden, harici bir satın alma ajanının Valio Aimo'nun sistemleriyle etkileşime girmesini sağlayacak arayüzü ve işletim modelini prototiplemenizi istiyoruz — tek bir platformun kara kutusu üzerine değil, açık ve doğrulanabilir standartlar üzerine kurulu olarak.

## Problem

Foodservice ve toptancılıkta B2B sipariş verme hâlâ büyük ölçüde manuel veya menü tabanlı: alıcı bugünkü Valio Aimo web mağazasına giriş yapar ve katalogda arama yapar. Keşfetmek istediğimiz adım, alıcı adına **bulan, karşılaştıran, sipariş veren ve istisnaları yöneten harici satın alma ajanları** ve bunların doğrudan Valio Aimo ile çalışması — bütün bir pazar yeri inşa ederek değil, harici bir ajanın Valio Aimo'nun sistemleriyle etkileşime girebilmesini sağlayacak arayüzü ve işletim modelini tanımlayarak.

### Çekirdek (Core) — çözümünüz şunları mümkün kılmalı

- **Ürün, fiyat ve stok sorgulama:** harici bir ajanın neyin mevcut olduğunu ve hangi fiyata olduğunu araması ve almasını sağlayan yapılandırılmış bir yol.
- **Fiyat ve stok sorgusu, ardından sipariş verme:** sorgudan taahhüde geçişi sağlayan tanımlı bir etkileşim — pazarlıkla belirlenen teklif değil, sabit fiyatlar temelinde.
- **Yetki ve insan onayı yönetimi:** ajanın kendi başına neye karar verebileceği ve neyin imza için bir insana gitmesi gerektiği.

### Opsiyonel — açık yönler (kazanmak için zorunlu değil)

- **Teslimat süresi ve sürdürülebilirlik karşılaştırması:** yalnızca fiyat değil; teslimat güvenilirliği ve web mağazasında zaten gösterilen sertifikalar/sürdürülebilirlik etiketleri. (Kategori düzeyinde karbon ayak izi aralıkları bir "stretch goal".)
- **İkame (substitution) ve diğer istisna yönetimi:** tam olarak istenen karşılanamadığında ne olur. (Geçen yılın Zero-Fail Logistics challenge'ı başarısız teslimatları derinlemesine ele aldı, bu yüzden burada çekirdek olarak puanlanmıyor.)
- **İzlenebilirlik ve açıklanabilirlik:** ajanın ne yaptığına ve neden yaptığına dair insan tarafından okunabilir bir kayıt.

### Örnek istek

> "Pazartesiye kadar 1.000 litre laktozsuz süte ihtiyacım var. Fiyat ve stok durumuna göre gereksinimlerimizi karşılayan bir seçenek bul."

Bir otel zincirinin ajanı bu isteği gönderir. Çözümünüz ürünleri, fiyatları ve stok durumunu yapılandırılmış biçimde döndürür; ajan ya kendi yetkisi dâhilinde siparişi verir ya da onay için bir insana gönderir.

**Etki:** Bu, bugünkü e-ticaret arayüzünü bir chatbot ile değiştirmekle ilgili değil. Bu, bir Avrupalı foodservice toptancısının otonom ajanlar tarafından nasıl güvenli ve açık biçimde okunabilir hâle getirilebileceğini tanımlamakla ilgili — bu arayüz başka birinin platformu tarafından tanımlanmadan önce.

## Valio Aimo®

Valio Aimo®, 2021'de Valio tarafından cesur bir ileri sıçrama olarak kuruldu ve geleneksel toptancılığı yeni nesil bir foodservice gücüne dönüştürdü. Biz bir dağıtımcıdan fazlasıyız: Aimo, toptancılığın genişliği ve çevikliğinin, küresel bir gıda inovatörünün derin ürün uzmanlığı, Ar-Ge kabiliyeti ve marka inşa gücüyle buluştuğu yerdir.

- Restoranlar, foodservice operatörleri ve endüstriyel müşteriler genelinde **20.000 profesyonel müşteri**, işlerini her gün yürütmek için bize güveniyor.
- **700 güvenilir tedarikçiden** tedarik edilen **25.000 ürün adı**, Finlandiya'nın en kapsamlı foodservice ürün yelpazelerinden birini oluşturuyor.
- **350 uzman**, lojistik, hizmet ve dijital dönüşümde mükemmelliği sunmaya adanmış durumda.
- Finlandiya'nın lider süt ve gıda şirketi **Valio'nun bir parçası**:
  - 2,4 milyar € ciro
  - 60 ihracat pazarında faaliyet
  - Öncü bir sürdürülebilirlik misyonu: 2035'e kadar karbon-nötr süt

Valio'nun bir parçası olarak Aimo, foodservice'in AI odaklı çağ için nasıl yeniden icat edilebileceğinin en ileri ucunu temsil ediyor. Valio Aimo® yalnızca bugünün mutfaklarına hizmet etmiyor; foodservice'in geleceğini şekillendiriyor.

## İçgörü (Insight)

### Ele alınmasını görmek istediğimiz problemler

- **Güven protokolü:** Harici bir ajanın, Valio Aimo'nun stok, fiyat ve menşe iddialarına manuel bir kontrol olmadan güvenebilmesi için gereken minimum veri/doğrulama standardı.
- **Guardrail (koruma bariyeri) modeli:** Hangi kararlar insan onayı gerektirir (harcama eşiği, belirtilen tercihlerin dışındaki ikameler), hangileri tamamen otonom olabilir.
- **Yanıt mantığı:** Bir ajanın kısmi stok durumunu ve Valio Aimo'nun kendi ürün yelpazesi içinde ikame sıralamasını ("iyi-daha iyi-en iyi") nasıl ele aldığı.
- **Denetim izi (audit trail):** Hem alıcı hem de Valio Aimo için, ajanın satın alma kararlarının kullanışlı ve insan tarafından okunabilir bir günlüğü.

### Fikir uyandıracak örnek senaryolar

- **İhtiyaç → tarif → sipariş:** Bir şef, mevsimlik bir malzeme etrafında gelecek haftanın menüsünü planlar. Ajan tarifler önerir, stokta neyin eksik olduğunu hesaplar ve siparişi verir.
- **Son dakika etkinliği:** Yarın için beklenmedik bir rezervasyon. Ajan tariflerin neyi gerektirdiğini belirler, zamanında tedarik edilemeyecekleri işaretler ve basit bir SKU değişimi yerine tarif mantığı üzerinden ikameler önerir.
- **Tedarikçi stok tükenmesi:** Kilit bir ürün hafta ortasında tükenir. Ajan, mutfak boşluğu fark etmeden tarifle uyumlu bir ikame önerir ve ödünleşimi (trade-off) açıklar.
- **Proaktif satış ajanı:** Valio Aimo'nun kendi ajanı yeni veya mevsimlik bir ürünü fark eder ve alıcının ajanına proaktif olarak önerir.
- **Baskı altında alerjen-güvenli ikame:** Bir okul veya hastane mutfağında, bir ikame yine de katı alerjen ve beslenme kurallarını karşılamalıdır.
- **Düzenli sipariş kayması tespiti:** Ajan bir mutfağın tekrarlayan haftalık siparişini öğrenir ve otomatik yenilemeden önce anomalileri (porsiyon sayılarının yavaşça artması, maliyetin kayması) işaretler.

**Kapsam notu:** Yapılandırılmış bir veri/API arayüzü mockluyoruz. Valio Aimo'nun üretim arayüzünün nihayetinde geleneksel bir API mi, MCP tarzı bir ajan arayüzü mi, yoksa kendi başına tam bir servis ajanı mı olacağı bu challenge'ın kapsamı dışındadır.

## Kaynaklar

### Bizim getireceklerimiz

- Valio Aimo'nun web mağazası/backend sistemlerini taklit eden **sentetik ürün kataloğu, simüle edilmiş tedarikçiler ve mock API'ler** — gerçekçi veriler üzerine kurulu: fiyatlar, stok davranışı, teslimat tedarik süreleri ve sürdürülebilirlik sertifikaları/etiketleri (kategori düzeyinde karbon ayak izi aralıkları stretch goal olarak).
- **Gerçek zamanlı, canlı veya gizli hiçbir veri ya da sistem yok** — karşısında geliştirdiğiniz her şey sentetiktir.
- AI ajanları, toptancılık ticaret kuralları, istisna yönetimi ve veri yönetişimi konusunda mentorluk; ayrıca hangi arka plan verilerinin ve kurallarının kritik olduğuna dair koçluk.

**Tercih edilen teknoloji:** AI ajanları (agentic/tool-use framework'leri), açık veri/birlikte çalışabilirlik standartları, öneri mantığı, opsiyonel olarak doğrulanabilir kimlik bilgisi (verifiable credential) veya güven katmanı yaklaşımları. Agentic ödeme standartları (ör. x402, Nevermined) ilgili bağlamdır ancak zorunlu değildir.

### Mentorlar

- **Pazar Yeri/Ticari:** Hanna Forsström, Pirjo Kaartinen
- **Online Satış:** Minni Ruhtinas
- **IT/Teknik entegrasyonlar & Veri yönetişimi:** Tommi Nurmi-Aro, Ville Takanen, Riku Tapper

### Erişim

Challenge kaynaklarına buradan erişin: https://junction26.valioaimo.fi/

- Kaynak portalı şifresi: `aimo_ai_challenge`
- Portal; indirilebilir veri setleri, API dokümantasyonu ve mock backend sistemlerine erişim talimatlarını içerir.
- **API kimlik doğrulama — API anahtarı:** `junct_tDnQn4VI_nfhELGCOEkY4vbuxm324MmYmILuokYG03Y`
  (Valio Aimo'nun mock API'lerine yapılan isteklerin kimlik doğrulaması için bu anahtarı kullanın; entegrasyon talimatları için portaldaki dokümantasyona bakın.)
- **Web mağazası test hesapları:** Test kimlik bilgileri ve erişim talimatları, hazır olduğunda ayrıca paylaşılacaktır.

Sağlanan tüm veriler sentetiktir. Hiçbir üretim sistemi veya gizli iş verisi dâhil değildir.

## ŞİRKET BİLGİSİ

**Valio Aimo** — Valio Aimo®, 2021'de Valio tarafından cesur bir ileri sıçrama olarak kuruldu; geleneksel toptancılığı yeni nesil bir foodservice gücüne dönüştürdü. Biz bir dağıtımcıdan fazlasıyız: Aimo, toptancılığın genişliği ve çevikliğinin, küresel bir gıda inovatörünün derin ürün uzmanlığı, Ar-Ge kabiliyeti ve marka inşa gücüyle buluştuğu yerdir. Restoranlar, foodservice operatörleri ve endüstriyel müşteriler genelinde 20.000 profesyonel müşteri her gün işlerini yürütmek için bize güveniyor. 700 güvenilir tedarikçiden 25.000 ürün adı, Finlandiya'nın en kapsamlı foodservice yelpazelerinden birini oluşturuyor. 350 uzman lojistik, hizmet ve dijital dönüşümde mükemmelliğe adanmış durumda. Finlandiya'nın lider süt ve gıda şirketi Valio'nun bir parçası olarak — 2,3 milyar € ciro, 50 ihracat pazarında faaliyet ve öncü bir sürdürülebilirlik misyonu (2035'e kadar karbon-nötr süt) — Aimo, foodservice'in AI odaklı çağ için nasıl yeniden icat edilebileceğinin en ileri ucunu temsil ediyor. Valio Aimo® yalnızca bugünün mutfaklarına hizmet etmiyor; foodservice'in geleceğini şekillendiriyor.

Web sitesi: https://www.valioaimo.fi/

> Not: Metinde ciro ve ihracat pazarı sayıları iki yerde farklı veriliyor (2,4 milyar € / 60 pazar ve 2,3 milyar € / 50 pazar). Orijinaldeki bu tutarsızlık olduğu gibi korunmuştur.

## Değerlendirme Kriterleri

- **Uçtan uca çalışan demo (%30)**
  Çözüm, mock ortamda yalnızca izole parçalar değil, tam bir istek → sorgulama → sipariş/onay akışını baştan sona çalıştırıyor.
- **Problemi ve kullanıcı ihtiyacını çözme (%20)**
  Ajanın, alıcının gerçekte istediği şeyle ne kadar isabetli eşleştiği ve ne kadar az manuel düzeltme gerektirdiği.
- **Otonomi ve kontroller (%20)**
  Guardrail tasarımının kalitesi. Ajanın tek başına neye karar verebildiği, neyin insan onayı gerektirdiği ve kararların nasıl günlüklendiği ve denetlenebilir kılındığı.
- **İstisna durumlarına dayanıklılık (%15)**
  Kısmi stok durumu, ikameler ve teslimat kısıtlarının "happy path" ötesinde ele alınması.
- **Açıklık ve üretime giden inandırıcı yol (%15)**
  Tescilli (proprietary) bir format yerine açık, dokümante edilmiş standartlar; varsayımların net biçimde görünür olması; iyi gerekçelendirilmiş teknik tercihler ve sonraki geliştirme adımlarına dair inandırıcı bir görüş.

## Ödüller

| Sıra | Ödül |
|------|------|
| 1. | 4.000 € |
| 2. | 2.000 € |
| 3. | 1.000 € |
