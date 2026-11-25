# Convertitore di file

Un'app web che gira su docker dove si può trascinare dentro i files e convertirli in un altro formato.

## Requisiti

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (include già Docker Compose), avviato.
- Git, solo se scarichi il progetto da un repository.

## Avvio

```bash
git clone <indirizzo-del-repository>
cd progetto_docker
docker compose up -d --build
```

Poi apri **http://localhost:8080** nel browser, trascina uno o più file,
scegli il formato e premi **Converti**.

**Da VS Code:** con l'estensione Docker, fai clic destro su `docker-compose.yml`
e scegli **Compose Up**. Nel pannello Docker vedi i container in esecuzione.

## Variabili d'ambiente

Non serve configurare niente: senza file `.env` vengono usati i valori di default.
Per cambiarli, copia `.env.example` in `.env` e modifica quello che ti serve:

| Variabile | Default | Cosa fa |
| --- | --- | --- |
| `PORT` | `8080` | porta su cui si apre il sito |
| `MAX_UPLOAD_MB` | `500` | dimensione massima di un file caricato |
| `FILE_TTL_MINUTES` | `60` | dopo quanti minuti i file vengono cancellati |
| `MAX_WORKERS` | `2` | conversioni che possono girare insieme |
| `APP_CPUS` / `APP_MEMORY` | `2` / `2g` | risorse massime del container dell'app |
| `GOTENBERG_MEMORY` | `1g` | memoria massima del container Gotenberg |

Dopo una modifica al `.env`, esegui `docker compose up -d` per applicarla.
Il file `.env`
