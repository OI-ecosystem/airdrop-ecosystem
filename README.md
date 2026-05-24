# Sentinel & Hunter Intelligence — Landing Pages

Two product landing pages, ready to deploy on GitHub Pages in under 10 minutes.

## What's in here

```
index.html                          ← Root page linking both products
hunter-intelligence/
  index.html                        ← Hunter Intelligence landing page (live DefiLlama TVL)
sentinel-engine/
  index.html                        ← Sentinel Engine landing page
```

---

## How to put this live on GitHub Pages

### Step 1 — Create a GitHub repository

1. Go to github.com and sign in
2. Click the **+** button top right → **New repository**
3. Name it something like `sentinel-pages` or `airdrop-intelligence`
4. Set it to **Public** (required for free GitHub Pages)
5. Click **Create repository**

### Step 2 — Upload the files

**Option A — Drag and drop (easiest)**
1. Open your new repository
2. Drag the entire contents of this folder into the browser window
3. GitHub will ask you to commit — click **Commit changes**

**Option B — Git command line**
```bash
cd sentinel-pages
git init
git add .
git commit -m "Initial launch"
git branch -M main
git remote add origin https://github.com/YOURUSERNAME/sentinel-pages.git
git push -u origin main
```

### Step 3 — Enable GitHub Pages

1. In your repository, click **Settings**
2. Scroll down to **Pages** in the left sidebar
3. Under **Source**, select **Deploy from a branch**
4. Select **main** branch, **/ (root)** folder
5. Click **Save**

### Step 4 — Your site is live

After 2-3 minutes your pages will be live at:
```
https://YOURUSERNAME.github.io/sentinel-pages/
https://YOURUSERNAME.github.io/sentinel-pages/hunter-intelligence/
https://YOURUSERNAME.github.io/sentinel-pages/sentinel-engine/
```

---

## Custom domain (optional, when ready)

Buy a domain (Namecheap, Google Domains — around £10-15/year).

1. In GitHub Pages settings, enter your custom domain
2. At your domain registrar, add a CNAME record pointing to `YOURUSERNAME.github.io`
3. Takes 10-30 minutes to propagate

Suggested domains:
- `hunterintelligence.io`
- `sentinelairdrop.io`
- `hunterprotocol.io`

---

## Live TVL data — how it works

The Hunter landing page automatically pulls live TVL data from DefiLlama's free public API when someone visits the page. No API key needed. It fetches data for:

- Base L2
- Morpho
- Polymarket
- Aerodrome
- Hyperliquid

The fetch happens in the visitor's browser — no server needed, no API costs.

If DefiLlama is slow or unavailable, the page shows the cached scores gracefully.

---

## When you're ready to take payments

At the point you want Stripe subscriptions and a real subscriber database, migrate to Vercel (you already have an account). The HTML pages can be dropped straight into a Next.js project with minimal changes.

Everything else — Telegram delivery, the Hunter scoring engine, the subscriber API — is already built in the Sentinel codebase.

---

## Updating the protocol registry

The protocol list and scores are in the `<script>` section of `hunter-intelligence/index.html`. To update a score or add a new protocol, find the `ALL_PROTOCOLS` array and edit directly. Commit and push — the live page updates in seconds.

---

## Files to keep private (never commit these)

- Any `.env` files
- Private keys
- API keys
- The Sentinel Python codebase

These landing pages contain zero sensitive information — safe to put on a public GitHub repository.
