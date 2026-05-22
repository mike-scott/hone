# core/static

Vendored front-end assets, served at `/static/`. No build step and no CDN —
the operator UI is self-contained in the image (see `ARCHITECTURE.md` →
Operator web UI). Vendored here:

- `bootstrap.min.css` and `bootstrap.bundle.min.js` — Bootstrap 5.3.3
  (<https://getbootstrap.com/>)
- `htmx.min.js` — HTMX 2.0.4 (<https://htmx.org/>)

`core/templates/base.html` references them by exactly these names.

To refresh (re-pin the versions, then re-download):

```
curl -fsSL -o bootstrap.min.css        https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css
curl -fsSL -o bootstrap.bundle.min.js  https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js
curl -fsSL -o htmx.min.js              https://cdn.jsdelivr.net/npm/htmx.org@2.0.4/dist/htmx.min.js
```
