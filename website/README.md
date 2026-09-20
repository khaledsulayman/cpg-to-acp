# Carewright website

This directory is published directly to GitHub Pages by
[`pages.yml`](../.github/workflows/pages.yml) when changes under `website/` reach
`main`. No build step or package installation is required.

## Add or update a partner

1. Add the partner's official logo to `assets/partners/`. Prefer SVG; a transparent
   PNG or WebP also works. Use artwork intended for a white background.
2. Add an entry to [`partners.json`](partners.json), separated from the previous
   entry by a comma:

   ```json
   {
     "name": "Example Partner",
     "logo": "assets/partners/example-partner.svg",
     "url": "https://example.com/",
     "showName": false
   }
   ```

3. Preview the page, then commit the JSON and logo together. The order of the
   entries determines the order on the page. No HTML or CSS changes are needed.

| Field | Behavior |
| --- | --- |
| `name` | Required. Also supplies the accessible name when only a logo is visible. |
| `logo` | Path to a file inside `assets/partners/`, relative to this directory. Omit for a name-only entry. External image URLs are not used. |
| `url` | Optional partner website link (HTTP or HTTPS). Omit for an unlinked entry. |
| `showName` | Optional boolean, default `false`. Set to `true` to show the name below the logo. |

The partner strip follows the introduction on desktop. On smaller screens it
appears between the introductory text/buttons and the decision-table illustration.
Logos wrap automatically, with two columns on phones. White tiles preserve the
original logo colors in both light and dark mode; images retain their proportions.
A missing or broken logo falls back to the partner's name.

The list is loaded by a small browser script. JavaScript must be enabled and the
page must be served over HTTP(S); opening `index.html` directly as a file will not
load the JSON. An empty list, a failed request, or invalid JSON hides the strip;
entries without a nonempty name are skipped. Check the browser console if a list
does not appear.

## Preview

From the repository root:

```sh
python3 -m http.server 8000 --bind 127.0.0.1 --directory website
```

Open <http://127.0.0.1:8000/>. Check desktop and phone widths and both light and
dark system themes. When changing the layout, also try several extra entries to
check wrapping, an entry with `showName: true`, and a name-only entry.

## Logo sources

These are local copies of official company assets, retrieved on 2026-09-20.

| Partner | Asset source | Reference |
| --- | --- | --- |
| Red Hat | [Horizontal full-color SVG](https://www.redhat.com/rhdc/managed-files/Logo-Red_Hat-A-Standard-RGB.svg) | [Logo standards](https://www.redhat.com/en/about/brand/standards/logo) |
| Trisotech | [Header logo PNG](https://www.trisotech.com/wp-content/themes/trisotech/images/logo-trisotech-brand.png) | [Official website](https://www.trisotech.com/) |

Partner logos remain the trademarks of their respective owners; the project's
Apache 2.0 license does not relicense those marks. When adding a logo, record its
official source here and preserve the original artwork and appropriate clear space.
