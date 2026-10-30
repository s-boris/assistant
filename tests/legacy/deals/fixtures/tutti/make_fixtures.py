#!/usr/local/bin/python3.13 -I
"""Synthetic Tutti.ch search pages for the `deals tutti` tests (invented listings, places, ids; no recorded pages).

They mimic only what deals' tutti_parse reads: <title>, listing links /de/vi/<slug>/<id>, and the Next.js
<script id="__NEXT_DATA__"> with props.pageProps.dehydratedState.queries[] → SearchListingsByConstraints →
listings.edges[].node (listingID, localization.title/body, formattedPrice, postcodeInformation, timestamp).
Run it to rewrite the *.html files here:  python3 -I make_fixtures.py
"""

import html
import json
import os
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))


def node(lid, title, price, body="", place="Musterdorf", ts="2026-10-09T10:00:00+02:00", slug=True):
    return {
        "language": "DE",
        "listingID": str(lid),
        "localization": {"title": title, "body": body, "translationStatus": "ORIGINAL"},
        "postcodeInformation": {
            "postcode": "9999",
            "locationName": place,
            "canton": {"shortName": "XX", "name": "Testkanton"},
        },
        "timestamp": ts,
        "formattedPrice": price,
        "source": {"id": "tutti", "label": ""},
        "primaryCategory": {"categoryID": "computerComponentsAccessories"},
        "sellerInfo": {"alias": "seller-test", "logoURL": None, "subscriptionInfo": None},
        "seoInformation": {"deSlug": "x" if slug else None},
        "_slug": slug,
    }


def page(query, nodes):
    edges = []
    links = []
    for n in nodes:
        slug = n.pop("_slug")
        edges.append({"node": n})
        lid = n["listingID"]
        path = (
            (
                f"/de/vi/testkanton/computer-zubehoer/komponenten-zubehoer/"
                f"{n['localization']['title'].lower().replace(' ', '-')[:40]}/{lid}"
            )
            if slug
            else f"/de/vi/{lid}"
        )
        links.append(
            f'<a href="{html.escape(path)}"><div>{html.escape(n["localization"]["title"])}</div>'
            f"<span>{html.escape(n['formattedPrice'] or '')}</span></a>"
        )
    data = {
        "props": {
            "pageProps": {
                "messages": {"actions.edit": "Bearbeiten"},
                "dehydratedState": {
                    "mutations": [],
                    "queries": [
                        {
                            "queryKey": ["SearchListingsByConstraints", 0],
                            "state": {
                                "data": {
                                    "listings": {
                                        "totalCount": len(edges),
                                        "edges": edges,
                                        "placements": {"keyValues": []},
                                    },
                                    "galleryListings": [],
                                    "query": query,
                                    "searchToken": "TESTTOKEN",
                                }
                            },
                        },
                        {"queryKey": ["Categories"], "state": {"data": {"categoriesRoot": {}}}},
                    ],
                },
            }
        },
        "page": "/[lang]/q/[...slug]",
    }
    return (
        f'<!DOCTYPE html><html lang="de"><head><title>{len(edges)} Inserate für "{html.escape(query)}" in Alle '
        f"Kategorien - tutti.ch</title></head><body><main>{''.join(links)}</main>"
        f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data, ensure_ascii=False)}</script>'
        f"</body></html>\n"
    )


PAGES = {
    "16TB": [
        node(
            900001,
            "WD Elements Desktop 16TB",
            "240.-",
            "Externe Festplatte, 1 Jahr alt, keine defekten Sektoren.",
            ts="2026-10-09T18:00:00+02:00",
        ),
        node(900002, "Seagate Exos 16TB SATA", "280.-", "Aus Server ausgebaut, SMART ok.", slug=False),
        node(900003, "Seagate EXOS 16TB SAS", "150.-", "SAS 12Gb/s."),
        node(900004, "Desktop Rechner mit 16TB SSD", "2'400.-", "Workstation."),
        node(900005, "Dual-bay Docking Station für 2.5/3,5 Zoll", "40.-", "Unterstützt bis 16TB."),
        node(900006, "NAS-Server Testbox 2-Bay", "60.-", "Ohne Festplatten."),
        node(900007, "WD Red Plus 16TB defekt", "30.-", "Wird nicht erkannt."),
        node(900008, "Toshiba MG08 16TB", "320.-", "SATA, 2 Jahre Garantie."),
    ],
    "14TB": [
        node(900002, "Seagate Exos 16TB SATA", "280.-", "Aus Server ausgebaut, SMART ok.", slug=False),
        node(900011, "2x WD Elements 14TB", "440.-", "Zwei Stück, nur zusammen."),
        node(900012, 'WD My Passport 2.5" 5TB', "90.-", "Portable."),
        node(900013, "Seagate Exos 18TB", "210.-", "Neu."),
    ],
    "12TB": [],
    "WD Elements": [
        node(
            900021,
            "WD Elements 12TB inkl. USB Kabel und Netzadapter",
            "170.-",
            "Funktioniert einwandfrei.\u2028Abholung oder Versand.\x85",
        ),  # raw separators, as real pages have
        node(900022, "WD Elements 2TB", "50.-", ""),
    ],
    "My Book": [
        node(900031, "WD My Book 14000GB", "Auf Anfrage", "Preis nach Vereinbarung."),
    ],
    "Seagate Expansion": [
        node(900041, "Seagate Expansion Desktop 4 To", "80.-", "Disque dur externe."),
    ],
}

CHALLENGE = (
    '<!DOCTYPE html><html lang="en-US"><head><title>Just a moment...</title></head><body>'
    '<div id="challenge-running">Checking your browser</div>'
    '<form id="challenge-form" action="/de/q/suche?__cf_chl_f_tk=TEST" method="POST"></form>'
    '<script>window._cf_chl_opt={cType: "managed"};</script></body></html>\n'
)
NODATA = (
    '<!DOCTYPE html><html lang="de"><head><title>tutti.ch</title></head><body><main>'
    "<p>Neue Seite ohne Daten.</p></main></body></html>\n"
)


def main():
    for q, nodes in PAGES.items():
        with open(os.path.join(HERE, urllib.parse.quote(q, safe="") + ".html"), "w") as f:
            f.write(page(q, [dict(n) for n in nodes]))
    with open(os.path.join(HERE, "challenge.html"), "w") as f:
        f.write(CHALLENGE)
    with open(os.path.join(HERE, "nodata.html"), "w") as f:
        f.write(NODATA)


if __name__ == "__main__":
    main()
