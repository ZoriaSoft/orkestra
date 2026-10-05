# Orkestra'nın Çalışma Mantığı

Bu doküman orkestra motorunun (Faz 2) tasarımını ve çalışma akışını anlatır.
Kod İngilizce, bu rehber Türkçe'dir.

## Temel fikir

Tek cümle: **muhakeme gerektirmeyen mikro-görevler ucuz modellere (hamal),
her çıktının doğrulaması ve son sentez güçlü modele (kalfa/şef/birleştirici)
gider.** Ucuz model "ne önemli" diye karar vermez; sadece dar bir işi katı
bir şemayla yapar. Doğruluktan kalfa sorumludur.

Neden? Ucuz modellerin zayıf noktası muhakeme, güçlü yanı hacim ve hız.
Güçlü modelin zayıf noktası ise maliyet. Orkestra ikisini öyle bir sıraya
koyar ki ucuz modelin hatası duvara örülmeden yakalanır, güçlü modelin
pahalı muhakemesi yalnız gerektiği yerde (bölme, hakemlik, sentez) harcanır.

## Roller

| Rol | Kim | Girdi → Çıktı | Altın kural |
|---|---|---|---|
| **ŞEF** | `tier: strong` model | Görev → mikro-görev listesi (JSON) | Her parçanın doğruluğu **mekanik kontrol edilebilmeli**. "Araştır" değil, "şu 20 URL'deki fiyatları tabloya dök" yazar. |
| **HAMAL** | `tier: cheap` model havuzu | Mikro-görev → katı şemalı JSON | Muhakeme yasak: emin değilse uydurmaz, `"unknown"`/`null` yazar. Paralel çalışır, parça başına `budget_tokens` kotası olabilir. |
| **KALFA** | Önce kod, sonra strong model | Worker çıktısı → `{pass, reasons[], fix_hint}` | 1. aşama deterministik: JSON mı, şema geçerli mi, alıntı input'ta var mı. 2. aşama (gri alan) strong modele sorulur. Kalan parça en fazla `max_retries` (vars. 2) kez tekrar eder, sonra **güçlü modele yükseltilir**. |
| **BİRLEŞTİRİCİ** | `tier: strong` model | Doğrulanmış parçalar → nihai çıktı | Sadece **geçmiş** parçaları görür. Sentezdeki muhakeme onundur. |

## Akış şeması

```
GÖREV (kullanıcı girdisi)
   │
   ▼
┌──────────────┐   {"pieces": [...]} JSON plan
│  ŞEF         │   (bozuk plan → 1 düzeltme hakkı, sonra EngineError)
│  (strong)    │
└──────┬───────┘
       │  fan-out: her parça ucuz havuzdan bir modele dağıtılır
       ▼        ▼        ▼
   ┌───────┐ ┌───────┐ ┌───────┐
   │ HAMAL │ │ HAMAL │ │ HAMAL │   paralel ThreadPoolExecutor
   │(cheap)│ │(cheap)│ │(cheap)│   dar görev + JSON şema dayatma
   └───┬───┘ └───┬───┘ └───┬───┘
       └─────────┼─────────┘
                 ▼
         ┌──────────────┐   1) deterministik: şema, alıntı (x-from-input), tip
         │  KALFA       │   2) gri alan: strong model hakemliği
         │              │
         └──────┬───────┘
        geçti   │   kaldı → fix_hint ile retry (en fazla max_retries)
          │     │   hâlâ kaldı → strong modele YÜKSELTME (1 deneme)
          │     └─→ (döngü)         o da kalırsa → parça FAILED
          ▼
┌──────────────┐   yalnız "geçti" parçalar + başarısız id listesi
│ BİRLEŞTİRİCİ │
│  (strong)    │
└──────┬───────┘
       ▼
SONUÇ + rapor {status, pieces, usage, errors}
```

## Adım adım: `Orchestra.run(task)`

