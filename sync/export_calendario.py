"""Exporta la página Calendario de Notion a Markdown y archivos locales.

La ejecución sin --publish solo inspecciona la estructura.
"""
import argparse
import json
import os
import shutil
import tempfile
from collections import Counter
from pathlib import Path

from export_clases import Notion, Renderer, ROOT, rich

OUTPUT = ROOT / "calendario"
STATE = ROOT / ".notion-calendario-state.json"


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


def calendar_fingerprint(api, page, blocks):
    """Detecta cambios en bloques anidados sin depender de URLs firmadas caducables."""
    from incremental import signature

    def stable(value):
        if isinstance(value, list):
            return [stable(item) for item in value]
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                # Los archivos de Notion proporcionan URLs firmadas variables.
                if key == "file" and isinstance(item, dict):
                    result[key] = {k: stable(v) for k, v in item.items() if k not in ("url", "expiry_time")}
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

    return signature([
        page.get("id"),
        page.get("last_edited_time"),
        stable(page.get("cover")),
        snapshot(blocks),
        "export-calendar-v2",
    ])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--publish", action="store_true", help="Exporta Calendario al repositorio")
    parser.add_argument("--force", action="store_true", help="Regenera el calendario completo")
    args = parser.parse_args()

    token = os.getenv("NOTION_TOKEN", "").strip()
    page_id = os.getenv("NOTION_CALENDARIO_ID", "").strip()
    if not token or not page_id:
        raise RuntimeError("Falta NOTION_TOKEN o NOTION_CALENDARIO_ID.")

    api = Notion(token)
    page = api.request("GET", f"/pages/{page_id}")
    if page.get("object") != "page":
        raise RuntimeError("El identificador de Calendario no apunta a una página.")
    if page.get("archived") or page.get("in_trash"):
        raise RuntimeError("La página Calendario está archivada o en la papelera.")

    blocks = api.children(page_id)
    counts = scan_blocks(api, blocks)
    print("Página:", page_title(page))
    print("Bloques de primer nivel:", len(blocks))
    print("Bloques totales (incluidos anidados):", sum(counts.values()))
    for kind, count in sorted(counts.items()):
        print(f"  {kind}: {count}")
    print("Portada en Notion:", "sí" if page.get("cover") else "no")

    signature_value = calendar_fingerprint(api, page, blocks)
    old_state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    changed = args.force or old_state.get("signature") != signature_value or not (OUTPUT / "README.md").is_file()
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

        # Las portadas de página también pueden contener imágenes útiles.
        cover = page.get("cover")
        if cover:
            lines.append(renderer.asset(cover, "Portada del calendario", inline=True))
            lines.append("")

        body = renderer.blocks(blocks)
        if body:
            lines.extend([body, ""])
        else:
            lines.append("_Todavía no hay contenido publicado en Calendario._")
            lines.append("")

        # Una página completamente vacía NO debe borrar una versión ya publicada.
        if not blocks and not cover and OUTPUT.exists():
            raise RuntimeError("La página aparece vacía; se conserva la versión publicada.")

        (stage / "README.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        if OUTPUT.exists():
            shutil.rmtree(OUTPUT)
        shutil.copytree(stage, OUTPUT)

    STATE.write_text(json.dumps({
        "page_id": page_id,
        "last_edited_time": page.get("last_edited_time"),
        "block_count": sum(counts.values()),
        "signature": signature_value,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("EXPORTACIÓN PREPARADA: calendario/README.md y assets.")
    print("GitHub Actions registrará los cambios en un commit.")


if __name__ == "__main__":
    main()
