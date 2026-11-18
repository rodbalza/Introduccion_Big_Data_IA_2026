"""Exporta la página Calendario de Notion a Markdown y archivos locales.

La ejecución sin --publish solo inspecciona la estructura.
Las vistas de bases de datos de Notion no son renderizables directamente en GitHub,
por lo que la base Clases se transforma en un calendario estático Markdown.
"""
import argparse
import calendar as pycalendar
import json
import os
import shutil
import tempfile
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from export_clases import Notion, Renderer, ROOT, date_of, rich, title_of

OUTPUT = ROOT / "calendario"
STATE = ROOT / ".notion-calendario-state.json"

MONTHS_ES = {
    1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril",
    5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto",
    9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre",
}
WEEKDAYS_ES = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]


def page_title(page):
    properties = page.get("properties") or {}
    for prop in properties.values():
        if prop.get("type") == "title":
            title = rich(prop.get("title")).strip()
            if title:
                return title
    return "Calendario"


def scan_blocks(api, blocks):
    """Cuenta también los bloques anidados, sin descargar los archivos."""
    counts = Counter()
    for block in blocks:
        kind = block.get("type", "unknown")
        counts[kind] += 1
        if block.get("has_children") and kind not in ("child_page", "child_database"):
            counts.update(scan_blocks(api, api.children(block["id"])))
    return counts


def load_classes(api, db_id):
    """Lee las páginas activas de la base Clases."""
    db = api.request("GET", f"/databases/{db_id}")
    sources = db.get("data_sources") or []
    if len(sources) != 1:
        raise RuntimeError(f"Se esperaba 1 data source en Clases; se encontraron {len(sources)}")
    pages = api.items("POST", f"/data_sources/{sources[0]['id']}/query")
    pages = [
        p for p in pages
        if p.get("object") == "page"
        and not p.get("archived")
        and not p.get("in_trash")
    ]
    pages.sort(key=lambda p: (date_of(p) or "9999", title_of(p).lower(), p["id"]))
    return pages


def parse_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def md_escape(text):
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


def render_classes_calendar(pages):
    """Convierte las fechas de Clases en calendarios mensuales Markdown."""
    by_month = defaultdict(lambda: defaultdict(list))
    for page in pages:
        day = parse_date(date_of(page))
        if not day:
            continue
        by_month[(day.year, day.month)][day.day].append(md_escape(title_of(page)))

    if not by_month:
        return "_No hay clases con fecha para mostrar en el calendario._"

    out = [
        "## Vista de calendario",
        "",
        "> Esta es la versión estática para GitHub de la vista Calendar de Notion. "
        "Se actualiza automáticamente desde la base de datos **Clases**.",
        "",
    ]

    cal = pycalendar.Calendar(firstweekday=0)
    for year, month in sorted(by_month):
        out.append(f"### {MONTHS_ES[month]} {year}")
        out.append("")
        out.append("| " + " | ".join(WEEKDAYS_ES) + " |")
        out.append("| " + " | ".join(["---"] * 7) + " |")

        for week in cal.monthdayscalendar(year, month):
            cells = []
            for day_num in week:
                if day_num == 0:
                    cells.append("")
                    continue
                entries = by_month[(year, month)].get(day_num, [])
                cell = f"**{day_num}**"
                if entries:
                    cell += "<br>" + "<br>".join(entries)
                cells.append(cell)
            out.append("| " + " | ".join(cells) + " |")
        out.append("")

    return "\n".join(out).rstrip()


