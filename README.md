# orkestra

Open-source LLM provider/model registry — and the foundation for a layered
agent orchestra: a strong model decomposes (**sef**), cheap models execute
micro-tasks in parallel (**hamal**), a validator gates every output
(**kalfa**), a strong model synthesizes (**birlestirici**).

**Phase 1 (this release):** provider + model registries and the `orkestra`
CLI. The orchestration engine lands in Phase 2 — the registry is already
designed for it (`tier: strong|cheap`, `purpose` tags, cost hints, and a
`resolve()` seam that hands the engine endpoint + model id + API key).

## Install

```bash
git clone https://github.com/ZoriaSoft/orkestra.git
cd orkestra
pip install -e .
```

Python 3.11+. Dev extras: `pip install -e ".[dev]"` then `pytest`.

## 5-minute quickstart

```bash
# 1. Register a provider (any OpenAI-compatible endpoint)
export OPENAI_API_KEY="sk-..."
orkestra providers add openai --base-url https://api.openai.com/v1 --api-key-env OPENAI_API_KEY

# 2. Probe it: GET /v1/models -> latency + reachable model ids
orkestra providers test openai

# 3. Register models on it
orkestra models add planner -p openai -t strong --purpose chat --purpose code --model-id gpt-5 --cost-in 2.0 --cost-out 8.0
orkestra models add worker  -p openai -t cheap  --purpose micro-task --model-id gpt-5-mini --cost-in 0.10 --cost-out 0.40

# 4. Inspect
orkestra models list --tier cheap
orkestra config show
```

Config lives in `~/.orkestra/config.yaml` (created on first write, mode
`0600`; override the home dir with `ORKESTRA_HOME`). **API keys are never
stored** — only the *name* of the env var holding them.

## Commands

| Command | What it does |
|---|---|
| `orkestra providers add NAME -u URL [-k ENV] [-H 'K=V'] [-t SEC]` | register an OpenAI-compatible endpoint |
| `orkestra providers list` | table of providers (+ model count) |
| `orkestra providers test NAME [--all]` | probe `/v1/models`: latency + reachable models |
| `orkestra providers remove NAME` | remove (refused while models use it) |
| `orkestra models add NAME -p PROVIDER -t strong\|cheap [--purpose P]... [--model-id ID] [--cost-in X] [--cost-out Y]` | register a model |
| `orkestra models list [--tier T] [--purpose P] [--provider P]` | filtered model table |
| `orkestra models remove NAME` | remove a model |
| `orkestra config path` / `orkestra config show` | where/how the config is stored |

## Layout

```
src/orkestra/
  schema.py     # pydantic: ProviderConfig, ModelConfig, OrkestraConfig, Tier, Purpose
  config.py     # ConfigStore — atomic YAML load/save, ORKESTRA_HOME
  registry.py   # Registry — CRUD + resolve() (the Phase-2 engine seam)
  client.py     # OpenAI-compatible probe (GET /v1/models)
  errors.py     # explicit exception hierarchy
  cli/          # typer commands: providers, models, config
tests/          # unit + integration tests incl. a real mock OpenAI server
docs/           # Turkish guides: kurulum, provider-ekleme, model-ekleme
```

Errors are explicit (typed exceptions → red message + non-zero exit); the
test suite ships a real local mock server — no flaky network dependencies.

---

## orkestra (Türkçe)

Açık kaynak LLM sağlayıcı/model kayıt sistemi — ve katmanlı bir ajan
orkestrasının temeli: güçlü model böler (**şef**), ucuz modeller mikro-görevleri
paralel yürütür (**hamal**), doğrulayıcı her çıktıyı denetler (**kalfa**),
güçlü model sentezler (**birleştirici**).

**Faz 1 (bu sürüm):** provider + model registry'leri ve `orkestra` CLI'ı.
Orkestrasyon motoru Faz 2'de gelir — registry şimdiden ona göre tasarlandı.

### Kurulum

```bash
pip install -e .
```

Python 3.11+. Geliştirme için: `pip install -e ".[dev]"` ardından `pytest`.

### Hızlı başlangıç

```bash
export OPENAI_API_KEY="sk-..."
orkestra providers add openai --base-url https://api.openai.com/v1 --api-key-env OPENAI_API_KEY
orkestra providers test openai
orkestra models add hamal -p openai -t cheap --purpose micro-task --model-id gpt-5-mini
orkestra models list --tier cheap
```

Konfigürasyon `~/.orkestra/config.yaml`'da durur. **API anahtarları asla
dosyaya yazılmaz** — yalnızca anahtarı tutan ortam değişkeninin adı saklanır.

Ayrıntılı kılavuzlar: [docs/kurulum.md](docs/kurulum.md) ·
[docs/provider-ekleme.md](docs/provider-ekleme.md) ·
[docs/model-ekleme.md](docs/model-ekleme.md)
