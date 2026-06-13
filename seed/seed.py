"""
Loop — database seed script for "Brew & Co." demo.

Seeds ONLY the `customers` and `orders` tables with a deliberately engineered
cohort distribution so the campaign agent's segment query returns a meaningful
audience and the "why included / why excluded" explainability has real contrast.

Re-runnable: TRUNCATEs orders then customers at the start, and uses a fixed
random seed, so every run produces a clean, deterministic dataset.

Cohorts (by construction):
  A) ~140 dormant high-spenders : total spend > 5000 AND last order > 60 days ago
  B)  ~60 active high-spenders  : total spend > 5000 but a recent order (<60 days)
  C) ~100 new / low-spend       : low total spend and/or very recent signup

Only customers/orders are touched; campaigns/messages/message_events stay empty.
"""

import os
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

try:
    import psycopg
except ImportError:
    sys.exit("psycopg (v3) is not installed. Run: pip install -r requirements.txt")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SEED = 42
random.seed(SEED)

N_DORMANT_HIGH = 140  # Cohort A — the hero demo segment (target 130–150)
N_ACTIVE_HIGH = 60  # Cohort B — high spend but excluded by recency
N_LOW_SPEND = 100  # Cohort C — excluded by spend
TOTAL_CUSTOMERS = N_DORMANT_HIGH + N_ACTIVE_HIGH + N_LOW_SPEND  # 300

HIGH_SPEND_THRESHOLD = 5000  # rupees; matches the agent's segment filter
RECENCY_DAYS = 60  # the recency cut-off the agent uses

NOW = datetime.now(timezone.utc)

# ---------------------------------------------------------------------------
# Realistic Indian name / city pools (mix of regions)
# ---------------------------------------------------------------------------
FIRST_NAMES = [
    # North
    "Aarav",
    "Vihaan",
    "Ishaan",
    "Kabir",
    "Rohit",
    "Aman",
    "Harshit",
    "Yash",
    "Simran",
    "Priya",
    "Neha",
    "Pooja",
    "Ananya",
    "Ritika",
    "Kavya",
    "Sneha",
    # South
    "Arjun",
    "Karthik",
    "Surya",
    "Vignesh",
    "Hari",
    "Ganesh",
    "Pranav",
    "Sandeep",
    "Lakshmi",
    "Divya",
    "Meena",
    "Anjali",
    "Swathi",
    "Deepa",
    "Nithya",
    "Revathi",
    # East
    "Soumya",
    "Rahul",
    "Abhijit",
    "Debojit",
    "Sourav",
    "Tanmay",
    "Arnab",
    "Bikram",
    "Riya",
    "Moumita",
    "Ipsita",
    "Sutapa",
    "Paromita",
    "Madhumita",
    "Sromona",
    "Trisha",
    # West
    "Het",
    "Jay",
    "Parth",
    "Dhruv",
    "Nikhil",
    "Manav",
    "Rohan",
    "Aditya",
    "Isha",
    "Aditi",
    "Khushi",
    "Vaishnavi",
    "Sakshi",
    "Mitali",
    "Janhvi",
    "Diya",
]

LAST_NAMES = [
    # North
    "Sharma",
    "Verma",
    "Gupta",
    "Malhotra",
    "Chauhan",
    "Sehgal",
    "Bhatia",
    "Khanna",
    # South
    "Reddy",
    "Nair",
    "Iyer",
    "Menon",
    "Pillai",
    "Naidu",
    "Raju",
    "Krishnan",
    # East
    "Banerjee",
    "Chatterjee",
    "Mukherjee",
    "Das",
    "Bose",
    "Ghosh",
    "Sen",
    "Dutta",
    # West
    "Patel",
    "Shah",
    "Joshi",
    "Desai",
    "Kulkarni",
    "Deshpande",
    "Pawar",
    "Mehta",
]

CITIES = [
    # Metros
    "Bengaluru",
    "Mumbai",
    "Delhi",
    "Hyderabad",
    "Chennai",
    "Kolkata",
    "Pune",
    # Tier-2
    "Jaipur",
    "Kochi",
    "Indore",
    "Nagpur",
    "Surat",
    "Lucknow",
    "Coimbatore",
    "Bhopal",
    "Visakhapatnam",
    "Chandigarh",
]


def make_phone() -> str:
    """Indian mobile: +91 followed by 10 digits starting 6-9."""
    return "+91" + str(random.randint(6, 9)) + "".join(str(random.randint(0, 9)) for _ in range(9))


def make_email(first: str, last: str, taken: set) -> str:
    base = f"{first}.{last}".lower()
    domain = random.choice(["gmail.com", "outlook.com", "yahoo.in", "brewco-fans.in"])
    email = f"{base}@{domain}"
    while email in taken:  # guarantee uniqueness
        email = f"{base}{random.randint(1, 9999)}@{domain}"
    taken.add(email)
    return email


def order_ts(min_days_ago: float, max_days_ago: float) -> datetime:
    """An EXPLICIT timestamp between max_days_ago and min_days_ago in the past.

    Order timestamps are always set here and inserted explicitly — we never
    rely on the orders.created_at column default (now()), which would date
    every order to 'today' and make max(created_at) always recent.
    """
    days = random.uniform(min_days_ago, max_days_ago)
    return NOW - timedelta(days=days, hours=random.uniform(0, 24))


def amounts_summing_above(n: int, lo_total: float, hi_total: float) -> list:
    """n realistic amounts whose sum is a random total in (lo_total, hi_total).

    lo_total is kept comfortably above the ₹5000 threshold so the sum clears
    it even after per-amount rounding.
    """
    total = random.uniform(lo_total, hi_total)
    weights = [random.uniform(0.6, 1.4) for _ in range(n)]
    s = sum(weights)
    return [round(total * w / s, 2) for w in weights]