def calendar_fingerprint(api, page, blocks, class_pages):
    """Detecta cambios en Calendario y en las fechas/títulos de Clases."""
    from incremental import signature

    def stable(value):
        if isinstance(value, list):
            return [stable(item) for item in value]
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if key == "file" and isinstance(item, dict):
                    result[key] = {
                        k: stable(v) for k, v in item.items()
                        if k not in ("url", "expiry_time")
                    }
                else:
                    result[key] = stable(item)
            return result
        return value

    def snapshot(items):
        values = []
        for block in items:
            values.append(stable(block))
            if block.get("has_children") and block.get("type") not in ("child_page", "child_database"):
                values.append(snapshot(api.children(block["id"])))
        return values

    classes_snapshot = [
        {
            "id": p.get("id"),
            "last_edited_time": p.get("last_edited_time"),
            "title": title_of(p),
            "date": date_of(p),
        }
        for p in class_pages
    ]

    return signature([
        page.get("id"),
        page.get("last_edited_time"),
        stable(page.get("cover")),
        snapshot(blocks),
        classes_snapshot,
        "export-calendar-v3-static-db-calendar",
    ])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--publish", action="store_true", help="Exporta Calendario al repositorio")
    parser.add_argument("--force", action="store_true", help="Regenera el calendario completo")
    args = parser.parse_args()

    token = os.getenv("NOTION_TOKEN", "").strip()
    page_id = os.getenv("NOTION_CALENDARIO_ID", "").strip()
    classes_id = os.getenv("NOTION_CLASES_ID", "").strip()
    if not token or not page_id or not classes_id:
        raise RuntimeError("Falta NOTION_TOKEN, NOTION_CALENDARIO_ID o NOTION_CLASES_ID.")

    api = Notion(token)
    page = api.request("GET", f"/pages/{page_id}")
    if page.get("object") != "page":
        raise RuntimeError("El identificador de Calendario no apunta a una página.")
    if page.get("archived") or page.get("in_trash"):
        raise RuntimeError("La página Calendario está archivada o en la papelera.")

    blocks = api.children(page_id)
    counts = scan_blocks(api, blocks)
    class_pages = load_classes(api, classes_id)

    print("Página:", page_title(page))
    print("Bloques de primer nivel:", len(blocks))
    print("Bloques totales (incluidos anidados):", sum(counts.values()))
    for kind, count in sorted(counts.items()):
        print(f"  {kind}: {count}")
    print("Portada en Notion:", "sí" if page.get("cover") else "no")
    print("Clases disponibles para calendario:", len(class_pages))

    if counts.get("child_database"):
        print(
            "INFO: Notion expone una base de datos hija, pero GitHub no puede "
            "renderizar su vista interactiva; se genera una versión estática."
        )

    signature_value = calendar_fingerprint(api, page, blocks, class_pages)
    old_state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    changed = (
        args.force
        or old_state.get("signature") != signature_value
        or not (OUTPUT / "README.md").is_file()
    )
    print("Calendario:", "ACTUALIZAR" if changed else "SIN CAMBIOS")

    if not args.publish:
        print("PREVISUALIZACIÓN: no se han escrito archivos.")
        return
    if not changed:
        print("[OK] Calendario sin cambios: no se regeneran imágenes ni Markdown.")
        return

    with tempfile.TemporaryDirectory(prefix="notion-calendario-") as temp:
        stage = Path(temp) / "calendario"
        stage.mkdir()
        renderer = Renderer(api, stage)
        lines = ["# Calendario — Introducción al Big Data e IA", ""]

        cover = page.get("cover")
        if cover:
            lines.append(renderer.asset(cover, "Portada del calendario", inline=True))
            lines.append("")

        # Sustituye la vista interactiva de Notion por una representación visible en GitHub.
        lines.append(render_classes_calendar(class_pages))
        lines.append("")

        # Conserva el resto del contenido de la página (imágenes, texto, tablas, etc.).
        # Renderer ignora child_database para evitar duplicar una vista que GitHub no soporta.
        body = renderer.blocks(blocks)
        if body:
            lines.extend([body, ""])
        elif not class_pages:
            lines.append("_Todavía no hay contenido publicado en Calendario._")
            lines.append("")

        if not blocks and not cover and not class_pages and OUTPUT.exists():
            raise RuntimeError("La página aparece vacía; se conserva la versión publicada.")

        (stage / "README.md").write_text(
            "\n".join(lines).rstrip() + "\n",
            encoding="utf-8",
        )
        if OUTPUT.exists():
            shutil.rmtree(OUTPUT)
        shutil.copytree(stage, OUTPUT)

    STATE.write_text(json.dumps({
        "page_id": page_id,
        "last_edited_time": page.get("last_edited_time"),
        "block_count": sum(counts.values()),
        "class_count": len(class_pages),
        "signature": signature_value,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("EXPORTACIÓN PREPARADA: calendario/README.md y assets.")
    print("GitHub Actions registrará los cambios en un commit.")


if __name__ == "__main__":
    main()
