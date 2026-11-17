import os

# Tests call the tools with fake data. Keep those calls out of Baton analytics.
os.environ.setdefault("BATON_DISABLED", "1")

# Turn on optional sign-in and saved places, with an in-memory store, for tests.
os.environ.setdefault("AUTHKIT_DOMAIN", "test.authkit.app")
os.environ.setdefault("PLACES_STORE", "memory")
os.environ.setdefault("BATON_USERS", "tester@example.com")
