"""Utilidades compartidas de sincronización incremental de Notion."""
import hashlib
import json
import shutil
from pathlib import Path


def read_state(path):
    if not path.exists():
        return {"pages": {}, "signatures": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"Estado inválido: {path}")
    return {"pages": data.get("pages") or {}, "signatures": data.get("signatures") or {}}


def signature(value):
    serialized = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def page_signature(page, *additional):
    # No se incluyen enlaces temporales de archivos firmados.
    return signature([page.get("id"), page.get("last_edited_time"), page.get("properties"), additional])


def write_state(path, pages, signatures):
    data = {"version": 2, "pages": pages, "signatures": signatures}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def remove_folder(root, relative):
    # Bloquea rutas fuera del directorio de exportación.
    base = root.resolve()
    target = (base / relative).resolve()
    if target == base or base not in target.parents:
        raise RuntimeError(f"Ruta de eliminación insegura: {relative}")
    if target.exists():
        if not target.is_dir():
            raise RuntimeError(f"Se esperaba una carpeta: {target}")
        shutil.rmtree(target)


def replace_folder(staged, target):
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(staged, target)
