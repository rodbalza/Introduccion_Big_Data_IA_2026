"""Exportación manual y controlada de la base de datos Clases de Notion."""
import argparse
import json
import os
import re
import shutil
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "clases"
STATE = ROOT / ".notion-clases-state.json"
API = "https://api.notion.com/v1"
VERSION = "2025-09-03"


def slug(text):
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "clase"


def rich(parts):
    output = []
    for item in parts or []:
        text = item.get("plain_text", "")
        if item.get("type") == "equation":
            text = "$" + item.get("equation", {}).get("expression", "") + "$"
        ann = item.get("annotations") or {}
        if ann.get("code"):
            text = "`" + text.replace("`", "\\`") + "`"
        if ann.get("bold"):
            text = "**" + text + "**"
        if ann.get("italic"):
            text = "*" + text + "*"
        if ann.get("strikethrough"):
            text = "~~" + text + "~~"
        href = item.get("href")
        if href:
            text = "[" + text.replace("]", "\\]") + "](" + href + ")"
        output.append(text)
    return "".join(output)


def cell(parts):
    return re.sub(r"(?<!\\)\|", r"\\|", rich(parts)).replace("\n", "<br>")


class Notion:
    def __init__(self, token):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Notion-Version": VERSION,
            "Content-Type": "application/json",
        })

    def request(self, method, route, **kwargs):
        import time
        for attempt in range(6):
            res = self.session.request(method, API + route, timeout=60, **kwargs)
            if res.status_code == 429 or (500 <= res.status_code < 600):
                if attempt < 5:
                    time.sleep(float(res.headers.get("Retry-After", min(2 ** attempt, 12))))
                    continue
            if not res.ok:
                raise RuntimeError(f"Notion {method} {route}: HTTP {res.status_code}: {res.text[:500]}")
            return res.json()
        raise RuntimeError("Notion no respondió después de varios intentos")

    def items(self, method, route, *, payload=None):
        values = []
        cursor = None
        while True:
            if method == "POST":
                data = dict(payload or {})
                data["page_size"] = 100
                if cursor:
                    data["start_cursor"] = cursor
                result = self.request("POST", route, json=data)
            else:
                params = {"page_size": 100}
                if cursor:
                    params["start_cursor"] = cursor
                result = self.request("GET", route, params=params)
            values.extend(result.get("results", []))
            if not result.get("has_more"):
                return values
            cursor = result.get("next_cursor")
            if not cursor:
                raise RuntimeError("Notion indica más páginas pero no devuelve cursor")

    def children(self, block_id):
        return self.items("GET", f"/blocks/{block_id}/children")


def title_of(page):
    for value in page.get("properties", {}).values():
        if value.get("type") == "title":
            return rich(value.get("title")).strip() or "Sin título"
    return "Sin título"


def date_of(page):
    for value in page.get("properties", {}).values():
        if value.get("type") == "date":
            return (value.get("date") or {}).get("start") or ""
    return ""


