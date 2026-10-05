# Provider Ekleme

`orkestra providers` komut grubu, OpenAI-uyumlu uç noktaları (endpoint)
yönetir: OpenAI, yerel ollama, kendi proxy'n — `/v1/models` ve
`/v1/chat/completions` konuşan her servis.

## Ekleme

```bash
orkestra providers add <ad> --base-url <url> [--api-key-env <ENV_ADI>] \
    [--header 'Ad=Değer'] [--timeout <saniye>]
```

| Parametre | Zorunlu | Açıklama |
|---|---|---|
| `ad` | evet | Benzersiz kayıt adı (küçük harf slug: `a-z0-9._-`) |
| `--base-url` | evet | Uç nokta kökü. `/v1` soneki olsa da olmasa da olur |
| `--api-key-env` | hayır | API anahtarını tutan ortam değişkeninin **adı**. Yerel/auth'suz servislerde boş bırak |
| `--header` | hayır | Ek HTTP başlığı, tekrarlanabilir: `-H 'X-Team=orkestra'` |
| `--timeout` | hayır | İstek zaman aşımı, saniye (varsayılan 15, üst sınır 600) |

Örnekler:

```bash
# OpenAI
export OPENAI_API_KEY="sk-..."
orkestra providers add openai --base-url https://api.openai.com/v1 --api-key-env OPENAI_API_KEY

# Kimlik doğrulamasız yerel servis
orkestra providers add ollama --base-url http://localhost:11434

# Özel başlık + kısa timeout
orkestra providers add proxy --base-url https://proxy.example.com \
    --api-key-env PROXY_KEY -H 'X-Team=arastirma' --timeout 30
```

> **Gizlilik:** `config.yaml`'da yalnızca ortam değişkeninin adı saklanır —
> anahtarın kendisi asla dosyaya yazılmaz. `--header` değerleri ise düz metin
> olarak saklanır; başlığa gizli değer koyma.

## Listeleme

```bash
orkestra providers list
```

Kayıtlı provider'ları tablo hâlinde gösterir: ad, base URL, env var adı,
başlık/timeout ve bağlı model sayısı.

## Bağlantı testi

```bash
orkestra providers test <ad>          # ilk 20 model id gösterilir
orkestra providers test <ad> --all    # tamamını listele
```

`GET {base_url}/v1/models` çağrısı yapar; gecikme (ms), erişilebilir model
sayısı ve model id'lerini raporlar. Uç nokta kapalıysa, HTTP hata döndürürse
ya da beklenmedik yanıt verirse çıkış kodu **1** olur (script'lerde
kullanılabilir).

## Kaldırma

```bash
orkestra providers remove <ad>
```

Provider'a bağlı model varken kaldırma **reddedilir** — önce modelleri
`orkestra models remove` ile temizle. Bu, yanlışlıkla model kayıtlarını
sahipsiz bırakmayı önler.

## Sorun giderme

| Belirti | Çözüm |
|---|---|
| `env var ... is not set` | `export <ENV_ADI>=...` yap; `providers add` eksik değişkene karşı uyarır |
| `ConnectError` | URL/Port ve servisin ayakta olduğunu kontrol et (`curl {base_url}/models` ya da `/v1/models`) |
| `HTTP 401` | Env var'daki anahtar değeri yanlış veya eskimiş |
| `unexpected JSON shape` | Uç nokta OpenAI-uyumlu değil; `/models` yanıtı `{"data": [...]}` döndürmeli |
