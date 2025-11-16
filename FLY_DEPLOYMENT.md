# Fly.io Deployment Guide

## Prerequisites
- Fly.io account (sign up at https://fly.io)
- Fly CLI installed

## Step 1: Install Fly CLI

### Mac/Linux:
```bash
curl -L https://fly.io/install.sh | sh
```

### Windows (PowerShell):
```powershell
pwsh -Command "iwr https://fly.io/install.ps1 -useb | iex"
```

## Step 2: Login to Fly.io
```bash
fly auth login
```

## Step 3: Set Secrets (Environment Variables)

These are your sensitive credentials that shouldn't be in code:

```bash
fly secrets set OPENAI_API_KEY="sk-proj-..."
fly secrets set ACCOUNT_SID="AC..."
fly secrets set AUTH_TOKEN="..."
fly secrets set PHONE_NUMBER="+1..."
fly secrets set SUPABASE_URL="https://..."
fly secrets set SUPABASE_PUBLISHED_KEY="eyJ..."
fly secrets set API_BASE_URL="https://helloml-backend.fly.dev"
```

**Important:** Update `API_BASE_URL` after you know your Fly.io app URL!

## Step 4: Launch the App

```bash
cd /home/noah-gallego/Desktop/HelloML
fly launch
```

When prompted:
- **App name:** `helloml-backend` (or your preferred name)
- **Region:** `iad` (Ashburn, VA - closest to Twilio)
- **Would you like to set up a Postgresql database?** No
- **Would you like to set up an Upstash Redis database?** No
- **Would you like to deploy now?** Yes

## Step 5: Get Your App URL

After deployment:
```bash
fly status
```

Your app will be at: `https://helloml-backend.fly.dev`

## Step 6: Update Twilio Webhook

Update your Twilio phone number webhook to the new URL:

**Script (update_webhook_to_fly.py):**
```python
from twilio.rest import Client
import os
from dotenv import load_dotenv

load_dotenv()

client = Client(os.getenv("ACCOUNT_SID"), os.getenv("AUTH_TOKEN"))

phone_number = "+16614603917"
agent_id = 5

# New Fly.io webhook URL
new_webhook = f"https://helloml-backend.fly.dev/conversation/{agent_id}/voice"

numbers = client.incoming_phone_numbers.list(phone_number=phone_number)
if numbers:
    numbers[0].update(voice_url=new_webhook, voice_method='POST')
    print(f"✅ Updated to {new_webhook}")
```

Or update manually in Twilio Console.

## Step 7: Test!

Call your phone number: **+16614603917**

Should hear:
- Agent greeting
- High-quality OpenAI voice (shimmer)
- Low latency responses
- Full conversation with WebSocket streaming

## Monitoring

**View logs:**
```bash
fly logs
```

**Check status:**
```bash
fly status
```

**SSH into machine:**
```bash
fly ssh console
```

## Scaling

**Add more instances:**
```bash
fly scale count 2
```

**Change machine size:**
```bash
fly scale vm shared-cpu-2x
```

## Costs

**Fly.io pricing (as of 2025):**
- **Shared CPU (256MB RAM):** ~$2/month (always-on)
- **Additional instances:** ~$2/month each
- **Bandwidth:** First 100GB free

**Much cheaper than Vercel for WebSocket workloads!**

## Troubleshooting

**Deploy fails:**
```bash
fly deploy --verbose
```

**App won't start:**
```bash
fly logs
```

**WebSocket still failing:**
- Check firewall allows TCP 443
- Verify `auto_stop_machines = false` in fly.toml
- Check logs for connection errors

## Custom Domain (Optional)

To use `api.helloml.app` instead of `.fly.dev`:

```bash
fly certs add api.helloml.app
```

Then add DNS records as instructed by Fly.

## Rollback

If needed:
```bash
fly releases
fly deploy --image flyio/helloml-backend:v1
```

---

**That's it!** Your backend now supports WebSockets and will work with Twilio Media Streams + OpenAI Realtime API.
