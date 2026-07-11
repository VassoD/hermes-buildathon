"""Seed script: inserts realistic sample tickets into tickets.db.

Run with: python seed_data.py
Category and status are intentionally left at their defaults,
classification happens in phase 2.
"""

from db import get_connection

SEED_TICKETS = [
    # --- Billing (5) ---
    (
        "marcus.webb@fakemail.test",
        "Charged twice this month",
        "Hi, I just checked my bank statement and I see two charges of $49 "
        "from you on July 3rd. I'm only on one plan. Please fix this, I "
        "only expected a single charge.",
    ),
    (
        "priya.raman@example-corp.test",
        "On the wrong plan tier",
        "I signed up for the Starter plan at $19/month but my invoice shows "
        "the Pro plan at $49/month. I never upgraded. Can you move me back "
        "to Starter and adjust the bill?",
    ),
    (
        "jclarke@smallbiz.test",
        "Need an invoice for June",
        "Our accounting team needs a proper VAT invoice for the June payment. "
        "The receipt email doesn't include our company details or a VAT "
        "number field. Can you send a formal invoice?",
    ),
    (
        "annika.berg@nordiska.test",
        "Payment failed but I have funds",
        "My card was declined for this month's renewal even though there are "
        "plenty of funds on it. I got three failure emails already. How do I "
        "retry the payment before my account gets suspended?",
    ),
    (
        "tomas.silva@freelance.test",
        "Cancel my subscription and give my money back",
        "I want to cancel my subscription. Also I think that means you should "
        "refund me what I paid this month, right? I don't really understand "
        "how the billing cycle works. Please just cancel everything.",
    ),
    # --- Technical (5) ---
    (
        "dev.ops@quickship.test",
        "Can't log in since yesterday",
        "Since yesterday afternoon I get 'invalid credentials' even after "
        "resetting my password twice. The reset email arrives, I set a new "
        "password, and it still rejects it. Chrome and Firefox, same result.",
    ),
    (
        "hana.kobayashi@designhaus.test",
        "Export to PDF button does nothing",
        "The Export to PDF button in the reports view just spins forever and "
        "no file downloads. It worked last week. I'm on the Pro plan and "
        "this is blocking my client deliverable for tomorrow.",
    ),
    (
        "it.admin@brightschool.test",
        "Slack integration stopped posting",
        "Our Slack integration stopped posting notifications about 3 days "
        "ago. I reconnected the workspace and reauthorized the app but "
        "nothing comes through. The integration status page says 'connected'.",
    ),
    (
        "leo.martin@datacruncher.test",
        "Mobile app not syncing with web",
        "Changes I make in the mobile app don't show up on the web dashboard "
        "for hours, sometimes not at all. Both are on the latest version and "
        "logged into the same account. This started after your last update.",
    ),
    (
        "sofia.rossi@studioroma.test",
        "App crashes on startup after update",
        "After updating to version 4.2 the desktop app crashes immediately "
        "on startup. macOS Sonoma, M2 MacBook. I already tried reinstalling "
        "and clearing the cache folder. Crash report attached... well, it "
        "would be if your form let me attach files.",
    ),
    # --- Refund eligibility (3) ---
    (
        "gary.holt@budgetguy.test",
        "Requesting refund for last 40 days",
        "I signed up about 40 days ago and honestly barely used the product. "
        "It's not what I expected. I'd like a full refund of my first "
        "payment. I know it's been a bit over a month but I hope you can "
        "make an exception.",
    ),
    (
        "melissa.chan@wanderer.test",
        "Refund for unused subscription",
        "I subscribed to the annual plan in May, but I've logged in exactly "
        "once. Life got in the way and I'm not going to use it. Can I get a "
        "refund for the unused portion of the year?",
    ),
    (
        "gary.holt@budgetguy.test",
        "Duplicate purchase, need one refunded",
        "Me again. This time it's clear cut: I accidentally bought the same "
        "add-on pack twice within five minutes, probably a double click at "
        "checkout. I know I asked about a refund before and that didn't go "
        "my way, but this one is literally a duplicate purchase. Please "
        "refund one of the two charges.",
    ),
    # --- Ambiguous / mixed (2) ---
    (
        "nina.petrov@cloudkitchen.test",
        "Charged twice AND the feature doesn't work",
        "This is really frustrating. I was charged twice for the premium "
        "analytics add-on this month, and on top of that the analytics "
        "dashboard shows a blank page whenever I open it. So I'm paying "
        "double for something that doesn't even load. Fix both please.",
    ),
    (
        "dev.ops@quickship.test",
        "Still locked out, want this month refunded",
        "Following up on my login issue from earlier this week. I still "
        "can't get into my account, which means I've paid for a service I "
        "literally cannot access. If you can't fix the login today I want "
        "this month's payment refunded.",
    ),
]


def seed() -> None:
    conn = get_connection()
    try:
        existing = conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        if existing > 0:
            print(f"tickets.db already contains {existing} tickets, skipping seed.")
            print("Delete tickets.db and rerun to reseed from scratch.")
            return
        conn.executemany(
            "INSERT INTO tickets (customer_email, subject, body) VALUES (?, ?, ?)",
            SEED_TICKETS,
        )
        conn.commit()
        print(f"Inserted {len(SEED_TICKETS)} tickets into tickets.db.")
    finally:
        conn.close()


if __name__ == "__main__":
    seed()