1. **Model seçimi** (init'te, koşudan önce):
   - Strong model: `--strong` ile verilen veya `tier: strong` kayıtlı ilk
     model (isme göre sıralı). Yoksa `EngineError` — sessiz fallback yok.
   - Hamal havuzu: tüm `tier: cheap` modeller. Boşsa `EngineError`.
   - `--budget` verilmişse havuzdaki **her** modelde `--cost-in/--cost-out`
     ipucu zorunlu; eksikse koşu başlamaz (vana çalışamayacaksa susmayız).
2. **ŞEF**: görevi JSON plana böler (`{"pieces": [...]}`). Plan
   pydantic ile doğrulanır: benzersiz id, `output_schema`'da `type`,
   `max_pieces` üst sınırı, `jsonschema.check_schema` geçerliliği.
   İlk cevap bozuksa hata geri-beslenir, 1 düzeltme hakkı vardır.
3. **HAMAL**: her parça bir cheap modele atanır (round-robin), paralel
   çalışır. Çıktı `json_mode` ile istenir ama yine de kalfa doğrular.
4. **KALFA** her deneme çıktısını denetler:
   - **Aşama 1 (kod)**: JSON ayrıştı mı, `output_schema`'ya uyuyor mu,
     `x-from-input` işaretli alanlar `input` listesinin elemanı mı.
     İhlaller modele sormadan doğrudan `reasons` olur — bedava ve hızlı.
   - **Aşama 2 (hakem)**: deterministik geçemeyen ama yargı isteyen
     `acceptance` kriterleri strong modele sorulur; karar
     `{pass, reasons[], fix_hint}` şemasındadır. `--no-arbitrate` ile
     kapatılırsa kriterler `unchecked_acceptance` olarak rapora yazılır —
     hiçbir şey sessizce "geçmiş" sayılmaz.
5. **Retry + yükseltme**: kalan parça `fix_hint` + violation listesiyle
   hamala geri döner; `max_retries` (varsayılan 2) dolduğunda parça bir
   kez strong modelde çalışır ve yine kalfadan geçmek zorundadır.
   O da kalırsa parça `failed`.
6. **BİRLEŞTİRİCİ**: `passed`/`escalated` parçaların çıktılarını alır,
   başarısız parça id'lerini bilir ve çıktıda bunu dürüstçe belirtir.
   Çıktı JSON ise ayrıştırılır, değilse metin olarak döner.
7. **Rapor**: `run()` her zaman bir dict döner (kurulum hataları hariç —
   onlar `EngineError`/`OrkestraError` olarak fırlatılır):
   `status` ∈ `ok` | `partial` | `failed` | `budget_exceeded`.

## Bütçe vanası ve maliyet bilinci

Her LLM çağrısı `UsageLedger`'a yazılır: rol, model, parça id, deneme no,
prompt/completion token, tahmini USD (modelin `cost` ipuçlarından).
Provider `usage` döndürmezse token'lar ~4 karakter/token ile **tahmin
edilir ve `estimated=True` diye işaretlenir** — rapor dürüst kalır.

İki vana: `--budget` (USD) ve `--token-budget`. Vana her çağrıdan **önce**
kontrol edilir; aşılırsa koşu durur, rapor `budget_exceeded` döner ve o
ana kadarki kullanım/parçalar kaybolmaz.

## Neden ucuz model muhakeme yapmaz?

- Ucuz modelin hata modu "kendinden emin uydurma"dır. Ona muhakeme
  verirsen hatasını denetleyecek başka ucuz model gerekir — sonsuz regres.
- Bunun yerine görev tanımı daralır: tek alan, tek çıktı şeması, tek iş.
- "Bilmiyorum" meşru bir cevaptır (`"unknown"`); uydurmak ise kalfada
  garantili yakalanır (şema + alıntı kontrolleri).
- Gerçekten muhakeme gereken şey üç yerde kalır ve hepsi strong modelde:
  bölmek (şef), gri-alan hakemliği (kalfa aşama 2), sentez (birleştirici).

## Örnek uçtan uca senaryo

```bash
export OPENAI_API_KEY="sk-..."
orkestra providers add openai -u https://api.openai.com/v1 -k OPENAI_API_KEY
orkestra models add brain -p openai -t strong --model-id gpt-5 \
    --cost-in 2.0 --cost-out 8.0
orkestra models add mule  -p openai -t cheap  --model-id gpt-5-mini \
    --purpose micro-task --cost-in 0.10 --cost-out 0.40

orkestra run "Şu üç ürün sayfasındaki fiyatları TL'ye çevirip tablo yap: ..." \
    --budget 0.50 --max-parallel 4
```

Senaryo akışı (örnek: 20 URL fiyat tablosu):

1. Şef görevi 20 parçaya böler: `t-001..t-020`, her biri tek URL +
   `output_schema {fiyat:number, kaynak:string(x-from-input: urls)}`.
2. Hamal havuzu parçaları paylaşır; her parça tek çağrıda JSON döner.
3. Kalfa deterministik denetler: `kaynak` input URL'lerinden biri değilse
   anında reddeder (`citation` ihlali) → retry.
4. `t-007` iki kez kalırsa → strong modele yükseltilir, `escalated`
   olarak geçer.
5. `t-013` her şeye rağmen kalırsa → `failed`; birleştirici çıktıda
   "t-013 eksik" diye belirtir, status `partial`.
6. Rapor: tablo + parça durumları + çağrı başına token/maliyet dökümü.

## Hata sözleşmesi (özet)

| Durum | Davranış |
|---|---|
| Hamal çıktısı bozuk JSON | deneme sayılır, fix_hint ile retry |
| Şema/alıntı ihlali | deterministik kalfa reddi → retry |
| Gri alan ihlali | strong hakem reddi → retry |
| Retry tükendi | strong modele 1 yükseltme denemesi |
| Yükseltme de kaldı | parça `failed`; koşu `partial`/`failed` |
| Şef planı bozuk | 1 düzeltme hakkı → `EngineError` |
| Hakem bozuk karar verir | 1 retry → `EngineError` (altyapı arızası) |
| Bütçe doldu | koşu durur → `status: budget_exceeded` |
| Strong/cheap model yok | başlamadan `EngineError` |

İlke: **sessiz hata yok.** Parça içi hatalar rapora yazılır, koşuyu
imkânsız kılan hatalar exception olarak fırlatılır.
