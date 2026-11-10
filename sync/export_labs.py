"""Sincroniza los laboratorios DP-900 y AI-901 desde Notion al repositorio público.

Sin --publish, solo enumera el contenido sin escribir archivos.
"""
import argparse
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from export_clases import Notion, Renderer, ROOT, rich, slug, title_of

OUT = ROOT / "laboratorios"
STATE = ROOT / ".notion-labs-state.json"
CERTS = ("DP-900", "AI-901")


def cert_of(page):
    """Lee la propiedad Select de Notion, sin depender de su posición."""
    properties = page.get("properties") or {}
    preferred = next(
        (value for key, value in properties.items()
         if key.strip().lower() == "select" and value.get("type") == "select"),
        None,
    )
    candidates = [preferred] if preferred else []
    candidates += [p for p in properties.values() if p.get("type") == "select" and p is not preferred]
    for prop in candidates:
        selected = prop.get("select") or {}
        label = selected.get("name", "").upper().strip()
        if label in CERTS:
            return label
    raise ValueError(f"Laboratorio sin certificación DP-900/AI-901: {title_of(page)}")


def path_urls(page):
    """Extrae los enlaces URL en propiedades de la página (Ruta Aprendizaje, etc.)."""
    links = []
    for key, prop in (page.get("properties") or {}).items():
        name = key.lower()
        if name not in ("ruta aprendizaje", "ruta de aprendizaje", "learning path"):
            continue
        if prop.get("type") == "url" and prop.get("url"):
            links.append((key, prop["url"]))
        elif prop.get("type") == "rich_text":
            for item in prop.get("rich_text", []):
                href = item.get("href") or (item.get("text") or {}).get("link", {}).get("url")
                if href:
                    links.append((key, href))
    return links


def order_key(page):
    title = title_of(page)
    match = re.search(r"\blab\s*0*(\d+)\b", title, re.IGNORECASE)
    # El contenido introductorio sin número aparece primero; los demás, por número.
    return (0, 0, title.casefold()) if not match else (1, int(match.group(1)), title.casefold())


def index_row(title, relative):
    # Escapa separadores de tabla.
    return f"| [{title.replace('|', chr(92) + '|')}]({relative}) |"


