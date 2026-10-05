# Kurulum

`orkestra`, OpenAI-uyumlu LLM sağlayıcılarını ve modellerini tek bir yerel
kayıt defterinde (registry) toplayan bir Python CLI'ıdır. Bu kılavuz Faz 1
kapsamındadır: provider + model yönetimi. Orkestrasyon motoru (şef → hamal →
kalfa → birleştirici) Faz 2'de gelir.

## Gereksinimler

- Python **3.11+**
- pip

## Kurulum

```bash
git clone https://github.com/ZoriaSoft/orkestra.git
cd orkestra
pip install -e .
```

Geliştirme/test bağımlılıklarıyla birlikte:

```bash
pip install -e ".[dev]"
pytest
```

Kurulumu doğrula:

```bash
orkestra --version
orkestra --help
```

## Konfigürasyon nerede durur?

İlk yazımda `~/.orkestra/config.yaml` oluşturulur (dizin `0700`, dosya
`0600` izinleriyle). Konumu görmek için:

```bash
orkestra config path
```

Farklı bir konfigürasyon kökü kullanmak istersen (ör. test veya çoklu
profil), `ORKESTRA_HOME` ortam değişkeni yeterlidir:

```bash
export ORKESTRA_HOME=/path/to/profil
```

## API anahtarları nasıl tutulur?

Anahtar **değeri** asla `config.yaml`'a yazılmaz. Provider kaydında yalnızca
anahtarın yaşadığı **ortam değişkeninin adı** saklanır (`api_key_env`).
Çalışma zamanında değer ortamdan okunur:

```bash
export OPENAI_API_KEY="sk-..."
orkestra providers add openai --base-url https://api.openai.com/v1 --api-key-env OPENAI_API_KEY
```

`providers add` sırasında değişken tanımlı değilse uyarı verilir; kayıt yine
de oluşur (değişkeni sonra export edebilirsin).

## Sonraki adımlar

- Provider ekleme ve test: [provider-ekleme.md](provider-ekleme.md)
- Model kaydı: [model-ekleme.md](model-ekleme.md)
