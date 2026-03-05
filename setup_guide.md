# Setup Guide

## Quick Start (No API Keys Required)

The tool works immediately with **RSS feeds** and **Yahoo Finance** — no registration needed.

```bash
# 1. Activate the virtual environment
venv\Scripts\activate          # Windows
# source venv/bin/activate     # Mac/Linux

# 2. Run a test scrape
python main.py scrape --once

# 3. Launch the dashboard
python main.py dashboard
# Open http://localhost:8050 in your browser
```

---

## Optional: Enable Reddit (Free)

Reddit data significantly improves coverage for retail-driven stocks (e.g. TSLA, NVDA, AMZN).

1. Go to [https://www.reddit.com/prefs/apps](https://www.reddit.com/prefs/apps)
2. Click **"Create App"** at the bottom
3. Fill in:
   - **Name**: `SentimentScraper`
   - **Type**: `script`
   - **Redirect URI**: `http://localhost:8080`
4. Click **Create App**
5. Copy the values:
   - **client_id**: shown under the app name (short string)
   - **client_secret**: the `secret` field

6. Add to your `.env` file:
```
REDDIT_CLIENT_ID=your_client_id_here
REDDIT_CLIENT_SECRET=your_client_secret_here
```

---

## Optional: Enable Claude AI Sentiment (Paid)

Claude provides more nuanced sentiment analysis than VADER, especially for financial nuance.

1. Go to [https://console.anthropic.com](https://console.anthropic.com)
2. Create an account and generate an API key
3. Add to your `.env` file:
```
CLAUDE_API_KEY=sk-ant-...
```

> Note: Claude scoring uses the `claude-haiku-4-5` model (the cheapest). Each article costs roughly $0.0001.

---

## Customizing Your Watchlist

Edit `config/watchlist.yaml` to add or remove stocks:

```yaml
sectors:
  Technology:
    - AAPL
    - MSFT
    - NVDA
  MyCustomSector:
    - PLTR
    - RKLB
```

No code changes needed — the tool picks up the YAML on next run.

---

## Customizing RSS Feeds

Edit `config/rss_feeds.yaml` to add news sources:

```yaml
feeds:
  - name: My Custom Feed
    url: https://example.com/rss.xml
```

---

## CLI Reference

```bash
python main.py setup             # Print this guide in the terminal
python main.py scrape --once     # Single scrape cycle (good for testing)
python main.py scrape            # Continuous scraping (every N minutes)
python main.py dashboard         # Launch dashboard at http://localhost:8050
python main.py dashboard --port 9000  # Custom port
```

---

## Disclaimer

This tool is for **informational and educational purposes only**. Sentiment signals are not financial advice. Always do your own research before making investment decisions.