def classes_by_lab(api):
    """Recupera las relaciones de Clases hacia laboratorios, si las hay."""
    dbid = os.getenv("NOTION_CLASES_ID", "").strip()
    if not dbid:
        return {}
    database = api.request("GET", f"/databases/{dbid}")
    sources = database.get("data_sources") or []
    if len(sources) != 1:
        raise RuntimeError("La base Clases no tiene exactamente una data source.")
    pages = api.items("POST", f"/data_sources/{sources[0]['id']}/query")
    existing = ROOT / ".notion-clases-state.json"
    manifest = json.loads(existing.read_text(encoding="utf-8")).get("pages", {}) if existing.exists() else {}
    reverse = {}
    for page in pages:
        title = title_of(page)
        folder = manifest.get(page["id"])
        if not folder:
            continue
        for prop in (page.get("properties") or {}).values():
            if prop.get("type") != "relation":
                continue
            for relation in prop.get("relation") or []:
                reverse.setdefault(relation["id"], []).append((title, folder))
    return reverse


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--force", action="store_true", help="Regenera todos los laboratorios")
    args = parser.parse_args()

    from incremental import page_signature, read_state, remove_folder, replace_folder, write_state

    token = os.getenv("NOTION_TOKEN", "").strip()
    dbid = os.getenv("NOTION_LABS_ID", "").strip()
    if not token or not dbid:
        raise RuntimeError("Falta NOTION_TOKEN o NOTION_LABS_ID")

    api = Notion(token)
    database = api.request("GET", f"/databases/{dbid}")
    sources = database.get("data_sources") or []
    if len(sources) != 1:
        raise RuntimeError(f"La base de Laboratorios tiene {len(sources)} data sources; se esperaba una.")
    pages = api.items("POST", f"/data_sources/{sources[0]['id']}/query")
    pages = [p for p in pages if p.get("object") == "page" and not p.get("archived") and not p.get("in_trash")]
    grouped = {cert: [] for cert in CERTS}
    for page in pages:
        grouped[cert_of(page)].append(page)
    for cert in CERTS:
        grouped[cert].sort(key=order_key)

    # Las relaciones también se incluyen en las firmas, para actualizar enlaces
    # si cambia la clase relacionada aunque el laboratorio siga intacto en Notion.
    relations = classes_by_lab(api)
    prior = read_state(STATE)
    old_pages, old_signatures = prior["pages"], prior["signatures"]
    registry, signatures, changes = {}, {}, []
    indexes = {}
    root_index = [
        "# Laboratorios — Introducción al Big Data e IA", "",
        "Laboratorios y recursos de aprendizaje organizados por certificación.", "",
    ]
    for cert in CERTS:
        root_index.append(f"- [{cert}]({cert}/README.md) — {len(grouped[cert])} laboratorios")
        root_index.append("")
        index = [f"# Laboratorios {cert}", "", "| Laboratorio |", "| --- |"]
        print(f"\n{cert}: {len(grouped[cert])} laboratorios")
        for page in grouped[cert]:
            pid = page["id"]
            title = title_of(page)
            dirname = slug(title) + "-" + pid.replace("-", "")[:12]
            relative = f"{cert}/{dirname}"
            linked = sorted(relations.get(pid, []))
            sig = page_signature(page, relative, linked, path_urls(page), "export-labs-v2")
            registry[pid], signatures[pid] = relative, sig
            exists = (OUT / relative / "README.md").is_file()
            changed = args.force or old_signatures.get(pid) != sig or old_pages.get(pid) != relative or not exists
            if changed:
                changes.append((cert, page, relative))
            print(f"  [{'ACTUALIZAR' if changed else 'SIN CAMBIOS'}] {title}")
            index.append(index_row(title, f"{dirname}/README.md"))
        indexes[cert] = "\n".join(index) + "\n"

    removed = [folder for pid, folder in old_pages.items() if pid not in registry]
    moved = [folder for pid, folder in old_pages.items() if pid in registry and folder != registry[pid]]
    print(f"\nTotal: {len(pages)} laboratorios")
    print(f"Por actualizar: {len(changes)}; sin cambios: {len(pages) - len(changes)}; eliminados: {len(removed)}")
    if not args.publish:
        print("PREVISUALIZACIÓN: no se ha escrito contenido.")
        return

    # Primero procesar y descargar todos los recursos a carpetas temporales.
    with tempfile.TemporaryDirectory(prefix="notion-labs-") as tmp:
        stage = Path(tmp)
        for cert, page, relative in changes:
            pid = page["id"]
            title = title_of(page)
            folder = stage / relative
            folder.mkdir(parents=True)
            body = Renderer(api, folder).blocks(api.children(pid))
            output = [f"# {title}", "", f"**Certificación:** {cert}", ""]
            for label, url in path_urls(page):
                output.extend([f"**{label}:** [{url}]({url})", ""])
            linked = relations.get(pid, [])
            if linked:
                output.extend(["## Clases relacionadas", ""])
                for class_title, class_folder in sorted(linked):
                    output.append(f"- [{class_title}](../../../clases/{class_folder}/README.md)")
                output.append("")
            if body:
                output.extend([body, ""])
            (folder / "README.md").write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")

        for _, _, relative in changes:
            replace_folder(stage / relative, OUT / relative)
        for relative in set(removed + moved):
            remove_folder(OUT, relative)

    OUT.mkdir(parents=True, exist_ok=True)
    for cert, index in indexes.items():
        (OUT / cert).mkdir(parents=True, exist_ok=True)
        (OUT / cert / "README.md").write_text(index, encoding="utf-8")
    (OUT / "README.md").write_text("\n".join(root_index).rstrip() + "\n", encoding="utf-8")
    write_state(STATE, registry, signatures)
    print(f"[OK] Laboratorios actualizados: {len(changes)}. No modificados: {len(pages) - len(changes)}.")


if __name__ == "__main__":
    main()
