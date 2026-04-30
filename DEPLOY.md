# Deploy sempre acceso

Il bot non puo' restare online "a prescindere dal PC" senza un host cloud:
serve un account su un provider, i segreti Telegram e almeno un piccolo worker
sempre acceso.

## Opzione consigliata: Fly.io

Fly.io e' comodo per questo progetto perche' deploya direttamente il Dockerfile
dalla cartella locale e supporta Machines con volume persistente.

1. Installa `flyctl` su Windows:

```powershell
powershell -Command "iwr https://fly.io/install.ps1 -useb | iex"
```

2. Login:

```powershell
fly auth login
```

3. Assicurati che `.env` abbia valori validi. In particolare rigenera il token
   bot con BotFather e aggiorna `TELEGRAM_BOT_TOKEN`.

4. Deploy:

```powershell
cd "C:\Users\matte\Documents\New project"
.\scripts\deploy_fly.ps1 -AppName shoppereaslybot
```

Se il nome app e' gia' preso:

```powershell
.\scripts\deploy_fly.ps1 -AppName shoppereaslybot-matteo
```

5. Vedi i log:

```powershell
fly logs -a shoppereaslybot
```

## Dopo il deploy

Apri la chat privata con il bot su Telegram:

```text
/claim
/destination @tuo_canale_destinazione
/folder https://t.me/addlist/...
```

Oppure manda `/destination_here` nel gruppo/chat in cui vuoi ricevere le offerte.

## Alternative

Render: il file `render.yaml` crea un background worker Docker con disco
persistente. Render documenta pero' che i background worker non sono disponibili
sul piano free e i persistent disk sono per servizi paid.

Railway: il file `railway.toml` imposta Dockerfile, start command e restart
policy. Va aggiunto un volume montato su `/data` e le variabili da dashboard.
Per restart policy `ALWAYS` serve piano paid.
