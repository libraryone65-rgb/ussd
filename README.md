# USSD Voting Service (Arkesel USSD + Paystack)

Minimal Flask-based USSD voting backend that uses the provided `nominees`, `votes`, and `vote_sessions` tables and initializes payments via Paystack.

Setup
- Create the MySQL schema using your `voting_nominees.sql` file (the triggers/views are included there).
- Create a `.env` file with the following variables:

```
DATABASE_URL=mysql+pymysql://USER:PASS@HOST/DBNAME
PAYSTACK_SECRET_KEY=sk_test_xxx
PRICE_PER_VOTE_NGN=100
PAYSTACK_CALLBACK_URL=https://your-server/paystack/callback
PORT=5000
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Run locally:

```bash
python ussd.py
```

Endpoints
- `POST /ussd` — main Arkesel USSD callback. Accepts common USSD fields (`sessionId`, `phoneNumber`, `text`) and returns plain-text responses prefixed with `CON ` or `END `.
- `POST /paystack/webhook` — Paystack webhook endpoint (update vote status to `completed` when payment succeeds).

Notes
- This is a minimal example. In production: verify webhook signatures, secure callbacks, handle concurrency, implement retries, and adapt the USSD fields exactly to the Arkesel payload schema.
