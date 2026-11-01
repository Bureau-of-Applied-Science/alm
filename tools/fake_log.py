"""Slowly append a plausible incident story to a log file.

Usage: python3 tools/fake_log.py /tmp/alm.log [--speed 1.0]
Runs a calm baseline, then a deploy, a DB degradation, an error burst, and recovery, looping.
"""
import argparse, random, time

STORY = [
    ("baseline", 20, [
        ("info", "caddy", "example.com GET /api/items -> 200"),
        ("info", "caddy", "example.com GET /static/app.css -> 200"),
        ("debug", "pm2:web", "gc pause 4ms, heap at 58%"),
        ("info", "pm2:worker", "job email-digest completed in 1.2s"),
        ("info", "caddy", "example.com GET /healthz -> 200"),
        ("warn", "caddy", "example.com GET /wp-login.php -> 404"),
    ]),
    ("deploy", 6, [
        ("info", "pm2:web", "SIGTERM received, draining 3 connections"),
        ("info", "pm2:web", "restarting app web (pid 4412 -> 4530)"),
        ("info", "pm2:web", "listening on :3000, build 9f3c21a"),
        ("warn", "caddy", "example.com GET /api/items -> 502 during upstream restart"),
    ]),
    ("degrade", 14, [
        ("warn", "pm2:web", "db pool wait 380ms, 9/10 connections busy"),
        ("warn", "pm2:web", "slow query 2140ms: SELECT * FROM orders WHERE user_id = $1 ORDER BY created_at"),
        ("info", "caddy", "example.com GET /api/orders -> 200"),
        ("warn", "caddy", "example.com GET /api/orders -> 504 upstream timeout after 5s"),
        ("debug", "pm2:web", "retrying query attempt 2/3"),
    ]),
    ("burst", 12, [
        ("error", "pm2:web", "TimeoutError: db pool exhausted, 10/10 busy, 41 waiting at Pool.acquire (db.js:88) at handler (orders.js:23)"),
        ("error", "caddy", "example.com POST /api/checkout -> 500"),
        ("error", "pm2:worker", "job invoice-render failed: ECONNRESET, will retry in 30s"),
        ("warn", "caddy", "example.com GET /api/orders -> 503"),
        ("error", "pm2:web", "unhandled rejection in checkout handler: cannot read properties of undefined (reading 'id')"),
    ]),
    ("recover", 10, [
        ("info", "pm2:web", "db pool recovered, 3/10 busy"),
        ("warn", "pm2:web", "circuit breaker half-open, probing db"),
        ("info", "caddy", "example.com POST /api/checkout -> 200"),
        ("info", "pm2:worker", "job invoice-render retry succeeded"),
        ("info", "caddy", "example.com GET /api/orders -> 200"),
    ]),
]

ap = argparse.ArgumentParser()
ap.add_argument("path")
ap.add_argument("--speed", type=float, default=1.0, help="higher = faster")
a = ap.parse_args()

with open(a.path, "a", buffering=1) as f:
    while True:
        for name, n, pool in STORY:
            print(f"-- phase: {name}", flush=True)
            for _ in range(n):
                lvl, src, msg = random.choice(pool)
                f.write(f"[{time.strftime('%H:%M:%S')}] {lvl.upper()} {src} {msg}\n")
                gap = {"baseline": (1.5, 4), "deploy": (0.8, 2), "degrade": (1, 2.5), "burst": (0.3, 1), "recover": (1, 3)}[name]
                time.sleep(random.uniform(*gap) / a.speed)
