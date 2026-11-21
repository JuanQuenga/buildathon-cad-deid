from conftest import run_adapter

SVG = ('<?xml version="1.0" encoding="UTF-8"?>\n<!-- Created by Jane Roe -->\n'
       '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
       'xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd" '
       'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
       'sodipodi:docname="Bracket_Main.svg" inkscape:export-filename="/home/jroe/out.png" width="10mm">\n'
       '<metadata><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
       'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:cc="http://creativecommons.org/ns#">'
       '<cc:Work><dc:creator><cc:Agent><dc:title>Jane Roe</dc:title></cc:Agent></dc:creator>'
       '<dc:publisher><cc:Agent><dc:title>Acme Widgets</dc:title></cc:Agent></dc:publisher></cc:Work></rdf:RDF></metadata>\n'
       '<g id="Bracket_Main" inkscape:label="Acme Widgets &amp; Co"><path d="M 0,0 L 10,10" style="fill:none"/>'
       '<text x="1" y="2">Drawn: Jane Roe &lt;ok&gt; &#233;</text>'
       '<image xlink:href="data:image/png;base64,AAAA" /><use xlink:href="#Bracket_Main"/>'
       '<a xlink:href="parts/Bracket_Main.svg"><text>ACME-4711</text></a></g>\n</svg>\n').encode()


def test_svg(tmp_path, plan):
    sr, rr, vr, out = run_adapter(tmp_path, "svg", "a.svg", SVG, plan)
    o = out.decode()
    assert "Jane" not in o and "Acme" not in o and "jroe" not in o and "ACME-4711" not in o
    assert '<!-- Created by Person Alpha -->' in o
    assert 'sodipodi:docname="Part_Gamma.svg"' in o
    assert 'id="Bracket_Main"' in o and 'xlink:href="#Bracket_Main"' in o  # ids/fragments untouched
    assert 'inkscape:label="Company Beta &amp; Co"' in o
    assert 'Drawn: Person Alpha &lt;ok&gt; é</text>' in o
    assert 'xlink:href="parts/Part_Gamma.svg"' in o
    assert '<path d="M 0,0 L 10,10" style="fill:none"/>' in o
    kinds = {(ob.kind, ob.value) for ob in sr.observations}
    assert ("person", "Jane Roe") in kinds and ("company", "Acme Widgets") in kinds
