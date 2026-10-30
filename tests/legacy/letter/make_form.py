"""Make a sample AcroForm PDF (text, checkbox, radio, combo, list) and a plain non-form PDF for the tests."""

import sys

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas


def form(path):
    c = canvas.Canvas(path, pagesize=A4)
    W, H = A4
    c.setFont("Helvetica-Bold", 14)
    c.drawString(25 * mm, H - 25 * mm, "Antrag Testformular")
    af = c.acroForm
    c.setFont("Helvetica", 10)
    y = H - 45 * mm
    for name, label in (("Name", "Name"), ("Vorname", "Vorname"), ("Strasse", "Strasse"), ("PLZOrt", "PLZ / Ort")):
        c.drawString(25 * mm, y + 2 * mm, label)
        af.textfield(name=name, x=60 * mm, y=y, width=90 * mm, height=7 * mm, borderWidth=0.5, fontSize=10)
        y -= 12 * mm
    c.drawString(25 * mm, y + 2 * mm, "Einverstanden")
    af.checkbox(name="Einverstanden", x=60 * mm, y=y, size=6 * mm, buttonStyle="check")
    y -= 12 * mm
    c.drawString(25 * mm, y + 2 * mm, "Zahlung")
    for i, v in enumerate(("Monatlich", "Jaehrlich")):
        af.radio(name="Zahlung", value=v, x=(60 + i * 35) * mm, y=y, size=6 * mm, selected=(i == 0))
        c.drawString((67 + i * 35) * mm, y + 2 * mm, v)
    y -= 12 * mm
    c.drawString(25 * mm, y + 2 * mm, "Kanton")
    af.choice(name="Kanton", options=["XX", "ZH", "TG"], value="ZH", x=60 * mm, y=y, width=40 * mm, height=7 * mm)
    y -= 20 * mm
    c.drawString(25 * mm, y + 12 * mm, "Sprache")
    af.listbox(
        name="Sprache",
        options=["Deutsch", "English", "Francais"],
        value="Deutsch",
        x=60 * mm,
        y=y,
        width=40 * mm,
        height=18 * mm,
    )
    c.drawString(25 * mm, 40 * mm, "Ort, Datum ______________________   Unterschrift ______________________")
    c.showPage()
    c.save()


def plain(path):
    c = canvas.Canvas(path, pagesize=A4)
    W, H = A4
    c.setFont("Helvetica-Bold", 14)
    c.drawString(25 * mm, H - 25 * mm, "Formular ohne Felder (Scan)")
    c.setFont("Helvetica", 10)
    c.drawString(25 * mm, H - 50 * mm, "Name: ________________________________")
    c.drawString(25 * mm, H - 60 * mm, "[ ] Ja    [ ] Nein")
    c.showPage()
    c.drawString(25 * mm, H - 25 * mm, "Seite 2")
    c.showPage()
    c.save()


if __name__ == "__main__":
    form(sys.argv[1])
    plain(sys.argv[2])
