import os

# Tests call the tools with fake data. Keep those calls out of Baton analytics.
os.environ.setdefault("BATON_DISABLED", "1")
