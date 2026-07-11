"""Seed script: appends demo client tickets themed around the Intelply product.

Run with: python seed_clients.py, then python run_pipeline.py so the crew
processes them for real (classification, drafts, review, memory, traces).

Unlike seed_data.py this script is additive: it skips tickets that already
exist (same customer and subject) instead of skipping the whole seed, so it
is safe to run against a database that already holds live conversations.
"""

from db import get_connection

CLIENT_TICKETS = [
    # --- Article generation (recurring bug across customers) ---
    (
        "eva.martins@copyloft.test",
        "Article generation stuck at 90%",
        "Every article I start today gets stuck at 90% on the progress bar "
        "and then shows 'generation failed'. The draft it leaves behind is "
        "blank. I have a client piece due tomorrow morning, please help.",
    ),
    (
        "content@nordicseo.test",
        "Generated article ignores my outline",
        "The generator produces a decent article but it completely ignores "
        "the outline I attach. Sections come out in a different order and "
        "two H2s I wrote are missing entirely. It worked correctly last "
        "week, so something is broken in how outlines are read.",
    ),
    # --- Sign-up and login (recurring bug across customers) ---
    (
        "ops@studiofern.test",
        "Can't log in after password reset",
        "Since yesterday I get 'invalid credentials' even after resetting "
        "my password twice. The reset email arrives, I set a new password, "
        "and login still fails on both Chrome and Safari.",
    ),
    (
        "hello@petitmarche.test",
        "Sign up with Google button is broken",
        "On the sign up page the 'Continue with Google' button opens a "
        "popup that closes immediately and nothing happens. I had to "
        "register with email instead. Reporting it because my colleague "
        "hit the same thing on her laptop.",
    ),
    # --- Integrations ---
    (
        "it@crestfield.test",
        "WordPress webhook sync failed",
        "Our WordPress integration stopped publishing drafts three days "
        "ago. I reconnected and reauthorized the plugin but every push "
        "since then has failed silently. The integration status page still "
        "says 'connected', which is misleading.",
    ),
    # --- Export ---
    (
        "anna@inkline.test",
        "Export to PDF missing images",
        "When I export an article to PDF, every inline image is missing "
        "from the file, just empty gaps where they should be. The web "
        "version renders fine. Export to Word has the same problem.",
    ),
    # --- Billing ---
    (
        "maya.laurent@atelier-sites.test",
        "Charged twice this month",
        "My bank statement shows two 49 EUR charges from you on July 3rd "
        "for the Growth plan. I only have one workspace and expected a "
        "single charge. Please correct the billing.",
    ),
    (
        "finance@brightlane.test",
        "Invoice doesn't include VAT details",
        "Our accounting team needs proper VAT invoices. The receipt email "
        "doesn't include our company name, address, or a VAT number field. "
        "Is there an option I'm missing, or can you add this?",
    ),
    (
        "growth@saastack.test",
        "Paying for Pro but still on Starter limits",
        "We upgraded to Pro last Monday and were charged for it, but the "
        "workspace still enforces the Starter generation limit. The plan "
        "page says Pro while the editor says we've hit the Starter cap. "
        "Those two can't both be right.",
    ),
    # --- Refunds ---
    (
        "jonas.keller@freelance.test",
        "Duplicate purchase, need one refunded",
        "I accidentally bought the same credit pack twice within a minute, "
        "almost certainly a double click at checkout. Can you refund one "
        "of the two charges? Happy to provide the receipts.",
    ),
    (
        "sam@quietowl.test",
        "Refund for unused annual plan",
        "I subscribed to the annual plan in May but I've generated exactly "
        "one article since. My situation changed and I won't be using the "
        "product. Can I get a refund for the unused portion of the year?",
    ),
    # --- UX friction and repeat customers (memory) ---
    (
        "hello@petitmarche.test",
        "How do I change the article tone?",
        "I can't find where to change the tone of voice for generated "
        "articles. I expected it in the editor sidebar but there's nothing "
        "there. How do I set a more casual tone for our blog?",
    ),
    (
        "eva.martins@copyloft.test",
        "Don't understand how credits are counted",
        "Following up separately from my generation issue: I don't "
        "understand how credits are counted. Regenerating a single section "
        "seems to cost as much as a full article. Is that intended? The "
        "pricing page doesn't explain it.",
    ),
]


def seed() -> None:
    conn = get_connection()
    try:
        inserted = 0
        for email, subject, body in CLIENT_TICKETS:
            exists = conn.execute(
                "SELECT 1 FROM tickets WHERE customer_email = ? AND subject = ?",
                (email, subject),
            ).fetchone()
            if exists:
                continue
            conn.execute(
                "INSERT INTO tickets (customer_email, subject, body) VALUES (?, ?, ?)",
                (email, subject, body),
            )
            inserted += 1
        conn.commit()
        skipped = len(CLIENT_TICKETS) - inserted
        print(f"Inserted {inserted} client tickets ({skipped} already present).")
        if inserted:
            print("Run python3 run_pipeline.py to let the crew process them.")
    finally:
        conn.close()


if __name__ == "__main__":
    seed()
