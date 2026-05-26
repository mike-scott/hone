# core/static

Vendored front-end assets, served at `/static/`. No build step and no CDN —
the operator UI is self-contained in the image (see `ARCHITECTURE.md` →
Operator web UI). Vendored here:

- `bootstrap.min.css` and `bootstrap.bundle.min.js` — Bootstrap 5.3.3
  (<https://getbootstrap.com/>)
- `adminlte.min.css` and `adminlte.min.js` — AdminLTE 4.0.0, the Bootstrap-5
  admin theme that gives the operator UI its layout (<https://adminlte.io/>)
- `bootstrap-icons.min.css` + `fonts/bootstrap-icons.woff2` — Bootstrap Icons
  1.13.1, the sidebar / UI icon set (<https://icons.getbootstrap.com/>)
- `htmx.min.js` — HTMX 2.0.4 (<https://htmx.org/>)

`core/templates/base.html` references them by exactly these names. The
light/dark theme toggle is plain inline JS in `base.html` (Bootstrap 5's
`data-bs-theme`); no extra asset.

To refresh (re-pin the versions, then re-download):

```
curl -fsSL -o bootstrap.min.css        https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css
curl -fsSL -o bootstrap.bundle.min.js  https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js
curl -fsSL -o adminlte.min.css         https://cdn.jsdelivr.net/npm/admin-lte@4.0.0/dist/css/adminlte.min.css
curl -fsSL -o adminlte.min.js          https://cdn.jsdelivr.net/npm/admin-lte@4.0.0/dist/js/adminlte.min.js
curl -fsSL -o bootstrap-icons.min.css  https://cdn.jsdelivr.net/npm/bootstrap-icons@1.13.1/font/bootstrap-icons.min.css
curl -fsSL -o fonts/bootstrap-icons.woff2  https://cdn.jsdelivr.net/npm/bootstrap-icons@1.13.1/font/fonts/bootstrap-icons.woff2
curl -fsSL -o htmx.min.js              https://cdn.jsdelivr.net/npm/htmx.org@2.0.4/dist/htmx.min.js
```