class Renderer:
    def __init__(self, api, folder):
        self.api = api
        self.folder = folder
        self.image_count = 0

    def asset(self, obj, caption, inline=False):
        kind = obj.get("type", "")
        url = (obj.get(kind) or {}).get("url")
        if not url:
            return ""
        if kind == "external":
            return f"![{caption}]({url})" if inline else f"[{caption}]({url})"
        # Los enlaces firmados de Notion caducan; siempre descargar los archivos.
        response = requests.get(url, timeout=90)
        response.raise_for_status()
        if len(response.content) > 25 * 1024 * 1024:
            raise RuntimeError("Archivo de Notion superior a 25 MB; requiere revisión")
        mime = response.headers.get("Content-Type", "").split(";")[0].lower()
        ext = {
            "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
            "image/webp": ".webp", "image/svg+xml": ".svg", "application/pdf": ".pdf",
        }.get(mime)
        if not ext:
            ext = Path(urlparse(url).path).suffix.lower()
            if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".pdf", ".txt", ".zip"):
                ext = ".bin"
        self.image_count += 1
        assets = self.folder / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        filename = f"asset-{self.image_count:03d}{ext}"
        (assets / filename).write_bytes(response.content)
        path = f"assets/{filename}"
        return f"![{caption}]({path})" if inline else f"[{caption}]({path})"

    def blocks(self, blocks):
        lines = []
        number = 0
        for block in blocks:
            kind = block.get("type")
            data = block.get(kind) or {}
            text = rich(data.get("rich_text"))
            result = ""
            if kind == "paragraph":
                result = text
            elif kind in ("heading_1", "heading_2", "heading_3"):
                result = "#" * (int(kind[-1]) + 1) + " " + text
            elif kind == "bulleted_list_item":
                result = "- " + text
            elif kind == "numbered_list_item":
                number += 1
                result = f"{number}. {text}"
            elif kind == "to_do":
                result = f"- [{'x' if data.get('checked') else ' '}] {text}"
            elif kind == "quote":
                result = "> " + text.replace("\n", "\n> ")
            elif kind == "callout":
                ico = data.get("icon") or {}
                emoji = ico.get("emoji", "") if ico.get("type") == "emoji" else ""
                result = "> " + (emoji + " " if emoji else "") + text
            elif kind == "code":
                language = data.get("language", "")
                code = "".join(p.get("plain_text", "") for p in data.get("rich_text", []))
                fence = "````" if "```" in code else "```"
                result = f"{fence}{language}\n{code}\n{fence}"
            elif kind == "divider":
                result = "---"
            elif kind == "bookmark":
                url = data.get("url", "")
                result = f"[{rich(data.get('caption')) or url}]({url})" if url else ""
            elif kind in ("embed", "link_preview"):
                url = data.get("url", "")
                result = f"<{url}>" if url else ""
            elif kind == "image":
                result = self.asset(data, rich(data.get("caption")) or "Imagen", inline=True)
            elif kind in ("file", "pdf"):
                result = self.asset(data, rich(data.get("caption")) or "Archivo")
            elif kind == "table":
                rows = self.api.children(block["id"])
                values = [[cell(c) for c in row.get("table_row", {}).get("cells", [])] for row in rows]
                if values:
                    width = max(map(len, values))
                    values = [r + [""] * (width - len(r)) for r in values]
                    result = "\n".join(
                        ["| " + " | ".join(values[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
                        + ["| " + " | ".join(row) + " |" for row in values[1:]]
                    )
            elif kind == "child_page":
                result = "**Subpágina:** " + data.get("title", "")
            elif kind == "toggle":
                result = f"<details>\n<summary>{text}</summary>\n"
            elif kind == "equation":
                result = "$$\n" + data.get("expression", "") + "\n$$"
            if result:
                lines.append(result)
            if block.get("has_children") and kind not in ("table", "child_page", "child_database"):
                nested = self.blocks(self.api.children(block["id"]))
                if nested:
                    lines.append(nested)
                if kind == "toggle":
                    lines.append("</details>")
            if result and kind not in ("bulleted_list_item", "numbered_list_item", "to_do"):
                lines.append("")
            if kind != "numbered_list_item":
                number = 0
        return "\n".join(lines).strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--publish", action="store_true", help="Publica las clases en el repositorio público")
    parser.add_argument("--force", action="store_true", help="Regenera todas las clases")
    args = parser.parse_args()
    token = os.getenv("NOTION_TOKEN")
    db_id = os.getenv("NOTION_CLASES_ID")
    if not token or not db_id:
        raise RuntimeError("Falta NOTION_TOKEN o NOTION_CLASES_ID")

    from incremental import page_signature, read_state, remove_folder, replace_folder, write_state
    api = Notion(token)
    db = api.request("GET", f"/databases/{db_id}")
    sources = db.get("data_sources") or []
    if len(sources) != 1:
        raise RuntimeError(f"Se esperaba 1 data source en Clases; se encontraron {len(sources)}")
    pages = api.items("POST", f"/data_sources/{sources[0]['id']}/query")
    pages = [p for p in pages if p.get("object") == "page" and not p.get("archived") and not p.get("in_trash")]
    pages.sort(key=lambda p: (date_of(p) or "9999", title_of(p).lower(), p["id"]))

    prior = read_state(STATE)
    old_pages, old_signatures = prior["pages"], prior["signatures"]
    manifest, signatures, changes = {}, {}, []
    index = [
        "# Clases — Introducción al Big Data e IA", "",
        "Apuntes publicados desde Notion, organizados cronológicamente.", "",
        "| Fecha | Clase |", "| --- | --- |",
    ]
    for page in pages:
        pid = page["id"]
        title = title_of(page)
        name = slug(title) + "-" + pid.replace("-", "")[:8]
        sig = page_signature(page, name, "export-clases-v2")
        manifest[pid], signatures[pid] = name, sig
        exists = (OUT / name / "README.md").is_file()
        # Estados antiguos sin firmas: se regeneran una sola vez para establecer baseline.
        updated = args.force or old_signatures.get(pid) != sig or old_pages.get(pid) != name or not exists
        if updated:
            changes.append(page)
        status = "ACTUALIZAR" if updated else "SIN CAMBIOS"
        print(f"  [{status}] {date_of(page) or 'Sin fecha'} | {title}")
        index.append(f"| {date_of(page)[:10] or '—'} | [{title.replace('|', chr(92) + '|')}]({name}/README.md) |")

    removed = [(pid, folder) for pid, folder in old_pages.items() if pid not in manifest]
    moved = [folder for pid, folder in old_pages.items() if pid in manifest and folder != manifest[pid]]
    print(f"Clases disponibles en Notion: {len(pages)}")
    print(f"Por actualizar: {len(changes)}; sin cambios: {len(pages) - len(changes)}; eliminadas: {len(removed)}")
    if not args.publish:
        print("PREVISUALIZACIÓN: no se ha escrito contenido.")
        return

    # Todas las páginas que requieren exportación se preparan antes de tocar archivos existentes.
    import tempfile
    with tempfile.TemporaryDirectory(prefix="notion-clases-") as tmp:
        stage = Path(tmp)
        for page in changes:
            pid = page["id"]
            title = title_of(page)
            name = manifest[pid]
            folder = stage / name
            folder.mkdir()
            body = Renderer(api, folder).blocks(api.children(pid))
            (folder / "README.md").write_text(f"# {title}\n\n{body}\n", encoding="utf-8")

        for page in changes:
            name = manifest[page["id"]]
            replace_folder(stage / name, OUT / name)
        for folder in set([f for _, f in removed] + moved):
            remove_folder(OUT, folder)

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "README.md").write_text("\n".join(index) + "\n", encoding="utf-8")
    write_state(STATE, manifest, signatures)
    print(f"[OK] Clases actualizadas: {len(changes)}. No modificadas: {len(pages) - len(changes)}.")


if __name__ == "__main__":
    main()
