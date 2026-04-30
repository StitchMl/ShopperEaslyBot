# Shopper Easly Bot

Servizio Telegram sempre acceso per aggregare messaggi da canali, bot o chat
shopping e ripubblicarli in un unico canale/gruppo.

Il vecchio script usava il Bot API. Quel modello non puo' leggere canali a cui
tu sei iscritto, ne' unirsi da solo tramite link: un bot Telegram riceve solo
gli update dei posti in cui e' stato aggiunto. Per questo il servizio usa
Telethon, cioe' il client Telegram dell'account utente, e puo' leggere le chat
che il tuo account vede gia'.

## Cosa fa

- ascolta i canali/chat configurati in `SOURCE_CHATS`;
- manda i messaggi aggregati in `DESTINATION_CHAT`;
- evita doppioni usando URL canonici e testo normalizzato;
- copia anche i media quando possibile;
- salva lo stato in SQLite, quindi puo' riavviarsi senza rimandare tutto;
- gira come worker Docker, adatto a un VPS o a un host cloud con processi
  always-on.
- puo' essere controllato da Telegram: mandi al bot un link cartella
  `https://t.me/addlist/...` e il servizio importa le sorgenti.

## Preparazione Telegram

1. Crea o scegli il canale/gruppo di destinazione.
2. Se vuoi che pubblichi un bot, crea un bot con BotFather e aggiungilo come
   admin o membro al canale/gruppo di destinazione.
3. Crea `TELEGRAM_API_ID` e `TELEGRAM_API_HASH` da `my.telegram.org`.
4. Genera una sessione utente locale:

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
python scripts\create_session.py
```

Oppure usa il setup guidato, che crea `.env` e genera `TELEGRAM_SESSION`:

```powershell
.\.venv\Scripts\python scripts\setup_env.py
```

Metti la stringa `TELEGRAM_SESSION=...` tra i segreti del servizio cloud.
Non committarla e non inviarla in chat.

Per trovare ID e username delle chat disponibili:

```powershell
python scripts\list_chats.py
```

## Configurazione minima

```env
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=...
TELEGRAM_SESSION=...
SOURCE_CHATS=@canale1,@canale2,-1001234567890
DESTINATION_CHAT=@mio_canale_offerte
TELEGRAM_BOT_TOKEN=
ADMIN_USER_IDS=
DATABASE_PATH=/data/shopperbot.sqlite3
```

Se `TELEGRAM_BOT_TOKEN` e' vuoto, i messaggi vengono inviati dal tuo account.
Se lo imposti, vengono inviati dal bot e puoi controllarlo via comandi Telegram.
In locale puoi usare `DATABASE_PATH=data/shopperbot.sqlite3`; nel cloud usa un
volume persistente, per esempio `/data/shopperbot.sqlite3`.

## Uso come bot con cartella Telegram

1. Avvia il servizio con `TELEGRAM_BOT_TOKEN` impostato.
2. Apri una chat privata con il bot.
3. Manda `/claim` per diventare admin la prima volta.
4. Imposta la destinazione:

```text
/destination @mio_canale_offerte
```

Oppure manda `/destination_here` nel gruppo/chat in cui vuoi ricevere le
offerte.

5. Da Telegram crea o apri la cartella con tutti i canali offerte, condividila
   e copia il link `https://t.me/addlist/...`.
6. Mandalo al bot:

```text
/folder https://t.me/addlist/...
```

Da quel momento i nuovi messaggi delle sorgenti importate vengono aggregati
nella destinazione. Il link cartella viene importato dal tuo account utente
Telethon: Telegram espone l'import delle cartelle come metodo utilizzabile
dagli utenti, non dal Bot API puro.

Se una chat o un bot non entra dalla cartella, aggiungilo manualmente:

```text
/add @nomecanale_o_bot
```

Se non conosci username o ID, cerca tra i dialoghi del tuo account:

```text
/find junction
```

Il bot risponde con righe gia' pronte tipo `/add 123456789`.

## Avvio locale

```powershell
python -m shopper_merge_bot
```

Oppure con Docker:

```powershell
docker compose up --build
```

## Deploy online

Usa un servizio cloud che supporti worker Docker sempre accesi e volumi
persistenti. Imposta le variabili di `.env.example` come secret del provider e
monta un volume persistente su `/data`, cosi' SQLite mantiene la deduplica dopo
i riavvii.

Percorso pronto: [DEPLOY.md](DEPLOY.md) contiene deploy Fly.io, Render e
Railway. Comando container:

```bash
python -m shopper_merge_bot
```

Health check HTTP non necessario: e' un worker Telegram, non un sito web.

## Note di sicurezza

- Rigenera con BotFather ogni token che e' stato salvato in chiaro nel vecchio
  script.
- `TELEGRAM_SESSION` equivale a una sessione login del tuo account: trattala
  come una password.
- Evita `MONITOR_ALL_CHATS=true` se non vuoi inoltrare accidentalmente chat non
  shopping.
