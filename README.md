# Bill Manager App

Small multi-user bill management app for tracking shared invoices, payments, allocations, reminders, and member self-service preferences.

This app supports:

- owner and member logins
- invoice allocation tracking
- payment entry and application tracking
- reminder delivery by email, SMS, and WhatsApp
- member invite and password reset emails
- Excel-based seed import for existing data

For VM deployment steps, see [DEPLOYMENT.md](/Users/justineayroor/Downloads/projects/tmobile-bill-manager/DEPLOYMENT.md).

## Tech Stack

- Python 3.12+
- Gradio UI
- SQLAlchemy
- Alembic
- SQLite
- Twilio
- Gmail SMTP or another SMTP provider

## Project Structure

```text
app/
  auth/        Authentication and password flows
  core/        Environment/config loading
  db/          Database setup and models
  scripts/     Helper scripts such as owner creation
  services/    Business logic, reminders, email, Twilio, accounting
  ui/          Gradio screens
seed/
  seed_excel.py            Excel import into the app database
  cleanup_tmobile_excel.py Optional data cleanup helper
data/
  seed_clean.xlsx          Example cleaned seed file
create_db.py               Database bootstrap script
```

## Environment Variables

Create a `.env` file in the project root.

Example:

```env
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your-email@gmail.com
SMTP_PASSWORD=your-app-password
FROM_EMAIL=your-email@gmail.com

OPENROUTER_API_KEY=your-openrouter-key
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_MODEL=openai/gpt-4o-mini
OPENROUTER_SITE_URL=http://localhost:7860
OPENROUTER_APP_NAME=tmobile-bill-manager

TWILIO_ACCOUNT_SID=your-twilio-account-sid
TWILIO_AUTH_TOKEN=your-twilio-auth-token
TWILIO_SMS_FROM=+15551234567
TWILIO_WHATSAPP_FROM=whatsapp:+14155238886
TWILIO_STATUS_CALLBACK_URL=

APP_BASE_URL=http://localhost:7860
TM_BILL_BROWSER_STATE_SECRET=change-this-in-non-local-envs
```

Important:

- Use plain `KEY=value` lines in `.env`
- Do not prefix values with `export`
- Set `APP_BASE_URL` to the actual URL users should click from invite and reset emails
- For local use, `APP_BASE_URL=http://localhost:7860` is fine

## Local Setup

### 1. Create and activate a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install dependencies

```bash
python -m pip install --upgrade pip
python -m pip install -e .
```

If editable install gives packaging trouble in your environment, install from the dependency list instead:

```bash
python -m pip install aiosmtplib gradio matplotlib openai openpyxl pandas bcrypt plotly pypdf python-dotenv sqlalchemy alembic twilio
```

### 3. Create the database

```bash
python create_db.py
```

This does two things:

- creates missing base tables
- applies Alembic migrations

### 4. Create the first owner account

```bash
python -m app.scripts.create_owner
```

You will be prompted for:

- owner email
- owner password

### 5. Optional: seed existing invoice and payment data

If you want to import your prepared Excel workbook:

```bash
PYTHONPATH=. python seed/seed_excel.py data/seed_clean.xlsx
```

Expected output:

```text
Imported allocations
Imported transactions
Seed import complete.
```

Notes:

- Run seeding only after `python create_db.py`
- Seeding is optional if you want to start with a blank app
- The seed importer expects `allocations` and `transactions` sheets when present

### 6. Start the app

```bash
python -m app.main
```

By default the app listens on:

```text
http://0.0.0.0:7860
```

For local usage, open:

```text
http://127.0.0.1:7860
```

## Daily Startup

From the project root:

```bash
source .venv/bin/activate
python -m app.main
```

If the database is already created, you do not need to rerun `create_db.py` each time.

## Reminder Channels

Supported reminder channels:

- email
- SMS
- WhatsApp

Notes for Twilio:

- SMS requires a Twilio number fully configured for your account
- WhatsApp testing usually works through the Twilio sandbox first
- WhatsApp sandbox recipients must join the sandbox before they can receive messages
- reminder logs are stored in the database and visible to both owners and members
- only owners should be allowed to send reminders

## Member and Owner Behavior

- Owners can manage members, invoices, payments, allocations, and reminders
- Members can view their own relevant data and update their preferences and password
- Members should not edit protected ownership/admin fields
- Invoice dropdowns in member view only show invoices relevant to that member

## Password and Invite Flow

- Owners can create or link member logins through the member management flow
- Invite emails point to `APP_BASE_URL`
- Password reset emails point to `APP_BASE_URL`
- If `APP_BASE_URL` is wrong, invite and reset links will be wrong too

## Common Commands

Create database:

```bash
python create_db.py
```

Create owner:

```bash
python -m app.scripts.create_owner
```

Seed data:

```bash
PYTHONPATH=. python seed/seed_excel.py data/seed_clean.xlsx
```

Run app:

```bash
python -m app.main
```

## Troubleshooting

### `sqlite3.OperationalError: no such table: members`

Run:

```bash
python create_db.py
```

### `ModuleNotFoundError: No module named 'app'` while seeding

Run the seed command from the project root:

```bash
cd /path/to/Bill-Manager-App
PYTHONPATH=. python seed/seed_excel.py data/seed_clean.xlsx
```

### `ModuleNotFoundError: No module named 'gradio'`

Make sure the virtual environment is active:

```bash
source .venv/bin/activate
```

Then verify:

```bash
python -m pip show gradio
```

### Invite or reset links point to the wrong address

Update `.env`:

```env
APP_BASE_URL=http://localhost:7860
```

Or use your deployed public URL, then restart the app.

## Security Notes

- Do not commit `.env`
- Rotate any credentials that were accidentally pasted into chat, screenshots, logs, or commits
- Change `TM_BILL_BROWSER_STATE_SECRET` in any shared or deployed environment
- For production-like usage, prefer a reserved static IP or a real domain

## Deployment

For GCP VM setup, firewall, static IP, seeding on the VM, and `tmux` startup flow, see [DEPLOYMENT.md](/Users/justineayroor/Downloads/projects/tmobile-bill-manager/DEPLOYMENT.md).
