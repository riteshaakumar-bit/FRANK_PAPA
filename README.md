# ARU BOT — Python fixed package

## Included files
- `aru_bot.py` — complete Python source based on the uploaded file, with fixes applied.
- `requirements.txt` — required Python packages.
- `.env.example` — environment-variable template.
- `Procfile` — generic web start command.
- `render.yaml` — Render deployment blueprint.
- `.gitignore` — keeps secrets and runtime data out of source control.

## Install
Use Python 3.10 or newer.

```bash
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows PowerShell:
# .venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

Copy `.env.example` to `.env` and set your real values in your host's environment panel.
The bot reads environment variables directly; it does not automatically load `.env`.

Run:

```bash
python aru_bot.py
```

## Deployment
- `WEBHOOK_URL` blank: long-polling mode; run only one instance.
- Webhook mode: set `WEBHOOK_URL` to your public HTTPS endpoint, normally
  `https://YOUR-DOMAIN/webhook`.
- Keep `APP_KEY` identical across deployments. Changing it breaks decryption of existing
  clone tokens.
- Use persistent storage for `BOT_DATA_DIR` if your host has an ephemeral filesystem.
- Firebase backup needs a valid `FIREBASE_URL` and suitable database access rules/auth.
- AI replies need a valid Groq key/model. Music search needs the configured external API.
- Group moderation needs the bot to have the relevant Telegram admin permissions.
- `/record` and `/stoprecord` signal an external recording worker; Telegram Bot API alone
  cannot record Voice Chat audio.

## Fixes included
- Safer numeric environment parsing and bounds.
- Firebase worker exception protection and synchronous fallback if its queue is full.
- Short negative `getMe` cache to reduce repeated Telegram requests during outages.
- Caption text is checked by the language filter too.
- Unknown callback buttons are answered.
- Main webhook accepts updates only after successful webhook setup and valid Telegram secret.
- Webhook setup failure falls back to polling.
- Failed update submission is not acknowledged as successful.
- Clone routing records are persisted before enabling a clone webhook, with rollback on failure.
- Admin-only `/tag`, `/tagmember`, and `/tagall`; `/tagall` is limited to 25 tracked users.
  Telegram Bot API does not provide a full group-member enumeration method.

## Source limitation
The uploaded file was already Python (`aru_bot.py`). No original PHP file was attached, so this
package repairs and preserves the features present in the uploaded Python source; exact parity
with an unavailable PHP source cannot be verified.
