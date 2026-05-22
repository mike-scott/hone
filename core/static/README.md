# core/static

Vendored front-end assets, served at `/static/`. No build step and no CDN —
the operator UI is self-contained in the image (see `ARCHITECTURE.md` →
Operator web UI). Place here:

- `bootstrap.min.css` and `bootstrap.bundle.min.js` — Bootstrap 5.3.x
  (<https://getbootstrap.com/>)
- `htmx.min.js` — HTMX 2.x (<https://htmx.org/>)

`core/templates/base.html` references them by exactly these names.
