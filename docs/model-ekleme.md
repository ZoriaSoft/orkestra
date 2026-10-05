# Model Ekleme

`orkestra models`, sağlayıcıların üzerindeki tekil modelleri kaydeder ve
orkestra katmanları için etiketler. Model ekleyebilmek için provider'ın önce
kayıtlı olması gerekir (bkz. [provider-ekleme.md](provider-ekleme.md)).

## Ekleme

```bash
orkestra models add <ad> --provider <provider> --tier <strong|cheap> \
    [--purpose <amaç>]... [--model-id <uzak-id>] \
    [--cost-in <usd>] [--cost-out <usd>]
```

| Parametre | Zorunlu | Açıklama |
|---|---|---|
| `ad` | evet | Benzersiz kayıt adı (slug). API'ye gönderilen id için de varsayılandır |
| `--provider` | evet | `orkestra providers list`'teki bir ad |
| `--tier` | evet | `strong` veya `cheap` (aşağıya bak) |
| `--purpose` | hayır | `chat`, `code`, `micro-task`; tekrarlanabilir |
| `--model-id` | hayır | Uzak taraftaki model id'si registry adından farklıysa |
| `--cost-in` / `--cost-out` | hayır | 1M token başına USD (girdi/çıktı) — bütçe vanası ipucu |

### `tier` ne işe yarar?

Orkestra tasarımında (Faz 2 motoru) modeller iki katmana ayrılır:

- **`strong`** — muhakeme katmanı: şef (görev bölme), kalfa'nın gri-alan
  hakemliği, birleştirici (sentez).
- **`cheap`** — hamal havuzu: dar, şemalı, muhakeme gerektirmeyen mikro-görevler.

`purpose` etiketleri modelin ne için uygun olduğunu bildirir
(`micro-task` = hamal işi, `code` = kod görevleri, `chat` = genel diyalog).

### `model_id` ne zaman gerekir?

Uzak servis id'si slug kurallarına uymuyorsa ya da yerel bir kısa ad
kullanmak istiyorsan:

```bash
# registry adı: hamal-hizli   API'ye giden id: provider-fast-9b
orkestra models add hamal-hizli -p proxy -t cheap --purpose micro-task \
    --model-id 'provider-fast-9b'
```

## Listeleme ve filtreleme

```bash
orkestra models list                        # hepsi
orkestra models list --tier cheap           # yalnız hamal havuzu
orkestra models list --purpose code         # kod amaçlılar
orkestra models list --provider ollama      # bir provider'ın modelleri
```

Tablo; ad, provider, uzak id, tier, amaçlar ve maliyet ipucunu gösterir.

## Kaldırma

```bash
orkestra models remove <ad>
```

## Örnek akış (uçtan uca)

```bash
export PROXY_KEY="..."
orkestra providers add proxy --base-url http://localhost:8080 --api-key-env PROXY_KEY
orkestra providers test proxy                      # hangi id'ler erişilebilir?

orkestra models add sef-ana   -p proxy -t strong --purpose chat --purpose code \
    --model-id 'glm-5.3' --cost-in 2.0 --cost-out 6.0
orkestra models add hamal-ufak -p proxy -t cheap --purpose micro-task \
    --model-id 'nova-mini' --cost-in 0.05 --cost-out 0.15
orkestra models list --tier cheap
```
