# SPX 0DTE Signal Bot — Setup Guide

## What this bot does
- Sends you a pre-market email at 6 AM ET every trading day
- Scans SPX live every 2 minutes during RTH entry windows
- Emails you a signal alert when conditions are met
- Max 3 signals per day, 30-min cooldown between signals

---

## STEP 1 — Gmail App Password

You need a Gmail "App Password" so the bot can send emails.
(This is NOT your regular Gmail password — it's a special 16-character code)

1. Go to: https://myaccount.google.com/security
2. Make sure 2-Step Verification is ON
3. Search for "App passwords" at the top
4. Click App passwords
5. Select "Mail" and "Other (custom name)" → type "SPX Bot"
6. Click Generate
7. Copy the 16-character password shown (e.g. abcd efgh ijkl mnop)
8. Save it — you'll need it in Step 3

---

## STEP 2 — Upload to GitHub

1. Go to https://github.com and sign in (or create a free account)
2. Click "New repository"
3. Name it: spx-bot
4. Make it Private
5. Click "Create repository"
6. Upload these 3 files:
   - bot.py
   - requirements.txt
   - railway.toml

---

## STEP 3 — Deploy on Railway

1. Go to https://railway.app and sign in with GitHub
2. Click "New Project" → "Deploy from GitHub repo"
3. Select your spx-bot repository
4. Once deployed, click on your service → go to "Variables" tab
5. Add these 4 environment variables:

   POLYGON_API_KEY   = 1u0RUGbackck5ayq2Ab05ErcVPDEs5pl
   ALERT_EMAIL       = alain.hanna55@gmail.com
   GMAIL_USER        = alain.hanna55@gmail.com
   GMAIL_PASSWORD    = (your 16-char App Password from Step 1)

6. Click Deploy

---

## STEP 4 — Verify it's working

- Check the Railway logs — you should see:
  "SPX 0DTE Signal Bot"
  "Alerts → alain.hanna55@gmail.com"

- Next trading day at 6 AM ET you'll get your first pre-market brief email
- During RTH entry windows (9:45–11:30 and 1:00–2:30 ET) signals will fire

---

## Signal Logic

The bot uses 5 indicators scored against each other:
- RSI(14) — oversold/overbought
- VWAP — price above/below volume-weighted average
- EMA 9/21 cross — short-term trend direction
- Momentum — last candle direction
- Price vs EMA9 — confirmation

Confidence = how strongly the indicators align (min 65% to fire)

---

## What you'll receive

Subject: SPX SIGNAL 🔴 PUT — 10:23 AM ET

SPX SIGNAL 🔴 PUT
━━━━━━━━━━━━━━━━━━━━━━━━
Time:       10:23 AM ET
Spot:       5,842.00
Strike:     5830 PUT 0DTE
Confidence: 71%
Target:     +45% of premium
Stop:       -50% of premium
Invalidate: 5,855.00
━━━━━━━━━━━━━━━━━━━━━━━━
RSI:   68.4
VWAP:  5,851.20
EMA9:  5,848.30
EMA21: 5,852.10

---

## Cost summary
- Massive/Polygon Indices Advanced: $99/mo
- Railway hosting: Free tier (enough for this bot)
- Gmail: Free
- Total: $99/mo
