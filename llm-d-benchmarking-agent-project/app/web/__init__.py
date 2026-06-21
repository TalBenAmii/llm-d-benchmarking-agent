"""Pure, decorator-free HTTP helpers extracted from ``app.main``.

This package holds the *mechanism* the FastAPI route handlers in ``app.main`` call but that
themselves do NOT register on the ``app`` object and do NOT need it: path-traversal hardening
for artifact/bundle serving, the public-share snapshot redaction, the no-cache static-files
subclass + CORS wiring, and the validation-error formatter.

Keeping these out of ``app.main`` shrinks the route module to its decorated handlers + the
``app`` wiring + the ``/ws`` loop, while the routes stay thin callers. Each helper is pure
(no module-level ``app``/``app.state``, no decorators) and takes whatever it needs as an
argument — notably any ``get_settings()`` call stays in the route handler (so a test that
monkeypatches ``app.main.get_settings`` still steers which workspace is resolved, and *when*).
"""
