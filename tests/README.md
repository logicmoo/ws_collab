"""Anti-calcification contract for the WS_COLLAB test suite.

Tests here exist to protect *behaviour*, not to freeze the current design. A test
that fails only because an internal name, file layout, or arbitrary early choice
changed is a bug in the test.

Rules
-----
1. **Assert contracts, not shapes.** Prefer "what I wrote, I can read back",
   "positions never repeat", "the other transport sees it" over asserting a
   literal filename, field order, or exact collection size.
2. **Never hard-code storage layout.** Resolve paths via
   ``store.stream(name).describe_files()``. Filenames and sidecars are internal.
3. **Never hard-code stream names where a role exists.** Use
   ``ws_collab.events.streams_for_role()`` or the ``STREAMS`` registry so adding
   or renaming a stream extends coverage instead of breaking tests.
4. **Avoid exact counts of growable things** (engines, streams, voices, routes).
   Assert the property that matters ("at least three independent hypotheses",
   "every configured engine reported something").
5. **No hardware, credentials, paid APIs, or network.** Deterministic doubles and
   fake devices/voices exercise the same code paths production uses.
6. **Never weaken security to make a test pass.** Configure real tokens instead.

If a change to production code requires editing many tests, that is a signal the
tests were pinning an implementation detail -- fix the test, not just the code.
"""
