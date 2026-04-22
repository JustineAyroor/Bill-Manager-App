# Deployment Guide

This document covers the lightweight Google Cloud VM deployment flow used for this app.

It is aimed at:

- one personal owner account
- a small number of members
- low monthly cost
- simple manual deployment

Current deployment style:

- Google Cloud Compute Engine VM
- Ubuntu
- SQLite database on the VM
- Gradio app started manually inside `tmux`
- static IP
- no reverse proxy yet
- no HTTPS yet

## Current Public URL Pattern

If you do not own a real domain yet, use the VM public IP:

```text
http://YOUR_STATIC_IP:7860
```

If you later buy a domain and wire DNS correctly, you can switch `APP_BASE_URL` to that domain.

## 1. Create the VM

Recommended for this app:

- machine type: `e2-micro`
- OS: Ubuntu
- external IP enabled

You may optionally fill the VM hostname field, but note:

- VM hostname does not create public DNS
- VM hostname does not register a domain
- public domain access still requires real DNS records

## 2. SSH Into the VM

Use either:

- GCP browser SSH
- local terminal SSH

The browser SSH works, but it may disconnect more often.

## 3. Install Git

If Git is missing:

```bash
sudo apt update
sudo apt install -y git
```

## 4. Clone the Repository

Create an app folder:

```bash
mkdir -p ~/apps
cd ~/apps
```

Clone the repo. HTTPS is simplest if SSH keys are not configured:

```bash
git clone https://github.com/JustineAyroor/Bill-Manager-App.git
cd Bill-Manager-App
```

If you use GitHub SSH, make sure your VM has a key with repo access.

## 5. Create the Virtual Environment

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m ensurepip --upgrade
python -m pip install --upgrade pip
python -m pip install -e .
```

If editable install is troublesome in that environment, install dependencies directly:

```bash
python -m pip install aiosmtplib gradio matplotlib openai openpyxl pandas bcrypt plotly pypdf python-dotenv sqlalchemy alembic twilio
```

## 6. Create the `.env` File

In the repo root, create `.env` with the required values.

Important formatting rule:

- use `KEY=value`
- do not use `export KEY=value`

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
OPENROUTER_SITE_URL=http://YOUR_STATIC_IP:7860
OPENROUTER_APP_NAME=tmobile-bill-manager

TWILIO_ACCOUNT_SID=your-twilio-account-sid
TWILIO_AUTH_TOKEN=your-twilio-auth-token
TWILIO_SMS_FROM=+15551234567
TWILIO_WHATSAPP_FROM=whatsapp:+14155238886
TWILIO_STATUS_CALLBACK_URL=

APP_BASE_URL=http://YOUR_STATIC_IP:7860
TM_BILL_BROWSER_STATE_SECRET=change-this-in-deployment
```

## 7. Create the Database

Run:

```bash
python create_db.py
```

This is required on a fresh VM. It creates the base tables and applies migrations.

## 8. Create the First Owner Login

Run:

```bash
python -m app.scripts.create_owner
```

Enter:

- owner email
- owner password

## 9. Optional: Seed Existing Data

If your seed workbook already exists on the VM, import it with:

```bash
PYTHONPATH=. python seed/seed_excel.py data/seed_clean.xlsx
```

Expected output:

```text
Imported allocations
Imported transactions
Seed import complete.
```

## 10. Update the App to Bind Publicly

The app should launch with:

- `server_name=0.0.0.0`
- `server_port=7860`

This repository already includes that behavior in [`app/main.py`](/Users/justineayroor/Downloads/projects/tmobile-bill-manager/app/main.py).

## 11. Start the App Manually

Activate the virtual environment:

```bash
source .venv/bin/activate
```

Start the app:

```bash
python -m app.main
```

If successful, you should see output like:

```text
Running on local URL: http://0.0.0.0:7860
```

## 12. Open Firewall Port 7860

In Google Cloud Console:

1. Open `VPC network`
2. Open `Firewall`
3. Click `Create firewall rule`

Use:

- Name: `allow-gradio-7860`
- Direction: `Ingress`
- Action: `Allow`
- Targets: `All instances in the network`
- Source IPv4 ranges: `0.0.0.0/0`
- Protocols and ports: `tcp:7860`

## 13. Reserve a Static IP

In Google Cloud Console:

1. Go to `IP addresses`
2. Find the VM external IP
3. Click `Promote to static IP`
4. Give it a name such as `billingmanager-ip`

Why this matters:

- ephemeral external IPs can change
- invite and reset links should not break

## 14. Set `APP_BASE_URL`

On the VM, set `APP_BASE_URL` in `.env` to the real public URL users should click.

If you are using the static IP directly:

```env
APP_BASE_URL=http://YOUR_STATIC_IP:7860
```

If you later get a real domain and HTTPS:

```env
APP_BASE_URL=https://your-domain.example.com
```

After changing `.env`, restart the app.

## 15. Keep the App Running With `tmux`

Install `tmux`:

```bash
sudo apt update
sudo apt install -y tmux
```

Create a session:

```bash
tmux new -s billmanager
```

Start the app inside that session:

```bash
cd ~/apps/Bill-Manager-App
source .venv/bin/activate
python -m app.main
```

Detach without stopping the app:

```text
Ctrl+b
d
```

Reattach later:

```bash
tmux attach -t billmanager
```

Stop the app inside `tmux` with:

```text
Ctrl+C
```

## 16. Basic Update Flow

When you push changes from local:

```bash
cd ~/apps/Bill-Manager-App
git pull
source .venv/bin/activate
```

If database changes were added:

```bash
python create_db.py
```

Then restart the app in `tmux`.

## 17. Testing the Deployment

If the VM public IP is `34.42.180.111`, open:

```text
http://34.42.180.111:7860
```

Verify:

- login page loads
- owner login works
- seeded data appears
- invite and reset email links point to the correct URL
- reminders send correctly

## 18. Hostname vs Public DNS

Important:

- setting the VM hostname to something like `billingmanager.jayroor.com` does not create a public domain
- it only sets the server hostname
- users cannot browse to that name unless DNS is created separately

To use a real domain later, you need:

1. a domain you actually own
2. a DNS `A` record pointing to the VM static IP
3. ideally Nginx and HTTPS

## 19. Troubleshooting

### Browser cannot reach the app

Check:

- app is running in `tmux`
- firewall rule allows `tcp:7860`
- the VM external IP is correct
- static IP is still attached

### `no such table: members`

Run:

```bash
python create_db.py
```

### `ModuleNotFoundError: No module named 'gradio'`

Activate the virtual environment first:

```bash
source .venv/bin/activate
```

Then verify:

```bash
python -m pip show gradio
```

### `ModuleNotFoundError: No module named 'app'` during seed import

Run seed import from the repo root:

```bash
cd ~/apps/Bill-Manager-App
PYTHONPATH=. python seed/seed_excel.py data/seed_clean.xlsx
```

### SSH disconnects kill the app

Use `tmux` as described above. Running the app directly in a normal SSH session will stop it when the SSH session dies.

## 20. Recommended Next Hardening Step

This deployment is good for testing and light personal use, but the next improvement should be:

1. create a `systemd` service so the app starts automatically on reboot
2. later add Nginx
3. later add HTTPS

## 21. Security Reminder

If any credentials were pasted into chat, screenshots, terminal logs, or committed by mistake, rotate them:

- SMTP password
- Twilio auth token
- OpenRouter API key
- browser/session secret