# ---------------------------------------------------------------------------
# Build the dataset in memory
# ---------------------------------------------------------------------------
def build_dataset():
    customers = []  # tuples for insert
    orders = []  # tuples for insert
    emails_taken = set()

    def new_customer(signup_min_days, signup_max_days):
        cid = uuid.uuid4()
        first = random.choice(FIRST_NAMES)
        last = random.choice(LAST_NAMES)
        name = f"{first} {last}"
        phone = make_phone()
        email = make_email(first, last, emails_taken)
        city = random.choice(CITIES)
        created_at = NOW - timedelta(
            days=random.uniform(signup_min_days, signup_max_days),
            hours=random.uniform(0, 24),
        )
        customers.append((cid, name, phone, email, city, created_at))
        return cid, created_at

    # -- Cohort A: dormant high-spenders -----------------------------------
    # 4–8 orders summing to MORE than ₹5000, and EVERY order dated 61–180 days
    # ago — so max(created_at) is guaranteed > 60 days old. These are the hero
    # segment: high spend AND dormant.
    for _ in range(N_DORMANT_HIGH):
        cid, _signup = new_customer(signup_min_days=200, signup_max_days=365)
        n = random.randint(4, 8)
        for amt in amounts_summing_above(n, lo_total=5600, hi_total=9000):
            orders.append((uuid.uuid4(), cid, amt, order_ts(61, 180)))

    # -- Cohort B: active high-spenders ------------------------------------
    # Orders summing to > ₹5000 from older purchases, PLUS at least one order
    # dated within the last 25 days. max(created_at) is recent, so they are
    # EXCLUDED by the recency filter despite qualifying on spend.
    for _ in range(N_ACTIVE_HIGH):
        cid, _signup = new_customer(signup_min_days=200, signup_max_days=365)
        n = random.randint(4, 7)
        for amt in amounts_summing_above(n, lo_total=5600, hi_total=9000):
            orders.append((uuid.uuid4(), cid, amt, order_ts(26, 180)))
        # the recency-breaking recent order(s)
        for _ in range(random.randint(1, 2)):
            amt = round(random.uniform(150, 600), 2)
            orders.append((uuid.uuid4(), cid, amt, order_ts(1, 25)))

    # -- Cohort C: new / low-spend -----------------------------------------
    # 1–4 small orders summing to UNDER ₹5000 (max ~₹2400), recent signups —
    # EXCLUDED by the spend filter.
    for _ in range(N_LOW_SPEND):
        cid, signup = new_customer(signup_min_days=1, signup_max_days=120)
        signup_age = max(1, (NOW - signup).days)
        for _ in range(random.randint(1, 4)):
            amt = round(random.uniform(150, 600), 2)
            orders.append((uuid.uuid4(), cid, amt, order_ts(0, signup_age - 0.5)))

    return customers, orders


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    load_dotenv()
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit(
            "ERROR: DATABASE_URL is not set. Add it to your .env file, e.g.\n"
            "  DATABASE_URL=postgresql://user:pass@host:5432/dbname"
        )

    print(f"Building deterministic dataset (seed={SEED}) ...")
    customers, orders = build_dataset()
    print(f"  prepared {len(customers)} customers and {len(orders)} orders in memory")

    try:
        conn = psycopg.connect(dsn, connect_timeout=10)
    except Exception as exc:  # noqa: BLE001 — surface a friendly message
        sys.exit(
            "ERROR: could not connect to the database using DATABASE_URL.\n"
            f"  Details: {exc}\n"
            "  Check the host/port/credentials and that the DB is reachable."
        )

    try:
        with conn:
            with conn.cursor() as cur:
                # Idempotent reset — respect FK order (orders -> customers).
                print("Truncating orders, customers ...")
                cur.execute("TRUNCATE TABLE orders, customers RESTART IDENTITY CASCADE;")

                print("Inserting customers ...")
                cur.executemany(
                    "INSERT INTO customers (id, name, phone, email, city, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    customers,
                )

                print("Inserting orders ...")
                cur.executemany(
                    "INSERT INTO orders (id, customer_id, amount, created_at) "
                    "VALUES (%s, %s, %s, %s)",
                    orders,
                )

                # ---- Verification queries -------------------------------
                cur.execute("SELECT count(*) FROM customers;")
                total_customers = cur.fetchone()[0]

                cur.execute("SELECT count(*), COALESCE(sum(amount), 0) FROM orders;")
                total_orders, total_value = cur.fetchone()

                # Exact count of Cohort A as the agent would compute it.
                cur.execute("""
                    select count(*) from customers c
                    join (select customer_id, sum(amount) spend, max(created_at) last_order
                          from orders group by customer_id) o on o.customer_id = c.id
                    where o.spend > 5000 and o.last_order < now() - interval '60 days';
                    """)
                cohort_a_count = cur.fetchone()[0]
    finally:
        conn.close()

    # ---- Summary -----------------------------------------------------------
    print("\n" + "=" * 56)
    print("  SEED COMPLETE — Brew & Co. demo dataset")
    print("=" * 56)
    print(f"  Total customers        : {total_customers}")
    print(f"  Total orders           : {total_orders}")
    print(f"  Total order value      : Rs {total_value:,.2f}")
    print("-" * 56)
    print(f"  Cohort A (hero segment): {cohort_a_count}")
    print(f"    spend > Rs {HIGH_SPEND_THRESHOLD} AND last order > {RECENCY_DAYS} days ago")
    print(f"    (engineered target ~{N_DORMANT_HIGH}; valid range 130–150)")
    print("=" * 56)


if __name__ == "__main__":
    main()
