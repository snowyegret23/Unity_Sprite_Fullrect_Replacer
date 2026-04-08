from __future__ import annotations

import argparse
import io
import json
import os
import re
import struct
import sys
import traceback as tb_module
from pathlib import Path
from typing import Any, Literal, NoReturn, cast

import UnityPy
from PIL import Image, ImageChops, ImageFilter
from UnityPy.export import SpriteHelper

try:
    import cv2  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    cv2 = None  # type: ignore[assignment]

try:
    import numpy as np  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    np = None  # type: ignore[assignment]

try:
    import mapbox_earcut as earcut  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    earcut = None  # type: ignore[assignment]


Language = Literal["ko", "en"]
JsonDict = dict[str, Any]
Mode = Literal["fullrect", "tightclip", "tightmesh"]


class TeeWriter:
    """KR: stdout/stderr를 콘솔과 파일에 동시에 기록합니다.
    EN: Mirror stdout/stderr to both console and file.
    """

    def __init__(self, file: io.TextIOBase, original_stream: io.TextIOBase) -> None:
        self.file = file
        self.original = original_stream

    def write(self, data: str) -> int:
        self.original.write(data)
        self.file.write(data)
        self.file.flush()
        return len(data)

    def flush(self) -> None:
        self.original.flush()
        self.file.flush()

    def fileno(self) -> int:
        return self.original.fileno()

    @property
    def encoding(self) -> str:
        return self.original.encoding


def exit_with_error(message: str, code: int = 1) -> NoReturn:
    print(f"[오류] {message}")
    raise SystemExit(code)


def warn_unitypy_version(expected_major_minor: tuple[int, int] = (1, 24)) -> None:
    version = getattr(UnityPy, "__version__", "")
    try:
        parts = version.split(".")
        major = int(parts[0])
        minor = int(parts[1])
    except (ValueError, IndexError, AttributeError):
        print(f"[경고] UnityPy 버전 확인 실패: {version!r}")
        return
    if (major, minor) != expected_major_minor:
        print(f"[경고] 현재 UnityPy {version} 사용 중입니다. 검증 권장 버전은 {expected_major_minor[0]}.{expected_major_minor[1]}.x 입니다.")


def get_script_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def find_assets_files(data_path: Path) -> list[Path]:
    # Unity_Font_Replacer와 동일하게 _Data 전체를 기본 재귀 스캔합니다.
    # 확장자로 확실히 비에셋인 파일만 제외하고, 나머지는 UnityPy 로드 시도 대상으로 둡니다.
    exclude_exts = {
        ".dll",
        ".manifest",
        ".exe",
        ".txt",
        ".json",
        ".xml",
        ".log",
        ".ini",
        ".cfg",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".wav",
        ".mp3",
        ".ogg",
        ".mp4",
        ".avi",
        ".mov",
        ".ress",
    }
    assets_files: list[Path] = []
    for p in data_path.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() in exclude_exts:
            continue
        assets_files.append(p)
    assets_files.sort()
    return assets_files


def resolve_input_path(input_path: str) -> tuple[Path, Path, list[Path]]:
    p = Path(input_path).expanduser().resolve()

    # 단일 에셋 파일 입력 허용 (확장자 제한 없음)
    if p.is_file():
        data_path = p.parent
        game_path = data_path.parent if data_path.name.lower().endswith("_data") else data_path
        return game_path, data_path, [p]

    if not p.is_dir():
        raise FileNotFoundError(f"경로를 찾을 수 없습니다: {p}")

    if p.name.lower().endswith("_data"):
        data_path = p
        game_path = p.parent
    else:
        data_candidates = [d for d in p.iterdir() if d.is_dir() and d.name.lower().endswith("_data")]
        if not data_candidates:
            raise FileNotFoundError(f"_Data 폴더를 찾을 수 없습니다: {p}")
        data_path = sorted(data_candidates)[0]
        game_path = p

    assets_files = find_assets_files(data_path)
    if not assets_files:
        raise FileNotFoundError(f"처리 가능한 에셋 파일을 찾을 수 없습니다: {data_path}")
    return game_path, data_path, assets_files


def is_unitypy_short_read_error(exc: Exception) -> bool:
    if not isinstance(exc, ValueError):
        return False
    msg = str(exc)
    return msg.startswith("Expected to read ") and " but only read " in msg


def read_object_tolerant(obj_reader: Any) -> Any:
    try:
        return obj_reader.read()
    except Exception as exc:
        if is_unitypy_short_read_error(exc):
            return obj_reader.read(check_read=False)
        raise


def read_typetree_tolerant(obj_reader: Any) -> dict[str, Any]:
    try:
        return cast(dict[str, Any], obj_reader.read_typetree())
    except Exception as exc:
        if is_unitypy_short_read_error(exc):
            return cast(dict[str, Any], obj_reader.read_typetree(check_read=False))
        raise


def parse_object_tolerant(obj_reader: Any) -> Any:
    try:
        return obj_reader.parse_as_object()
    except Exception as exc:
        if is_unitypy_short_read_error(exc):
            return obj_reader.parse_as_object(check_read=False)
        raise


def deref_parse_as_object_tolerant(pptr: Any, assetsfile: Any = None) -> Any:
    return parse_object_tolerant(pptr.deref(assetsfile))


def get_sprite_image_tolerant(sprite: Any) -> Image.Image:
    atlas = None
    if sprite.m_SpriteAtlas:
        atlas = deref_parse_as_object_tolerant(sprite.m_SpriteAtlas)
    elif sprite.m_AtlasTags:
        assert sprite.assets_file, "Sprite assets file is not set!"
        for obj in sprite.assets_file.objects.values():
            if obj.type == SpriteHelper.ClassIDType.SpriteAtlas:
                name = obj.peek_name()
                if name == sprite.m_AtlasTags[0]:
                    atlas = parse_object_tolerant(obj)
                    break
                atlas = None

    if atlas:
        sprite_atlas_data = next(value for key, value in atlas.m_RenderDataMap if key == sprite.m_RenderDataKey)
        assert isinstance(sprite_atlas_data, SpriteHelper.SpriteAtlasData), "SpriteAtlasData not found!"
    else:
        sprite_atlas_data = sprite.m_RD

    m_texture2d = sprite_atlas_data.texture
    alpha_texture = sprite_atlas_data.alphaTexture
    texture_rect = sprite_atlas_data.textureRect
    settings_raw = sprite_atlas_data.settingsRaw

    assert sprite.assets_file, "Sprite assets file is not set!"
    cache = cast(dict[Any, Any], sprite.assets_file._cache)

    if alpha_texture:
        cache_id = (m_texture2d.path_id, alpha_texture.path_id)
        if cache_id not in cache:
            original_image = SpriteHelper.get_image_from_texture2d(deref_parse_as_object_tolerant(m_texture2d), False)
            alpha_image = SpriteHelper.get_image_from_texture2d(deref_parse_as_object_tolerant(alpha_texture), False)
            cache[cache_id] = Image.merge("RGBA", (*original_image.split()[:3], alpha_image.split()[0]))
    else:
        cache_id = m_texture2d.path_id
        if cache_id not in cache:
            cache[cache_id] = SpriteHelper.get_image_from_texture2d(deref_parse_as_object_tolerant(m_texture2d), False)

    original_image = cache[cache_id]
    sprite_image = original_image.crop(
        (
            texture_rect.x,
            texture_rect.y,
            texture_rect.x + texture_rect.width,
            texture_rect.y + texture_rect.height,
        )
    )

    settings = SpriteHelper.SpriteSettings(settings_raw)
    if settings.packed == 1:
        rotation = settings.packingRotation
        if rotation == SpriteHelper.SpritePackingRotation.kSPRFlipHorizontal:
            sprite_image = sprite_image.transpose(SpriteHelper.Transpose.FLIP_LEFT_RIGHT)
        elif rotation == SpriteHelper.SpritePackingRotation.kSPRFlipVertical:
            sprite_image = sprite_image.transpose(SpriteHelper.Transpose.FLIP_TOP_BOTTOM)
        elif rotation == SpriteHelper.SpritePackingRotation.kSPRRotate180:
            sprite_image = sprite_image.transpose(SpriteHelper.Transpose.ROTATE_180)
        elif rotation == SpriteHelper.SpritePackingRotation.kSPRRotate90:
            sprite_image = sprite_image.transpose(SpriteHelper.Transpose.ROTATE_270)

    if settings.packingMode == SpriteHelper.SpritePackingMode.kSPMTight:
        assert sprite.object_reader, "Sprite object reader is not set!"
        mesh = SpriteHelper.MeshHandler(sprite.m_RD, sprite.object_reader.version)
        mesh.process()
        if mesh.m_UV0 and any(u or v for u, v in mesh.m_UV0):
            try:
                sprite_image = SpriteHelper.render_sprite_mesh(sprite, mesh, original_image)
            except Exception:
                sprite_image = SpriteHelper.mask_sprite(sprite, mesh, sprite_image)
        else:
            sprite_image = SpriteHelper.mask_sprite(sprite, mesh, sprite_image)

    return sprite_image.transpose(SpriteHelper.Transpose.FLIP_TOP_BOTTOM)


def sanitize_filename(name: str) -> str:
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name.strip())
    sanitized = re.sub(r"\s+", " ", sanitized).strip()
    return sanitized or "unnamed"


def normalize_user_path_input(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""

    while True:
        changed = False

        while value and (value.startswith('"') or value.endswith('"')):
            if value.startswith('"'):
                value = value[1:].strip()
                changed = True
            if value.endswith('"'):
                value = value[:-1].strip()
                changed = True

        while value and (value.startswith("'") or value.endswith("'")):
            if value.startswith("'"):
                value = value[1:].strip()
                changed = True
            if value.endswith("'"):
                value = value[:-1].strip()
                changed = True

        if not changed:
            return value


def split_csv_args(values: list[str] | None) -> list[str]:
    if not values:
        return []
    out: list[str] = []
    for value in values:
        for token in value.split(","):
            token = token.strip()
            if token:
                out.append(token)
    return out


def normalize_mode(raw: Any, default: Mode = "fullrect") -> Mode:
    if not isinstance(raw, str):
        return default
    token = raw.strip().lower().replace("-", "").replace("_", "")
    if token in {"full", "fullrect", "rectangle"}:
        return "fullrect"
    if token in {"tight", "tightclip", "tightbbox"}:
        return "tightclip"
    if token in {"tightmesh", "tightpoly", "polygon"}:
        return "tightmesh"
    return default


def normalize_name_filter_token(raw: str) -> str:
    token = raw.strip()
    lower = token.lower()
    if lower.endswith(".sprite.png"):
        return token[: -len(".sprite.png")]
    if lower.endswith(".png"):
        return token[: -len(".png")]
    return token


def replacement_token_from_filename(file_name: str) -> str:
    token = file_name.strip()
    lower = token.lower()
    if lower.endswith(".sprite.png"):
        token = token[: -len(".sprite.png")]
    elif lower.endswith(".png"):
        token = token[: -len(".png")]
    else:
        token = Path(token).stem
    return token.strip()


def parse_extracted_sprite_png_name(file_name: str) -> tuple[str, int, str] | None:
    # format from --extract-all:
    # <sanitized_file_name>__<PathID>__<sanitized_sprite_name>.sprite.png
    lower = file_name.lower()
    if not lower.endswith(".sprite.png"):
        return None
    core = file_name[: -len(".sprite.png")]
    parts = core.split("__", 2)
    if len(parts) != 3:
        return None
    file_tag, path_id_raw, sprite_tag = parts
    file_tag = file_tag.strip()
    sprite_tag = sprite_tag.strip()
    if not file_tag or not sprite_tag:
        return None
    try:
        path_id = int(path_id_raw.strip(), 10)
    except Exception:
        return None
    return file_tag, path_id, sprite_tag


def build_filename_replacements(
    replace_dir: Path,
    *,
    recursive: bool,
    default_mode: Mode,
) -> dict[str, JsonDict]:
    def add_key(mapping: dict[str, JsonDict], key: str, entry: JsonDict, png_name: str) -> None:
        prev = mapping.get(key)
        if prev is not None and str(prev.get("replace_path")) != str(entry.get("replace_path")):
            print(f"[경고] 파일명 키 충돌로 이후 파일을 무시합니다: key={key} file={png_name}")
            return
        mapping[key] = entry

    pattern = "**/*.png" if recursive else "*.png"
    replacements: dict[str, JsonDict] = {}
    for png_path in sorted(replace_dir.glob(pattern)):
        if not png_path.is_file():
            continue
        token = replacement_token_from_filename(png_path.name)
        if not token:
            continue
        key = token.casefold()
        normalized: JsonDict = {
            "replace_path": str(png_path.resolve()),
            "mode": default_mode,
            "name": token,
        }
        # legacy name-token match
        add_key(replacements, key, normalized, png_path.name)
        add_key(replacements, f"name:{key}", normalized, png_path.name)

        parsed = parse_extracted_sprite_png_name(png_path.name)
        if parsed is not None:
            file_tag, path_id, sprite_tag = parsed
            file_tag_fold = file_tag.casefold()
            file_tag_safe_fold = sanitize_filename(file_tag).casefold()
            sprite_tag_fold = sprite_tag.casefold()
            sprite_tag_safe_fold = sanitize_filename(sprite_tag).casefold()

            # Prefer extracted filename pattern when available.
            add_key(replacements, f"idfile:{file_tag_fold}:{path_id}", normalized, png_path.name)
            add_key(replacements, f"idfile:{file_tag_safe_fold}:{path_id}", normalized, png_path.name)
            add_key(replacements, f"id:{path_id}", normalized, png_path.name)
            add_key(replacements, f"idname:{path_id}:{sprite_tag_fold}", normalized, png_path.name)
            add_key(replacements, f"idname:{path_id}:{sprite_tag_safe_fold}", normalized, png_path.name)
            add_key(replacements, f"name:{sprite_tag_fold}", normalized, png_path.name)
            add_key(replacements, f"name:{sprite_tag_safe_fold}", normalized, png_path.name)
    return replacements


def parse_id_filters(raw_values: list[str] | None) -> list[tuple[str, int]]:
    results: list[tuple[str, int]] = []
    for token in split_csv_args(raw_values):
        file_name = "*"
        path_id_raw = token
        if ":" in token:
            file_raw, path_id_raw = token.rsplit(":", 1)
            file_raw = file_raw.strip()
            file_name = "*" if not file_raw or file_raw == "*" else Path(file_raw).name.lower()
        try:
            path_id = int(path_id_raw, 0)
        except ValueError:
            print(f"[경고] 잘못된 PathID 필터를 무시합니다: {token}")
            continue
        results.append((file_name, path_id))
    return results


def parse_name_filters(raw_values: list[str] | None) -> tuple[set[str], set[str]]:
    exact_names: set[str] = set()
    exact_names_fold: set[str] = set()
    for token in split_csv_args(raw_values):
        normalized = normalize_name_filter_token(token)
        exact_names.add(normalized)
        exact_names_fold.add(normalized.casefold())
    return exact_names, exact_names_fold


def parse_name_contains(raw_values: list[str] | None) -> list[str]:
    return [token.casefold() for token in split_csv_args(raw_values)]


def sprite_matches_filters(
    file_name: str,
    path_id: int,
    sprite_name: str,
    id_filters: list[tuple[str, int]],
    names: set[str],
    names_fold: set[str],
    contains_tokens: list[str],
) -> bool:
    if id_filters:
        file_lower = file_name.lower()
        match_id = any((pid == path_id and (filt_file == "*" or filt_file == file_lower)) for filt_file, pid in id_filters)
        if not match_id:
            return False

    if names:
        if sprite_name not in names and sprite_name.casefold() not in names_fold:
            return False

    if contains_tokens:
        hay = sprite_name.casefold()
        if not any(token in hay for token in contains_tokens):
            return False

    return True


def make_sprite_record(
    *,
    file_name: str,
    assets_name: str,
    path_id: int,
    sprite_name: str,
    texture_rect_x: int,
    texture_rect_y: int,
    texture_rect_width: int,
    texture_rect_height: int,
    mode: Mode,
    replace_to: str = "",
) -> JsonDict:
    return {
        "Type": "Sprite",
        "File": file_name,
        "assets_name": assets_name,
        "Path_ID": path_id,
        "Name": sprite_name,
        "TextureRect_X": texture_rect_x,
        "TextureRect_Y": texture_rect_y,
        "TextureRect_Width": texture_rect_width,
        "TextureRect_Height": texture_rect_height,
        "Mode": mode,
        "Replace_to": replace_to,
    }


def load_sprite_records(
    assets_files: list[Path],
    *,
    id_filters: list[tuple[str, int]],
    names: set[str],
    names_fold: set[str],
    contains_tokens: list[str],
    default_mode: Mode,
) -> dict[str, JsonDict]:
    records: dict[str, JsonDict] = {}

    for assets_file in assets_files:
        try:
            env = UnityPy.load(str(assets_file))
        except Exception as e:
            print(f"[스킵] UnityPy.load 실패: {assets_file.name} ({e})")
            continue
        file_name = assets_file.name
        found_in_file = 0

        for obj in env.objects:
            if obj.type.name != "Sprite":
                continue
            sprite = read_object_tolerant(obj)
            sprite_name = str(getattr(sprite, "m_Name", "") or "")
            path_id = int(obj.path_id)
            if not sprite_matches_filters(file_name, path_id, sprite_name, id_filters, names, names_fold, contains_tokens):
                continue

            rect = getattr(getattr(sprite, "m_RD", None), "textureRect", None)
            rect_x = int(round(getattr(rect, "x", 0.0)))
            rect_y = int(round(getattr(rect, "y", 0.0)))
            rect_w = int(round(getattr(rect, "width", 0.0)))
            rect_h = int(round(getattr(rect, "height", 0.0)))

            key = f"{file_name}|{obj.assets_file.name}|{sprite_name}|Sprite|{path_id}"
            records[key] = make_sprite_record(
                file_name=file_name,
                assets_name=obj.assets_file.name,
                path_id=path_id,
                sprite_name=sprite_name,
                texture_rect_x=rect_x,
                texture_rect_y=rect_y,
                texture_rect_width=rect_w,
                texture_rect_height=rect_h,
                mode=default_mode,
            )
            found_in_file += 1

        if found_in_file:
            print(f"[정보] {assets_file.name}: Sprite {found_in_file}개")

    return records


def write_json(path: Path, payload: JsonDict | dict[str, JsonDict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def parse_sprites_to_json(
    assets_files: list[Path],
    *,
    json_out: Path,
    id_filters: list[tuple[str, int]],
    names: set[str],
    names_fold: set[str],
    contains_tokens: list[str],
    default_mode: Mode,
) -> int:
    records = load_sprite_records(
        assets_files,
        id_filters=id_filters,
        names=names,
        names_fold=names_fold,
        contains_tokens=contains_tokens,
        default_mode=default_mode,
    )
    write_json(json_out, records)
    print(f"[완료] JSON 저장: {json_out}")
    return len(records)


def extract_sprites(
    assets_files: list[Path],
    *,
    output_dir: Path,
    json_out: Path | None,
    id_filters: list[tuple[str, int]],
    names: set[str],
    names_fold: set[str],
    contains_tokens: list[str],
    default_mode: Mode,
) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    records: dict[str, JsonDict] = {}
    exported = 0
    skipped = 0

    for assets_file in assets_files:
        try:
            env = UnityPy.load(str(assets_file))
        except Exception as e:
            print(f"[스킵] UnityPy.load 실패: {assets_file.name} ({e})")
            continue
        file_name = assets_file.name
        file_count = 0

        for obj in env.objects:
            if obj.type.name != "Sprite":
                continue
            path_id = int(obj.path_id)
            sprite_name = ""
            try:
                sprite = read_object_tolerant(obj)
                sprite_name = str(getattr(sprite, "m_Name", "") or "")
                if not sprite_matches_filters(file_name, path_id, sprite_name, id_filters, names, names_fold, contains_tokens):
                    continue

                image = get_sprite_image_tolerant(sprite).convert("RGBA")
                rect = getattr(getattr(sprite, "m_RD", None), "textureRect", None)
                rect_x = int(round(getattr(rect, "x", 0.0)))
                rect_y = int(round(getattr(rect, "y", 0.0)))
                rect_w = int(round(getattr(rect, "width", 0.0)))
                rect_h = int(round(getattr(rect, "height", 0.0)))
                png_name = f"{sanitize_filename(file_name)}__{path_id}__{sanitize_filename(sprite_name)}.sprite.png"
                png_path = output_dir / png_name
                image.save(png_path)

                key = f"{file_name}|{obj.assets_file.name}|{sprite_name}|Sprite|{path_id}"
                records[key] = make_sprite_record(
                    file_name=file_name,
                    assets_name=obj.assets_file.name,
                    path_id=path_id,
                    sprite_name=sprite_name,
                    texture_rect_x=rect_x,
                    texture_rect_y=rect_y,
                    texture_rect_width=rect_w,
                    texture_rect_height=rect_h,
                    mode=default_mode,
                    replace_to=str(png_path),
                )
                exported += 1
                file_count += 1
            except Exception as e:
                skipped += 1
                if not sprite_name:
                    sprite_name = str(obj.peek_name() or "")
                print(f"[스킵] Sprite 추출 실패: {assets_file.name} | PathID={path_id} | Name={sprite_name} ({e})")
                continue

        if file_count:
            print(f"[정보] {assets_file.name}: Sprite {file_count}개 추출")

    if json_out:
        write_json(json_out, records)
        print(f"[완료] 추출 JSON 저장: {json_out}")

    if skipped:
        print(f"[정보] Sprite 추출 스킵 수: {skipped}")
    print(f"[완료] Sprite PNG 추출 수: {exported}")
    return exported


# ---------------------------------------------------------------------------
# UnityCN (China Unity) trailing bytes support
# ---------------------------------------------------------------------------
_trailing_bytes_store: dict[int, bytes] = {}


def _capture_trailing_bytes(obj: Any) -> bytes:
    """TypeTree 파싱 후 읽히지 않은 trailing bytes를 캡처합니다."""
    pos = obj.reader.Position
    end = obj.byte_start + obj.byte_size
    if pos < end:
        remaining = obj.reader.read_bytes(end - pos)
        obj.reader.Position = pos
        return remaining
    return b""


def _safe_parse_as_object(obj: Any, **kwargs: Any) -> Any:
    """parse_as_object()를 check_read=True로 먼저 시도하고,
    바이트 크기 불일치(중국판 Unity 등)로 실패하면 check_read=False로 재시도하고
    trailing bytes를 별도 저장소에 보존합니다.
    """
    obj_id = id(obj)
    try:
        result = obj.parse_as_object(check_read=True, **kwargs)
        _trailing_bytes_store.pop(obj_id, None)
        return result
    except ValueError as e:
        if "Expected to read" in str(e) and "bytes" in str(e):
            result = obj.parse_as_object(check_read=False, **kwargs)
            trailing = _capture_trailing_bytes(obj)
            if trailing:
                _trailing_bytes_store[obj_id] = trailing
            else:
                _trailing_bytes_store.pop(obj_id, None)
            return result
        raise


def _safe_parse_as_dict(obj: Any, **kwargs: Any) -> dict[str, Any]:
    """parse_as_dict()를 check_read=True로 먼저 시도하고,
    바이트 크기 불일치로 실패하면 check_read=False로 재시도하고
    trailing bytes를 별도 저장소에 보존합니다.
    """
    obj_id = id(obj)
    try:
        result = obj.parse_as_dict(check_read=True, **kwargs)
        _trailing_bytes_store.pop(obj_id, None)
        return result
    except ValueError as e:
        if "Expected to read" in str(e) and "bytes" in str(e):
            result = obj.parse_as_dict(check_read=False, **kwargs)
            trailing = _capture_trailing_bytes(obj)
            if trailing:
                _trailing_bytes_store[obj_id] = trailing
            else:
                _trailing_bytes_store.pop(obj_id, None)
            return result
        raise


def _safe_save(obj: Any, parse_dict: Any) -> None:
    """save() 후 trailing bytes가 있으면 raw data에 append합니다."""
    parse_dict.save()
    obj_id = id(obj)
    trailing = _trailing_bytes_store.pop(obj_id, b"")
    if trailing:
        current_data = obj.get_raw_data()
        obj.set_raw_data(current_data + trailing)


def _has_trailing_bytes(obj: Any) -> bool:
    """이 오브젝트에 TypeTree로 읽히지 않는 trailing bytes가 있는지 확인합니다."""
    return id(obj) in _trailing_bytes_store


def _detect_typetree_size_mismatch(obj: Any) -> bool:
    """TypeTree로 읽은 후 다시 쓰면 원본보다 작아지는지 감지합니다.
    중국판 Unity 등에서 TypeTree에 없는 추가 필드가 있으면 True를 반환합니다.
    """
    try:
        from UnityPy.helpers.TypeTreeHelper import write_typetree
        from UnityPy.streams import EndianBinaryWriter
        original_raw = obj.get_raw_data()
        d = obj.read_typetree(check_read=False)
        node = obj._get_typetree_node()
        w = EndianBinaryWriter(endian=obj.reader.endian)
        write_typetree(d, node, w, obj.assets_file)
        rewritten_size = w.Length
        w.dispose()
        return rewritten_size < len(original_raw)
    except Exception:
        return False


def _binary_patch_texture2d(
    obj: Any,
    *,
    image_data: bytes,
    width: int,
    height: int,
) -> bool:
    """Texture2D를 TypeTree 재직렬화 없이 바이너리 패치합니다.
    중국판 Unity 등에서 TypeTree가 커버하지 못하는 extra bytes가 있을 때 사용합니다.
    """
    import struct as _struct

    original_raw = obj.get_raw_data()
    if len(original_raw) < 48:
        return False

    # 원본 raw에서 스트림 경로 문자열을 찾아 필드 위치를 역추적합니다.
    stream_path_marker = None
    for marker in [b".resS", b".resource"]:
        idx = original_raw.find(marker)
        if idx >= 0:
            str_start = idx
            while str_start > 0 and original_raw[str_start - 1:str_start] not in (b"\x00",):
                str_start -= 1
                if idx - str_start > 200:
                    break
            path_len_pos = str_start - 4
            if path_len_pos < 0:
                continue
            try:
                path_len = _struct.unpack_from("<i", original_raw, path_len_pos)[0]
                if 0 < path_len < 256 and path_len_pos + 4 + path_len <= len(original_raw):
                    stream_path_marker = (path_len_pos, path_len, str_start)
                    break
            except Exception:
                continue

    # TypeTree로 파싱하여 image data 위치와 trailing bytes를 정확히 파악합니다.
    try:
        d_temp = obj.read_typetree(check_read=False)
        orig_img_data = d_temp.get("image data", b"")
        orig_img_len = len(orig_img_data) if isinstance(orig_img_data, (bytes, bytearray, memoryview)) else 0
    except Exception:
        return False

    if stream_path_marker is not None:
        path_len_pos, path_len, path_str_start = stream_path_marker
        stream_size_pos = path_len_pos - 4
        stream_offset_pos = stream_size_pos - 8
        image_data_size_pos = stream_offset_pos - 4
        orig_stream_end = path_str_start + path_len
        orig_stream_end += (4 - orig_stream_end % 4) % 4
    else:
        obj.reset()
        pos0 = obj.reader.Position
        obj.read_typetree(check_read=False)
        pos1 = obj.reader.Position
        typetree_bytes = pos1 - pos0

        trailing_size = len(original_raw) - typetree_bytes
        empty_stream_data_size = 16
        img_block_size = 4 + orig_img_len
        img_block_padded = img_block_size + (4 - img_block_size % 4) % 4

        image_data_size_pos = typetree_bytes - trailing_size - empty_stream_data_size - img_block_padded
        if image_data_size_pos < 0:
            image_data_size_pos = len(original_raw) - trailing_size - empty_stream_data_size - img_block_padded
        orig_stream_end = len(original_raw) - trailing_size

    if image_data_size_pos < 0 or image_data_size_pos >= len(original_raw):
        return False

    # TypeTree 파싱으로 정확한 필드 오프셋을 구합니다.
    from UnityPy.helpers.TypeTreeHelper import TypeTreeConfig as _TTC, read_value as _rv
    from UnityPy.streams import EndianBinaryReader as _EBR
    field_offsets: dict[str, int] = {}
    _tmp_reader = None
    try:
        _tmp_reader = _EBR(original_raw, endian=obj.reader.endian)
        _tmp_config = _TTC(True, obj.assets_file, False)
        _node = obj._get_typetree_node()
        for _child in _node.m_Children:
            _pos_before = _tmp_reader.Position
            _rv(_child, _tmp_reader, _tmp_config)
            field_offsets[_child.m_Name] = _pos_before
    except Exception:
        pass

    if "image data" in field_offsets:
        image_data_size_pos = field_offsets["image data"]

    part1 = bytearray(original_raw[:image_data_size_pos])

    # 정확한 오프셋으로 필드 패치
    if "m_Width" in field_offsets and field_offsets["m_Width"] + 4 <= len(part1):
        _struct.pack_into("<i", part1, field_offsets["m_Width"], width)
    if "m_Height" in field_offsets and field_offsets["m_Height"] + 4 <= len(part1):
        _struct.pack_into("<i", part1, field_offsets["m_Height"], height)
    if "m_CompleteImageSize" in field_offsets and field_offsets["m_CompleteImageSize"] + 4 <= len(part1):
        _struct.pack_into("<I", part1, field_offsets["m_CompleteImageSize"], len(image_data))

    part1 = bytes(part1)

    # Part 2+3 — inline image data + 빈 StreamingInfo
    from UnityPy.streams import EndianBinaryWriter
    w = EndianBinaryWriter(endian="<")
    w.write_int(len(image_data))
    w.write(image_data)
    pos = w.Length
    pad = (4 - pos % 4) % 4
    if pad:
        w.write(b"\x00" * pad)
    w.write_u_long(0)   # offset
    w.write_u_int(0)    # size
    w.write_int(0)      # empty path (length=0)
    pos = w.Length
    pad = (4 - pos % 4) % 4
    if pad:
        w.write(b"\x00" * pad)
    part2_3 = w.bytes
    w.dispose()

    # Part 4 — trailing bytes
    if "image data" in field_offsets and _tmp_reader is not None:
        typetree_end = _tmp_reader.Position
        part4 = original_raw[typetree_end:]
    else:
        part4 = original_raw[orig_stream_end:]

    new_raw = part1 + part2_3 + part4
    obj.set_raw_data(new_raw)
    obj.assets_file.mark_changed()
    return True


def save_serialized_file_with_fallback(serialized_file: Any) -> bytes:
    errors: list[Exception] = []
    for packer in ("original", "lz4", None):
        try:
            return cast(bytes, serialized_file.save(packer=packer))
        except Exception as e:  # pragma: no cover - 환경별 저장 포맷 대응
            errors.append(e)
    joined = "; ".join(str(e) for e in errors)
    raise RuntimeError(f"에셋 저장 실패: {joined}")


def _save_texture_with_unitycn_fallback(
    texture: Any,
    tex_obj_reader: Any | None,
    texture_image: Image.Image,
) -> None:
    """Texture2D를 저장합니다. UnityCN(trailing bytes/size mismatch)이 감지되면
    binary patch를 시도하고, 아니면 일반 save()를 사용합니다.
    """
    texture.set_image(texture_image)

    need_binary_patch = False
    if tex_obj_reader is not None:
        if _has_trailing_bytes(tex_obj_reader) or _detect_typetree_size_mismatch(tex_obj_reader):
            need_binary_patch = True

    if need_binary_patch and tex_obj_reader is not None:
        # binary patch에 필요한 image_data를 texture에서 가져옵니다
        tex_w, tex_h = texture_image.size
        # texture.set_image() 후 내부 image_data가 갱신되므로 get_raw_data에서 추출
        # save()를 통해 image_data를 얻는 대신, 직접 인코딩합니다
        try:
            from UnityPy.export.Texture2DConverter import image_to_texture2d
            img_data = image_to_texture2d(texture_image, texture.m_TextureFormat)
            if _binary_patch_texture2d(
                tex_obj_reader,
                image_data=img_data,
                width=tex_w,
                height=tex_h,
            ):
                return
        except Exception:
            pass
    # 일반 저장 경로
    texture.save()


def image_equal(a: Image.Image, b: Image.Image) -> bool:
    if a.size != b.size:
        return False
    return ImageChops.difference(a, b).getbbox() is None


def convert_settings_raw(raw: int, mode: Mode) -> int:
    if mode == "fullrect":
        return (raw | (1 << 1)) & ~(1 << 6)
    return (raw & ~(1 << 1)) | (1 << 6)


def _is_quad_mesh(rd: dict[str, Any]) -> bool:
    vd = rd.get("m_VertexData")
    if not isinstance(vd, dict):
        return False
    try:
        vcount = int(vd.get("m_VertexCount", 0))
    except Exception:
        return False
    if vcount != 4:
        return False

    idx = rd.get("m_IndexBuffer")
    if not isinstance(idx, list) or len(idx) != 12:
        return False

    sub_meshes = rd.get("m_SubMeshes")
    if not isinstance(sub_meshes, list) or not sub_meshes:
        return False
    first = sub_meshes[0]
    if not isinstance(first, dict):
        return False
    return int(first.get("indexCount", 0)) == 6 and int(first.get("vertexCount", 0)) == 4


def _is_tight_mesh(rd: dict[str, Any]) -> bool:
    vd = rd.get("m_VertexData")
    if not isinstance(vd, dict):
        return False
    try:
        vcount = int(vd.get("m_VertexCount", 0))
    except Exception:
        return False
    if vcount < 3:
        return False

    idx = rd.get("m_IndexBuffer")
    if not isinstance(idx, list) or len(idx) < 6 or (len(idx) % 6) != 0:
        return False

    sub_meshes = rd.get("m_SubMeshes")
    if not isinstance(sub_meshes, list) or not sub_meshes:
        return False
    first = sub_meshes[0]
    if not isinstance(first, dict):
        return False
    return int(first.get("indexCount", 0)) >= 3 and int(first.get("vertexCount", 0)) >= 3


def _read_mesh_bounds(rd: dict[str, Any]) -> tuple[float, float, float, float, float] | None:
    vd = rd.get("m_VertexData")
    if not isinstance(vd, dict):
        return None

    try:
        vcount = int(vd.get("m_VertexCount", 0))
    except Exception:
        return None
    data = vd.get("m_DataSize")
    if not isinstance(data, (bytes, bytearray)):
        return None
    if vcount < 3:
        return None

    pos_size = vcount * 12
    uv_size = vcount * 8
    if len(data) < pos_size + uv_size:
        return None

    xs: list[float] = []
    ys: list[float] = []
    zs: list[float] = []
    for i in range(vcount):
        x, y, z = struct.unpack_from("<fff", data, i * 12)
        xs.append(float(x))
        ys.append(float(y))
        zs.append(float(z))

    min_x = min(xs)
    max_x = max(xs)
    min_y = min(ys)
    max_y = max(ys)
    z = sum(zs) / len(zs) if zs else 0.0
    return min_x, max_x, min_y, max_y, z


def _pack_vertex_streams_pos_uv(
    positions: list[tuple[float, float, float]],
    uvs: list[tuple[float, float]],
) -> bytes:
    if len(positions) != len(uvs):
        raise ValueError("positions/uvs 개수가 일치해야 합니다.")

    out = bytearray()
    for px, py, pz in positions:
        out.extend(struct.pack("<fff", px, py, pz))

    # Unity VertexData stream은 stream 경계가 16-byte aligned 입니다.
    pad = (-len(out)) & 0x0F
    if pad:
        out.extend(b"\x00" * pad)

    for u, v in uvs:
        out.extend(struct.pack("<ff", u, v))

    return bytes(out)


def _force_quad_mesh_from_sprite_rect(
    rd: dict[str, Any],
    tree: dict[str, Any],
    *,
    rect_xywh: tuple[int, int, int, int] | None,
) -> bool:
    bounds = _read_mesh_bounds(rd)
    if bounds is None:
        return False
    _old_min_x, _old_max_x, _old_min_y, _old_max_y, z = bounds

    if rect_xywh is None:
        texture_rect = cast(dict[str, Any], rd.get("textureRect", {}))
        try:
            x = int(round(float(texture_rect.get("x", 0.0))))
            y = int(round(float(texture_rect.get("y", 0.0))))
            w = int(round(float(texture_rect.get("width", 0.0))))
            h = int(round(float(texture_rect.get("height", 0.0))))
        except Exception:
            return False
    else:
        x, y, w, h = rect_xywh

    if w <= 0 or h <= 0:
        return False

    ppu_raw = tree.get("m_PixelsToUnits", 100.0)
    try:
        ppu = float(ppu_raw)
    except Exception:
        ppu = 100.0
    if ppu <= 0.0:
        ppu = 100.0

    pivot_raw = cast(dict[str, Any], tree.get("m_Pivot", {"x": 0.5, "y": 0.5}))
    try:
        pivot_x = float(pivot_raw.get("x", 0.5))
    except Exception:
        pivot_x = 0.5
    try:
        pivot_y = float(pivot_raw.get("y", 0.5))
    except Exception:
        pivot_y = 0.5
    # Some formats store pivot as pixels instead of normalized 0..1.
    if pivot_x > 1.0:
        pivot_x = pivot_x / float(max(1, w))
    if pivot_y > 1.0:
        pivot_y = pivot_y / float(max(1, h))
    pivot_x = min(max(pivot_x, 0.0), 1.0)
    pivot_y = min(max(pivot_y, 0.0), 1.0)

    full_rect = cast(dict[str, Any], tree.get("m_Rect", {}))
    try:
        full_x = float(full_rect.get("x", 0.0))
    except Exception:
        full_x = 0.0
    try:
        full_y = float(full_rect.get("y", 0.0))
    except Exception:
        full_y = 0.0
    try:
        full_w = float(full_rect.get("width", float(w)))
    except Exception:
        full_w = float(w)
    try:
        full_h = float(full_rect.get("height", float(h)))
    except Exception:
        full_h = float(h)

    pivot_px = full_x + (full_w * pivot_x)
    pivot_py = full_y + (full_h * pivot_y)

    min_x = (float(x) - pivot_px) / ppu
    max_x = (float(x + w) - pivot_px) / ppu
    min_y = (float(y) - pivot_py) / ppu
    max_y = (float(y + h) - pivot_py) / ppu

    vd = cast(dict[str, Any], rd.get("m_VertexData", {}))
    # Build 4-vertex rectangle and keep uv all-zero (tooling-compatible import style).
    positions = [
        (min_x, min_y, z),
        (min_x, max_y, z),
        (max_x, max_y, z),
        (max_x, min_y, z),
    ]
    uvs = [(0.0, 0.0)] * 4

    vd["m_VertexCount"] = 4
    vd["m_DataSize"] = _pack_vertex_streams_pos_uv(positions, uvs)
    rd["m_VertexData"] = vd

    rd["m_IndexBuffer"] = [0, 0, 1, 0, 2, 0, 0, 0, 2, 0, 3, 0]

    sub_meshes = rd.get("m_SubMeshes")
    if not isinstance(sub_meshes, list) or not sub_meshes:
        sub_meshes = [{
            "firstByte": 0,
            "indexCount": 6,
            "topology": 0,
            "baseVertex": 0,
            "firstVertex": 0,
            "vertexCount": 4,
            "localAABB": {
                "m_Center": {"x": 0.0, "y": 0.0, "z": 0.0},
                "m_Extent": {"x": 0.0, "y": 0.0, "z": 0.0},
            },
        }]
    else:
        first = sub_meshes[0]
        if isinstance(first, dict):
            first["firstByte"] = 0
            first["indexCount"] = 6
            first["topology"] = int(first.get("topology", 0))
            first["baseVertex"] = 0
            first["firstVertex"] = 0
            first["vertexCount"] = 4
            sub_meshes[0] = first
    rd["m_SubMeshes"] = sub_meshes
    return True


def _polygon_area(points: list[tuple[float, float]]) -> float:
    if len(points) < 3:
        return 0.0
    s = 0.0
    n = len(points)
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        s += (x1 * y2) - (x2 * y1)
    return 0.5 * s


def _point_in_triangle(
    p: tuple[float, float],
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
) -> bool:
    def sign(p1: tuple[float, float], p2: tuple[float, float], p3: tuple[float, float]) -> float:
        return (p1[0] - p3[0]) * (p2[1] - p3[1]) - (p2[0] - p3[0]) * (p1[1] - p3[1])

    d1 = sign(p, a, b)
    d2 = sign(p, b, c)
    d3 = sign(p, c, a)

    eps = 1e-7
    has_neg = (d1 < -eps) or (d2 < -eps) or (d3 < -eps)
    has_pos = (d1 > eps) or (d2 > eps) or (d3 > eps)
    return not (has_neg and has_pos)


def _triangulate_polygon(points: list[tuple[float, float]]) -> list[int] | None:
    if len(points) < 3:
        return None

    poly: list[tuple[float, float]] = []
    for pt in points:
        if not poly or (abs(poly[-1][0] - pt[0]) > 1e-6 or abs(poly[-1][1] - pt[1]) > 1e-6):
            poly.append(pt)
    if len(poly) >= 2 and abs(poly[0][0] - poly[-1][0]) < 1e-6 and abs(poly[0][1] - poly[-1][1]) < 1e-6:
        poly.pop()
    if len(poly) < 3:
        return None

    area = _polygon_area(poly)
    is_ccw = area > 0.0

    indices = list(range(len(poly)))
    triangles: list[int] = []
    guard = 0
    max_guard = len(indices) * len(indices) * 4

    while len(indices) > 3 and guard < max_guard:
        ear_found = False
        m = len(indices)

        for i in range(m):
            i_prev = indices[(i - 1) % m]
            i_curr = indices[i]
            i_next = indices[(i + 1) % m]

            a = poly[i_prev]
            b = poly[i_curr]
            c = poly[i_next]
            cross = ((b[0] - a[0]) * (c[1] - a[1])) - ((b[1] - a[1]) * (c[0] - a[0]))
            if is_ccw:
                if cross <= 1e-8:
                    continue
            else:
                if cross >= -1e-8:
                    continue

            contains = False
            for j in indices:
                if j in (i_prev, i_curr, i_next):
                    continue
                if _point_in_triangle(poly[j], a, b, c):
                    contains = True
                    break
            if contains:
                continue

            if is_ccw:
                triangles.extend([i_prev, i_curr, i_next])
            else:
                triangles.extend([i_prev, i_next, i_curr])
            del indices[i]
            ear_found = True
            break

        if not ear_found:
            return None
        guard += 1

    if len(indices) == 3:
        i0, i1, i2 = indices
        if is_ccw:
            triangles.extend([i0, i1, i2])
        else:
            triangles.extend([i0, i2, i1])
    return triangles if triangles else None


def _decimate_polygon(
    points: list[tuple[float, float]],
    *,
    max_vertices: int,
) -> list[tuple[float, float]]:
    if len(points) <= max_vertices:
        return points
    step = len(points) / float(max_vertices)
    out: list[tuple[float, float]] = []
    used: set[int] = set()
    for i in range(max_vertices):
        idx = int(round(i * step))
        if idx >= len(points):
            idx = len(points) - 1
        if idx in used:
            continue
        used.add(idx)
        out.append(points[idx])
    if len(out) < 3:
        return points[:max_vertices]
    return out


def _normalize_ring(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    ring: list[tuple[float, float]] = []
    for x, y in points:
        fx = float(x)
        fy = float(y)
        if not ring or abs(ring[-1][0] - fx) > 1e-6 or abs(ring[-1][1] - fy) > 1e-6:
            ring.append((fx, fy))
    if len(ring) >= 2 and abs(ring[0][0] - ring[-1][0]) < 1e-6 and abs(ring[0][1] - ring[-1][1]) < 1e-6:
        ring.pop()
    return ring


def _extract_polygon_rings_from_alpha_cv2(
    alpha: Image.Image,
    *,
    threshold: int = 1,
    max_dim: int = 384,
    epsilon_ratio: float = 0.00025,
    max_vertices_per_ring: int = 256,
) -> list[tuple[list[tuple[float, float]], list[list[tuple[float, float]]]]]:
    if cv2 is None or np is None:
        return []
    if alpha.mode != "L":
        alpha = alpha.convert("L")

    src_w, src_h = alpha.size
    if src_w <= 0 or src_h <= 0:
        return []

    scale_x = 1.0
    scale_y = 1.0
    work = alpha
    if max(src_w, src_h) > max_dim:
        ratio = float(max_dim) / float(max(src_w, src_h))
        dst_w = max(1, int(round(src_w * ratio)))
        dst_h = max(1, int(round(src_h * ratio)))
        work = alpha.resize((dst_w, dst_h), Image.Resampling.BILINEAR)
        scale_x = float(src_w) / float(dst_w)
        scale_y = float(src_h) / float(dst_h)

    w, h = work.size
    arr = np.frombuffer(work.tobytes(), dtype=np.uint8).reshape((h, w))
    binary = np.where(arr > threshold, 255, 0).astype(np.uint8)

    found = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if len(found) == 2:
        contours, _ = found
    else:
        _, contours, _ = found
    if not contours:
        return []

    min_area = max(3.0, float(src_w * src_h) * 0.00002)

    def contour_to_ring(cnt: Any) -> list[tuple[float, float]] | None:
        if cnt is None or len(cnt) < 3:
            return None
        peri = float(cv2.arcLength(cnt, True))
        eps = max(0.05, peri * epsilon_ratio)
        approx = cv2.approxPolyDP(cnt, eps, True)
        used = approx if approx is not None and len(approx) >= 3 else cnt
        pts = [(float(p[0][0]) * scale_x, float(p[0][1]) * scale_y) for p in used]
        ring = _normalize_ring(pts)
        if len(ring) < 3:
            return None
        ring = _decimate_polygon(ring, max_vertices=max_vertices_per_ring)
        if len(ring) < 3:
            return None
        if abs(_polygon_area(ring)) < min_area:
            return None
        return ring

    groups: list[tuple[list[tuple[float, float]], list[list[tuple[float, float]]]]] = []
    for cnt in contours:
        outer = contour_to_ring(cnt)
        if outer is None:
            continue
        groups.append((outer, []))

    groups.sort(key=lambda g: abs(_polygon_area(g[0])), reverse=True)
    return groups


def _triangulate_rings_with_earcut(
    outer: list[tuple[float, float]],
    holes: list[list[tuple[float, float]]],
) -> tuple[list[tuple[float, float]], list[int]] | None:
    if np is None or earcut is None:
        return None

    rings: list[list[tuple[float, float]]] = []
    outer_n = _normalize_ring(outer)
    if len(outer_n) < 3:
        return None
    rings.append(outer_n)

    for hole in holes:
        hole_n = _normalize_ring(hole)
        if len(hole_n) >= 3:
            rings.append(hole_n)

    vertices: list[tuple[float, float]] = []
    ring_ends: list[int] = []
    for ring in rings:
        vertices.extend(ring)
        ring_ends.append(len(vertices))

    if len(vertices) < 3:
        return None

    try:
        verts_np = np.asarray(vertices, dtype=np.float64)
        ends_np = np.asarray(ring_ends, dtype=np.uint32)
        tri = earcut.triangulate_float64(verts_np, ends_np)
        tri_list = [int(x) for x in tri.tolist()]
    except Exception:
        return None

    if not tri_list:
        return None
    return vertices, tri_list


def _sprite_mesh_uv_is_zero(rd: dict[str, Any]) -> bool:
    vd = rd.get("m_VertexData")
    if not isinstance(vd, dict):
        return False
    try:
        vcount = int(vd.get("m_VertexCount", 0))
    except Exception:
        return False
    data = vd.get("m_DataSize")
    if not isinstance(data, (bytes, bytearray)):
        return False
    if vcount <= 0:
        return False

    pos_size = vcount * 12
    uv_off = (pos_size + 15) & ~15
    uv_size = vcount * 8
    if len(data) < uv_off + uv_size:
        return False

    sample_count = min(vcount, 4096)
    for i in range(sample_count):
        u, v = struct.unpack_from("<ff", data, uv_off + (i * 8))
        if abs(float(u)) > 1e-6 or abs(float(v)) > 1e-6:
            return False
    return True


def _extract_polygons_from_alpha(
    alpha: Image.Image,
    *,
    threshold: int = 8,
    max_dim: int = 192,
    max_vertices_per_polygon: int = 256,
) -> list[list[tuple[float, float]]]:
    if alpha.mode != "L":
        alpha = alpha.convert("L")
    src_w, src_h = alpha.size
    if src_w <= 0 or src_h <= 0:
        return []

    scale_x = 1.0
    scale_y = 1.0
    work = alpha
    if max(src_w, src_h) > max_dim:
        ratio = float(max_dim) / float(max(src_w, src_h))
        dst_w = max(1, int(round(src_w * ratio)))
        dst_h = max(1, int(round(src_h * ratio)))
        work = alpha.resize((dst_w, dst_h), Image.Resampling.BILINEAR)
        scale_x = float(src_w) / float(dst_w)
        scale_y = float(src_h) / float(dst_h)

    w, h = work.size
    buf = work.tobytes()

    def opaque(x: int, y: int) -> bool:
        return buf[y * w + x] > threshold

    edges: list[tuple[tuple[int, int], tuple[int, int]]] = []
    for y in range(h):
        for x in range(w):
            if not opaque(x, y):
                continue
            if y == 0 or not opaque(x, y - 1):
                edges.append(((x, y), (x + 1, y)))
            if x == w - 1 or not opaque(x + 1, y):
                edges.append(((x + 1, y), (x + 1, y + 1)))
            if y == h - 1 or not opaque(x, y + 1):
                edges.append(((x + 1, y + 1), (x, y + 1)))
            if x == 0 or not opaque(x - 1, y):
                edges.append(((x, y + 1), (x, y)))

    if not edges:
        return []

    next_map: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for start, end in edges:
        arr = next_map.setdefault(start, [])
        if end not in arr:
            arr.append(end)
    for arr in next_map.values():
        arr.sort()

    visited_edges: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    loops: list[list[tuple[int, int]]] = []
    total_edges = len(edges)
    for start, ends in next_map.items():
        for end in ends:
            edge0 = (start, end)
            if edge0 in visited_edges:
                continue

            loop: list[tuple[int, int]] = [start]
            curr = start
            nxt = end
            guard = 0
            max_guard = total_edges + 8
            while guard < max_guard:
                edge = (curr, nxt)
                if edge in visited_edges:
                    break
                visited_edges.add(edge)
                curr = nxt
                loop.append(curr)
                if curr == loop[0]:
                    break

                candidates = next_map.get(curr, [])
                next_unused: tuple[int, int] | None = None
                for cand in candidates:
                    if (curr, cand) not in visited_edges:
                        next_unused = cand
                        break
                if next_unused is None:
                    break
                nxt = next_unused
                guard += 1

            if len(loop) >= 4 and loop[0] == loop[-1]:
                loops.append(loop[:-1])

    if not loops:
        return []

    min_area = max(4.0, float(w * h) * 0.00005)
    polygons: list[list[tuple[float, float]]] = []
    for loop in loops:
        if len(loop) < 3:
            continue
        area = abs(_polygon_area([(float(px), float(py)) for px, py in loop]))
        if area < min_area:
            continue

        collapsed: list[tuple[int, int]] = []
        n = len(loop)
        for i in range(n):
            a = loop[(i - 1) % n]
            b = loop[i]
            c = loop[(i + 1) % n]
            cross = ((b[0] - a[0]) * (c[1] - b[1])) - ((b[1] - a[1]) * (c[0] - b[0]))
            if abs(float(cross)) <= 1e-9:
                continue
            collapsed.append(b)
        if len(collapsed) >= 3:
            loop = collapsed

        poly = [(float(px) * scale_x, float(py) * scale_y) for px, py in loop]
        poly = _decimate_polygon(poly, max_vertices=max_vertices_per_polygon)
        if len(poly) < 3:
            continue
        polygons.append(poly)

    polygons.sort(key=lambda pts: abs(_polygon_area(pts)), reverse=True)
    return polygons


def _force_tight_mesh_from_alpha(
    rd: dict[str, Any],
    alpha: Image.Image,
    *,
    bounds_override: tuple[float, float, float, float, float] | None = None,
) -> bool:
    bounds = bounds_override if bounds_override is not None else _read_mesh_bounds(rd)
    if bounds is None:
        return False
    min_x, max_x, min_y, max_y, z = bounds
    vd = rd.get("m_VertexData")
    if not isinstance(vd, dict):
        return False

    if alpha.mode != "L":
        alpha = alpha.convert("L")
    # tightmesh는 조금 넓게 잡아 누락 픽셀이 생기지 않도록 alpha를 확장합니다.
    alpha_mesh = alpha
    dilate_px = 5
    if dilate_px > 0:
        if cv2 is not None and np is not None:
            arr = np.frombuffer(alpha.tobytes(), dtype=np.uint8).reshape((alpha.height, alpha.width))
            binary = np.where(arr > 0, 255, 0).astype(np.uint8)
            kernel_size = (dilate_px * 2) + 1
            kernel = np.ones((kernel_size, kernel_size), dtype=np.uint8)
            binary = cv2.dilate(binary, kernel, iterations=1)
            alpha_mesh = Image.fromarray(binary, mode="L")
        else:
            kernel_size = (dilate_px * 2) + 1
            alpha_mesh = alpha.filter(ImageFilter.MaxFilter(kernel_size))

    tex_w, tex_h = alpha_mesh.size
    if tex_w <= 0 or tex_h <= 0:
        return False

    use_zero_uv = _sprite_mesh_uv_is_zero(rd)

    groups = _extract_polygon_rings_from_alpha_cv2(
        alpha_mesh,
        threshold=0,
        max_dim=384,
        epsilon_ratio=0.00025,
        max_vertices_per_ring=2048,
    )
    if not groups:
        # fallback without external deps
        polygons = _extract_polygons_from_alpha(alpha_mesh, threshold=0, max_dim=192, max_vertices_per_polygon=256)
        groups = [(poly, []) for poly in polygons]
    if not groups:
        return False

    span_x = max_x - min_x
    span_y = max_y - min_y
    tw = float(max(1, tex_w - 1))
    th = float(max(1, tex_h - 1))

    positions: list[tuple[float, float, float]] = []
    uvs: list[tuple[float, float]] = []
    triangles: list[int] = []
    for outer, holes in groups:
        tri_pack = _triangulate_rings_with_earcut(outer, holes)
        if tri_pack is not None:
            local_vertices2d, local_triangles = tri_pack
        else:
            local_vertices2d = _normalize_ring(outer)
            local_triangles = _triangulate_polygon(local_vertices2d)
            if local_triangles is None or len(local_triangles) < 3:
                continue

        local_positions: list[tuple[float, float, float]] = []
        local_uvs: list[tuple[float, float]] = []
        for px, py in local_vertices2d:
            nx = float(px) / tw
            ny = float(py) / th
            nx = min(max(nx, 0.0), 1.0)
            ny = min(max(ny, 0.0), 1.0)
            vx = min_x + (nx * span_x)
            vy = max_y - (ny * span_y)
            local_positions.append((vx, vy, z))
            if use_zero_uv:
                local_uvs.append((0.0, 0.0))
            else:
                local_uvs.append((nx, 1.0 - ny))

        if not local_positions:
            continue

        i0, i1, i2 = local_triangles[0], local_triangles[1], local_triangles[2]
        a = local_positions[i0]
        b = local_positions[i1]
        c = local_positions[i2]
        tri_cross = ((b[0] - a[0]) * (c[1] - a[1])) - ((b[1] - a[1]) * (c[0] - a[0]))
        if tri_cross > 0.0:
            for i in range(0, len(local_triangles), 3):
                local_triangles[i + 1], local_triangles[i + 2] = local_triangles[i + 2], local_triangles[i + 1]

        base_idx = len(positions)
        if base_idx + len(local_positions) > 65535:
            break
        positions.extend(local_positions)
        uvs.extend(local_uvs)
        for idx in local_triangles:
            triangles.append(base_idx + int(idx))

    if not positions or not triangles:
        return False

    # Keep sprite-local origin stable for UnityPy/runtime mask paths:
    # SpriteHelper normalizes mesh positions by global min(x/y) across all vertices.
    # If tight mesh vertices only cover opaque bbox, the mask is shifted.
    # Add 4 unreferenced anchors at textureRect bounds so min(x/y) == rect origin.
    if len(positions) + 4 <= 65535:
        if use_zero_uv:
            anchor_uvs = [(0.0, 0.0)] * 4
        else:
            anchor_uvs = [(0.0, 1.0), (0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]
        anchor_positions = [
            (min_x, min_y, z),
            (min_x, max_y, z),
            (max_x, max_y, z),
            (max_x, min_y, z),
        ]
        positions = anchor_positions + positions
        uvs = anchor_uvs + uvs
        triangles = [int(idx) + 4 for idx in triangles]

    vd["m_VertexCount"] = len(positions)
    vd["m_DataSize"] = _pack_vertex_streams_pos_uv(positions, uvs)
    rd["m_VertexData"] = vd

    idx_bytes = bytearray()
    for idx in triangles:
        idx_bytes.extend(struct.pack("<H", int(idx)))
    rd["m_IndexBuffer"] = list(idx_bytes)

    sub_meshes = rd.get("m_SubMeshes")
    if not isinstance(sub_meshes, list) or not sub_meshes:
        sub_meshes = [{
            "firstByte": 0,
            "indexCount": len(triangles),
            "topology": 0,
            "baseVertex": 0,
            "firstVertex": 0,
            "vertexCount": len(positions),
            "localAABB": {
                "m_Center": {"x": 0.0, "y": 0.0, "z": 0.0},
                "m_Extent": {"x": 0.0, "y": 0.0, "z": 0.0},
            },
        }]
    else:
        first = sub_meshes[0]
        if isinstance(first, dict):
            first["firstByte"] = 0
            first["indexCount"] = len(triangles)
            first["topology"] = int(first.get("topology", 0))
            first["baseVertex"] = 0
            first["firstVertex"] = 0
            first["vertexCount"] = len(positions)
            sub_meshes[0] = first
    rd["m_SubMeshes"] = sub_meshes
    return True


def apply_sprite_mode_and_rect(
    sprite_obj: Any,
    *,
    mode: Mode,
    tight_rect: tuple[int, int, int, int] | None = None,
) -> tuple[int, int]:
    tree = read_typetree_tolerant(sprite_obj)
    rd = cast(dict[str, Any], tree.get("m_RD", {}))
    before = int(rd.get("settingsRaw", 0))
    after = before

    # settingsRaw 비트:
    # bit1: packingMode(Rectangle=1, Tight=0)
    # bit6: meshType(FullRect=0, Tight=1)
    after = convert_settings_raw(after, mode)
    rd["settingsRaw"] = after

    if tight_rect is not None:
        x, y, w, h = tight_rect
        texture_rect = cast(dict[str, Any], rd.get("textureRect", {}))
        texture_rect["x"] = float(x)
        texture_rect["y"] = float(y)
        texture_rect["width"] = float(w)
        texture_rect["height"] = float(h)
        rd["textureRect"] = texture_rect

        texture_rect_offset = cast(dict[str, Any], rd.get("textureRectOffset", {}))
        if mode == "fullrect":
            texture_rect_offset["x"] = 0.0
            texture_rect_offset["y"] = 0.0
        else:
            texture_rect_offset["x"] = float(x)
            texture_rect_offset["y"] = float(y)
        rd["textureRectOffset"] = texture_rect_offset

    if mode in ("fullrect", "tightclip"):
        _force_quad_mesh_from_sprite_rect(rd, tree, rect_xywh=tight_rect)

    tree["m_RD"] = rd
    sprite_obj.save_typetree(tree)
    return before, after


def apply_tightmesh_mode_and_mesh(
    sprite_obj: Any,
    *,
    tight_rect: tuple[int, int, int, int],
    alpha: Image.Image,
) -> tuple[int, int, bool]:
    tree = read_typetree_tolerant(sprite_obj)
    rd = cast(dict[str, Any], tree.get("m_RD", {}))

    before = int(rd.get("settingsRaw", 0))
    after = convert_settings_raw(before, "tightmesh")
    rd["settingsRaw"] = after

    x, y, w, h = tight_rect
    texture_rect = cast(dict[str, Any], rd.get("textureRect", {}))
    texture_rect["x"] = float(x)
    texture_rect["y"] = float(y)
    texture_rect["width"] = float(w)
    texture_rect["height"] = float(h)
    rd["textureRect"] = texture_rect

    texture_rect_offset = cast(dict[str, Any], rd.get("textureRectOffset", {}))
    texture_rect_offset["x"] = float(x)
    texture_rect_offset["y"] = float(y)
    rd["textureRectOffset"] = texture_rect_offset

    z = 0.0
    prev_bounds = _read_mesh_bounds(rd)
    if prev_bounds is not None:
        z = prev_bounds[4]

    ppu_raw = tree.get("m_PixelsToUnits", 100.0)
    try:
        ppu = float(ppu_raw)
    except Exception:
        ppu = 100.0
    if ppu <= 0.0:
        ppu = 100.0

    pivot_raw = cast(dict[str, Any], tree.get("m_Pivot", {"x": 0.5, "y": 0.5}))
    try:
        pivot_x = float(pivot_raw.get("x", 0.5))
    except Exception:
        pivot_x = 0.5
    try:
        pivot_y = float(pivot_raw.get("y", 0.5))
    except Exception:
        pivot_y = 0.5

    # Some formats store pivot normalized [0..1], others in pixels.
    if pivot_x > 1.0:
        pivot_x = pivot_x / float(max(1, w))
    if pivot_y > 1.0:
        pivot_y = pivot_y / float(max(1, h))
    pivot_x = min(max(pivot_x, 0.0), 1.0)
    pivot_y = min(max(pivot_y, 0.0), 1.0)

    full_rect = cast(dict[str, Any], tree.get("m_Rect", {}))
    try:
        full_x = float(full_rect.get("x", 0.0))
    except Exception:
        full_x = 0.0
    try:
        full_y = float(full_rect.get("y", 0.0))
    except Exception:
        full_y = 0.0
    try:
        full_w = float(full_rect.get("width", float(w)))
    except Exception:
        full_w = float(w)
    try:
        full_h = float(full_rect.get("height", float(h)))
    except Exception:
        full_h = float(h)

    pivot_px = full_x + (full_w * pivot_x)
    pivot_py = full_y + (full_h * pivot_y)

    min_x = (float(x) - pivot_px) / ppu
    max_x = (float(x + w) - pivot_px) / ppu
    min_y = (float(y) - pivot_py) / ppu
    max_y = (float(y + h) - pivot_py) / ppu

    mesh_ok = _force_tight_mesh_from_alpha(
        rd,
        alpha,
        bounds_override=(min_x, max_x, min_y, max_y, z),
    )
    tree["m_RD"] = rd
    sprite_obj.save_typetree(tree)
    return before, after, mesh_ok


def update_atlas_settings(sprite: Any, mode: Mode) -> None:
    atlas_ptr = getattr(sprite, "m_SpriteAtlas", None)
    if not atlas_ptr:
        return
    if getattr(atlas_ptr, "m_FileID", 0) != 0 or getattr(atlas_ptr, "m_PathID", 0) == 0:
        return

    try:
        atlas = deref_parse_as_object_tolerant(atlas_ptr)
        render_map = getattr(atlas, "m_RenderDataMap", None)
        render_key = getattr(sprite, "m_RenderDataKey", None)
        if render_map is None or render_key is None or render_key not in render_map:
            return
        entry = render_map[render_key]
        raw = int(getattr(entry, "settingsRaw", 0))
        if mode == "fullrect":
            raw = (raw | (1 << 1)) & ~(1 << 6)
        else:
            raw = (raw & ~(1 << 1)) | (1 << 6)
        entry.settingsRaw = raw
        render_map[render_key] = entry
        atlas.save()
    except Exception:
        # atlas 맵 갱신 실패는 치명적이지 않으므로 무시
        return


def build_replacement_lookup(
    replacements: dict[str, JsonDict],
    *,
    json_base_dir: Path,
    default_mode: Mode,
) -> tuple[dict[tuple[str, str, int], JsonDict], dict[tuple[str, str, str], JsonDict], set[str]]:
    by_path_id: dict[tuple[str, str, int], JsonDict] = {}
    by_name: dict[tuple[str, str, str], JsonDict] = {}
    target_files: set[str] = set()

    for value in replacements.values():
        replace_raw = value.get("Replace_to")
        if not isinstance(replace_raw, str) or not replace_raw.strip():
            continue

        file_raw = value.get("File")
        assets_name_raw = value.get("assets_name")
        path_id_raw = value.get("Path_ID")
        name_raw = value.get("Name")
        if not isinstance(file_raw, str) or not file_raw.strip():
            continue
        if not isinstance(assets_name_raw, str) or not assets_name_raw.strip():
            continue

        replace_path = Path(replace_raw)
        if not replace_path.is_absolute():
            # 상대경로는 JSON 위치가 아닌 exe(또는 스크립트) 위치 기준으로 해석합니다.
            replace_path = (get_script_dir() / replace_path).resolve()

        mode = normalize_mode(value.get("Mode"), default=default_mode)
        normalized: JsonDict = {
            "replace_path": str(replace_path),
            "mode": mode,
            "name": str(name_raw) if isinstance(name_raw, str) else "",
        }
        for rect_key in ("TextureRect_X", "TextureRect_Y", "TextureRect_Width", "TextureRect_Height"):
            rect_val = value.get(rect_key)
            if isinstance(rect_val, (int, float)):
                normalized[rect_key] = int(round(float(rect_val)))
            elif isinstance(rect_val, str):
                try:
                    normalized[rect_key] = int(round(float(rect_val)))
                except ValueError:
                    pass

        file_lower = Path(file_raw).name.lower()
        assets_name = assets_name_raw
        target_files.add(file_lower)

        if isinstance(path_id_raw, int):
            by_path_id[(file_lower, assets_name, path_id_raw)] = normalized
        else:
            try:
                parsed = int(path_id_raw)
            except Exception:
                parsed = None
            if parsed is not None:
                by_path_id[(file_lower, assets_name, parsed)] = normalized

        if isinstance(name_raw, str) and name_raw:
            by_name[(file_lower, assets_name, name_raw)] = normalized

    return by_path_id, by_name, target_files


def replace_sprites_in_assets_file(
    assets_file: Path,
    *,
    by_path_id: dict[tuple[str, str, int], JsonDict],
    by_name: dict[tuple[str, str, str], JsonDict],
    by_filename: dict[str, JsonDict] | None,
    default_mode: Mode,
    skip_missing: bool,
    changed_only: bool,
) -> tuple[int, int, int]:
    try:
        env = UnityPy.load(str(assets_file))
    except Exception as e:
        print(f"[스킵] UnityPy.load 실패: {assets_file.name} ({e})")
        return 0, 0, 0
    file_lower = assets_file.name.lower()
    texture_cache: dict[int, tuple[Any, Any]] = {}  # path_id -> (texture_parsed, tex_obj_reader)

    replaced = 0
    skipped_missing = 0
    skipped_same = 0
    modified = False

    for obj in env.objects:
        if obj.type.name != "Sprite":
            continue
        sprite = read_object_tolerant(obj)
        path_id = int(obj.path_id)
        sprite_name = str(getattr(sprite, "m_Name", "") or "")
        assets_name = obj.assets_file.name

        entry = by_path_id.get((file_lower, assets_name, path_id))
        if entry is None:
            entry = by_name.get((file_lower, assets_name, sprite_name))
        if entry is None and by_filename is not None:
            # 1) extracted filename pattern match by file/pathid
            file_tag = sanitize_filename(assets_file.name).casefold()
            id_keys = (
                f"idfile:{assets_file.name.casefold()}:{path_id}",
                f"idfile:{file_tag}:{path_id}",
                f"idname:{path_id}:{sanitize_filename(sprite_name).casefold()}",
                f"idname:{path_id}:{sprite_name.casefold()}",
                f"id:{path_id}",
            )
            for k in id_keys:
                entry = by_filename.get(k)
                if entry is not None:
                    break
        if entry is None and by_filename is not None:
            # 2) fallback by sprite name tokens
            for token in (sprite_name.casefold(), sanitize_filename(sprite_name).casefold()):
                entry = by_filename.get(f"name:{token}") or by_filename.get(token)
                if entry is not None:
                    break
        if entry is None:
            continue

        replace_path = Path(cast(str, entry["replace_path"]))
        if not replace_path.exists():
            skipped_missing += 1
            if skip_missing:
                print(f"[스킵] 파일 없음: {replace_path}")
                continue
            raise FileNotFoundError(f"교체 파일을 찾을 수 없습니다: {replace_path}")

        replacement = Image.open(replace_path).convert("RGBA")
        target_mode = normalize_mode(entry.get("mode"), default_mode)

        texture_ptr = getattr(getattr(sprite, "m_RD", None), "texture", None)
        if texture_ptr is None:
            print(f"[스킵] texture 참조가 없습니다: {assets_file.name}:{path_id}:{sprite_name}")
            continue
        if getattr(texture_ptr, "m_FileID", 0) != 0:
            print(f"[스킵] 외부 texture 참조입니다: {assets_file.name}:{path_id}:{sprite_name}")
            continue

        texture_path_id = int(getattr(texture_ptr, "m_PathID", 0))
        if texture_path_id == 0:
            print(f"[스킵] 유효하지 않은 texture PathID: {assets_file.name}:{path_id}:{sprite_name}")
            continue

        cached = texture_cache.get(texture_path_id)
        texture = cached[0] if cached else None
        tex_obj_reader = cached[1] if cached else None
        if texture is None:
            for tex_obj in env.objects:
                if tex_obj.type.name == "Texture2D" and int(tex_obj.path_id) == texture_path_id:
                    texture = read_object_tolerant(tex_obj)
                    tex_obj_reader = tex_obj
                    texture_cache[texture_path_id] = (texture, tex_obj)
                    break
        if texture is None:
            print(f"[스킵] Texture2D를 찾을 수 없습니다: {assets_file.name}:{path_id}:{sprite_name}")
            continue

        try:
            texture_image = texture.image.convert("RGBA")
        except FileNotFoundError as e:
            print(f"[스킵] 리소스 파일 누락으로 Texture2D를 읽을 수 없습니다: {assets_file.name}:{path_id}:{sprite_name} ({e})")
            continue
        except Exception as e:
            print(f"[스킵] Texture2D 이미지 읽기 실패: {assets_file.name}:{path_id}:{sprite_name} ({e})")
            continue
        texture_rect = getattr(getattr(sprite, "m_RD", None), "textureRect", None)
        x = int(round(getattr(texture_rect, "x", 0.0)))
        y = int(round(getattr(texture_rect, "y", 0.0)))
        w = int(round(getattr(texture_rect, "width", 0.0)))
        h = int(round(getattr(texture_rect, "height", 0.0)))
        if w <= 0 or h <= 0:
            print(f"[스킵] textureRect 크기가 비정상입니다: {assets_file.name}:{path_id}:{sprite_name}")
            continue

        # JSON에 기준 textureRect가 있으면 이를 우선 사용해 반복 실행 시 누적 축소를 방지
        base_x = int(entry.get("TextureRect_X", x)) if isinstance(entry.get("TextureRect_X"), (int, float, str)) else x
        base_y = int(entry.get("TextureRect_Y", y)) if isinstance(entry.get("TextureRect_Y"), (int, float, str)) else y
        base_w = int(entry.get("TextureRect_Width", w)) if isinstance(entry.get("TextureRect_Width"), (int, float, str)) else w
        base_h = int(entry.get("TextureRect_Height", h)) if isinstance(entry.get("TextureRect_Height"), (int, float, str)) else h
        if base_w <= 0 or base_h <= 0:
            base_x, base_y, base_w, base_h = x, y, w, h
        base_y_top = texture_image.height - (base_y + base_h)

        tree_now = read_typetree_tolerant(obj)
        rd_now = cast(dict[str, Any], tree_now.get("m_RD", {}))
        raw_now = int(rd_now.get("settingsRaw", 0))
        rect_now = cast(dict[str, Any], rd_now.get("textureRect", {}))
        rect_now_x = int(round(float(rect_now.get("x", x))))
        rect_now_y = int(round(float(rect_now.get("y", y))))
        rect_now_w = int(round(float(rect_now.get("width", w))))
        rect_now_h = int(round(float(rect_now.get("height", h)))
        )
        m_rect_now = cast(dict[str, Any], tree_now.get("m_Rect", {}))
        full_x = int(round(float(m_rect_now.get("x", base_x))))
        full_y = int(round(float(m_rect_now.get("y", base_y))))
        full_w = int(round(float(m_rect_now.get("width", base_w))))
        full_h = int(round(float(m_rect_now.get("height", base_h))))
        if full_w <= 0 or full_h <= 0:
            full_x, full_y, full_w, full_h = base_x, base_y, base_w, base_h
        full_y_top = texture_image.height - (full_y + full_h)
        if full_x < 0 or full_y_top < 0 or (full_x + full_w) > texture_image.width or (full_y_top + full_h) > texture_image.height:
            full_x, full_y, full_w, full_h = base_x, base_y, base_w, base_h
            full_y_top = base_y_top

        if target_mode == "fullrect":
            # 기본은 현재 sprite rect를 기준으로 fullrect(직사각형 메쉬)로 전환합니다.
            # 단, tightclip 상태에서 full 캔버스 PNG를 넣은 경우 알파 bbox를 이용해
            # 원래 full rect 좌표를 역산해 범위까지 복원합니다.
            fullrect_x, fullrect_y, fullrect_w, fullrect_h = base_x, base_y, base_w, base_h
            write_x, write_y_top, write_w, write_h = base_x, base_y_top, base_w, base_h
            target_img = replacement if replacement.size == (base_w, base_h) else replacement.resize((base_w, base_h), Image.Resampling.LANCZOS)

            if replacement.size != (base_w, base_h):
                alpha_bbox = replacement.getchannel("A").getbbox()
                if alpha_bbox is not None:
                    bx0, by0, bx1, by1 = alpha_bbox
                    bbox_w = max(1, bx1 - bx0)
                    bbox_h = max(1, by1 - by0)
                    # tightclip rect(=base rect)와 replacement alpha bbox가 맞으면
                    # full canvas 좌표를 복원할 수 있습니다.
                    if abs(bbox_w - base_w) <= 1 and abs(bbox_h - base_h) <= 1:
                        cand_w, cand_h = replacement.size
                        cand_x = base_x - int(bx0)
                        cand_y = base_y - int(cand_h - by1)
                        cand_y_top = texture_image.height - (cand_y + cand_h)
                        if (
                            cand_w > 0
                            and cand_h > 0
                            and cand_x >= 0
                            and cand_y_top >= 0
                            and (cand_x + cand_w) <= texture_image.width
                            and (cand_y_top + cand_h) <= texture_image.height
                        ):
                            fullrect_x, fullrect_y, fullrect_w, fullrect_h = cand_x, cand_y, int(cand_w), int(cand_h)
                            write_x, write_y_top, write_w, write_h = cand_x, cand_y_top, int(cand_w), int(cand_h)
                            target_img = replacement

            if changed_only:
                current_crop = texture_image.crop((write_x, write_y_top, write_x + write_w, write_y_top + write_h))
                expected_raw = convert_settings_raw(raw_now, "fullrect")
                rect_ok = (rect_now_x, rect_now_y, rect_now_w, rect_now_h) == (fullrect_x, fullrect_y, fullrect_w, fullrect_h)
                mesh_ok = _is_quad_mesh(rd_now)
                if image_equal(current_crop, target_img) and raw_now == expected_raw and rect_ok and mesh_ok:
                    skipped_same += 1
                    continue

            # alpha 마스크 없이 덮어써야 sprite 결과가 원본 PNG와 일치합니다.
            texture_image.paste(target_img, (write_x, write_y_top))
            _save_texture_with_unitycn_fallback(texture, tex_obj_reader, texture_image)
            before, after = apply_sprite_mode_and_rect(obj, mode="fullrect", tight_rect=(fullrect_x, fullrect_y, fullrect_w, fullrect_h))
            update_atlas_settings(sprite, mode="fullrect")
        elif target_mode == "tightmesh":
            if replacement.size == (base_w, base_h):
                write_x, write_y_top, write_w, write_h = base_x, base_y_top, base_w, base_h
                fitted = replacement
            else:
                current_base_crop = texture_image.crop((base_x, base_y_top, base_x + base_w, base_y_top + base_h))
                current_bbox = current_base_crop.getchannel("A").getbbox()
                if current_bbox is not None:
                    cbx0, cby0, cbx1, cby1 = current_bbox
                    cbw = max(1, cbx1 - cbx0)
                    cbh = max(1, cby1 - cby0)
                    fitted = replacement if replacement.size == (cbw, cbh) else replacement.resize((cbw, cbh), Image.Resampling.LANCZOS)
                    write_x = base_x + cbx0
                    write_y_top = base_y_top + cby0
                    write_w, write_h = cbw, cbh
                else:
                    # Fallback: place replacement at base origin without upscaling to full base rect.
                    rw, rh = replacement.size
                    write_x, write_y_top = base_x, base_y_top
                    write_w = max(1, min(rw, texture_image.width - write_x))
                    write_h = max(1, min(rh, texture_image.height - write_y_top))
                    fitted = replacement if replacement.size == (write_w, write_h) else replacement.resize((write_w, write_h), Image.Resampling.LANCZOS)
            tight_y = texture_image.height - (write_y_top + write_h)

            if changed_only:
                current_crop = texture_image.crop((write_x, write_y_top, write_x + write_w, write_y_top + write_h))
                expected_raw = convert_settings_raw(raw_now, "tightmesh")
                rect_ok = (rect_now_x, rect_now_y, rect_now_w, rect_now_h) == (write_x, tight_y, write_w, write_h)
                mesh_ok = _is_tight_mesh(rd_now) and not _is_quad_mesh(rd_now)
                if image_equal(current_crop, fitted) and raw_now == expected_raw and rect_ok and mesh_ok:
                    skipped_same += 1
                    continue

            texture_image.paste((0, 0, 0, 0), (base_x, base_y_top, base_x + base_w, base_y_top + base_h))
            texture_image.paste(fitted, (write_x, write_y_top))
            _save_texture_with_unitycn_fallback(texture, tex_obj_reader, texture_image)

            before, after, mesh_ok = apply_tightmesh_mode_and_mesh(
                obj,
                tight_rect=(write_x, tight_y, write_w, write_h),
                alpha=fitted.getchannel("A"),
            )
            if not mesh_ok:
                print(f"[경고] tightmesh 생성 실패, 기존 메쉬를 유지합니다: {assets_file.name}:{path_id}:{sprite_name}")
            update_atlas_settings(sprite, mode="tightmesh")
        else:
            fitted = replacement if replacement.size == (base_w, base_h) else replacement.resize((base_w, base_h), Image.Resampling.LANCZOS)
            alpha = fitted.getchannel("A")
            bbox = alpha.getbbox()
            if bbox is None:
                bbox = (0, 0, base_w, base_h)

            bx0, by0, bx1, by1 = bbox
            clip_w = max(1, bx1 - bx0)
            clip_h = max(1, by1 - by0)
            clipped = fitted.crop((bx0, by0, bx1, by1))
            new_x = base_x + bx0
            new_y = base_y + (base_h - by1)  # textureRect.y는 bottom-origin
            new_y_top = base_y_top + by0

            if changed_only:
                current_crop = texture_image.crop((new_x, new_y_top, new_x + clip_w, new_y_top + clip_h))
                expected_raw = convert_settings_raw(raw_now, "tightclip")
                rect_ok = (rect_now_x, rect_now_y, rect_now_w, rect_now_h) == (new_x, new_y, clip_w, clip_h)
                if image_equal(current_crop, clipped) and raw_now == expected_raw and rect_ok:
                    skipped_same += 1
                    continue

            # 원 영역을 투명하게 지우고, bbox만 다시 기록
            texture_image.paste((0, 0, 0, 0), (base_x, base_y_top, base_x + base_w, base_y_top + base_h))
            texture_image.paste(clipped, (new_x, new_y_top))
            _save_texture_with_unitycn_fallback(texture, tex_obj_reader, texture_image)

            before, after = apply_sprite_mode_and_rect(
                obj,
                mode="tightclip",
                tight_rect=(new_x, new_y, clip_w, clip_h),
            )
            update_atlas_settings(sprite, mode="tightclip")

        print(f"[교체] {assets_file.name} | {sprite_name} | PathID={path_id} | Mode={target_mode} | settingsRaw {before}->{after}")
        replaced += 1
        modified = True

    if modified:
        assets_file.write_bytes(save_serialized_file_with_fallback(env.file))

    return replaced, skipped_missing, skipped_same


def replace_from_json(
    assets_files: list[Path],
    *,
    json_path: Path,
    default_mode: Mode,
    skip_missing: bool,
    changed_only: bool,
) -> tuple[int, int, int]:
    with json_path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError("JSON 루트는 객체(dict)여야 합니다.")

    replacements = cast(dict[str, JsonDict], payload)
    by_path_id, by_name, target_files = build_replacement_lookup(
        replacements,
        json_base_dir=json_path.parent.resolve(),
        default_mode=default_mode,
    )

    total_replaced = 0
    total_missing = 0
    total_same = 0

    for assets_file in assets_files:
        if assets_file.name.lower() not in target_files:
            continue
        replaced, missing, same = replace_sprites_in_assets_file(
            assets_file,
            by_path_id=by_path_id,
            by_name=by_name,
            by_filename=None,
            default_mode=default_mode,
            skip_missing=skip_missing,
            changed_only=changed_only,
        )
        total_replaced += replaced
        total_missing += missing
        total_same += same

    return total_replaced, total_missing, total_same


def replace_from_dir(
    assets_files: list[Path],
    *,
    replace_dir: Path,
    recursive: bool,
    default_mode: Mode,
    skip_missing: bool,
    changed_only: bool,
) -> tuple[int, int, int]:
    if not replace_dir.exists() or not replace_dir.is_dir():
        raise FileNotFoundError(f"교체 폴더를 찾을 수 없습니다: {replace_dir}")

    by_filename = build_filename_replacements(
        replace_dir,
        recursive=recursive,
        default_mode=default_mode,
    )
    if not by_filename:
        raise FileNotFoundError(f"교체할 PNG 파일을 찾을 수 없습니다: {replace_dir}")

    total_replaced = 0
    total_missing = 0
    total_same = 0

    for assets_file in assets_files:
        replaced, missing, same = replace_sprites_in_assets_file(
            assets_file,
            by_path_id={},
            by_name={},
            by_filename=by_filename,
            default_mode=default_mode,
            skip_missing=skip_missing,
            changed_only=changed_only,
        )
        total_replaced += replaced
        total_missing += missing
        total_same += same

    return total_replaced, total_missing, total_same


def ask_choice(prompt: str, valid: set[str]) -> str:
    while True:
        value = input(prompt).strip()
        if value in valid:
            return value
        print(f"[오류] 잘못된 입력입니다: {value}")


def main_cli(lang: Language = "ko") -> None:
    description = "Unity Sprite 교체/추출 도구 (fullrect + tightclip + tightmesh)"
    parser = argparse.ArgumentParser(
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  %(prog)s --gamepath "D:\\Games\\SomeGame" --parse
  %(prog)s --gamepath "D:\\Games\\SomeGame_Data\\sharedassets0.assets" --extract-all --ids "sharedassets0.assets:186"
  %(prog)s --gamepath "D:\\Games\\SomeGame" --list sprites.json --mode fullrect
  %(prog)s --gamepath "D:\\Games\\SomeGame" --list sprites.json --mode tightclip
  %(prog)s --gamepath "D:\\Games\\SomeGame" --list sprites.json --mode tightmesh
  %(prog)s --gamepath "D:\\Games\\SomeGame" --replace-dir ".\\sprites" --mode fullrect
        """,
    )
    parser.add_argument("--gamepath", type=str, help="게임 루트 / _Data / 단일 .assets 파일 경로")
    parser.add_argument("--parse", action="store_true", help="Sprite 메타 정보를 JSON으로 추출")
    parser.add_argument("--extract-all", action="store_true", help="Sprite PNG 전체(또는 필터 대상) 추출")
    parser.add_argument("--list", type=str, metavar="JSON_FILE", help="JSON 기반 Sprite 교체")
    parser.add_argument("--replace-dir", type=str, metavar="DIR", help="JSON 없이 파일명 기반 Sprite 교체 PNG 폴더")
    parser.add_argument("--replace-recursive", action="store_true", help="--replace-dir에서 하위 폴더까지 PNG 탐색")
    parser.add_argument("--ids", "--id", dest="ids", action="append", help="파일명:PathID 필터. 콤마로 여러 개 지정 가능")
    parser.add_argument("--name", "--names", dest="name", action="append", help="Sprite 이름 필터. 콤마로 여러 개 지정 가능")
    parser.add_argument("--name-contains", action="append", help="Sprite 이름 부분일치 필터. 콤마로 여러 개 지정 가능")
    parser.add_argument("--mode", choices=["fullrect", "tightclip", "tightmesh"], default="fullrect", help="교체 모드 기본값")
    parser.add_argument("--output-dir", type=str, help="추출 PNG 출력 폴더")
    parser.add_argument("--json-out", type=str, help="JSON 출력 파일 경로")
    parser.add_argument("--skip-missing", default=True, action=argparse.BooleanOptionalAction, help="없는 Replace_to 파일은 스킵 (기본: 켜짐)")
    parser.add_argument("--changed-only", default=True, action=argparse.BooleanOptionalAction, help="변경분만 반영 (기본: 켜짐)")
    parser.add_argument("--verbose", action="store_true", help="로그를 verbose.txt로 저장")
    args = parser.parse_args()

    # normalize path-like args to tolerate pasted quoted paths
    if args.gamepath:
        args.gamepath = normalize_user_path_input(args.gamepath)
    if args.list:
        args.list = normalize_user_path_input(args.list)
    if args.replace_dir:
        args.replace_dir = normalize_user_path_input(args.replace_dir)
    if args.output_dir:
        args.output_dir = normalize_user_path_input(args.output_dir)
    if args.json_out:
        args.json_out = normalize_user_path_input(args.json_out)

    warn_unitypy_version()

    verbose_file: io.TextIOBase | None = None
    if args.verbose:
        verbose_path = get_script_dir() / "verbose.txt"
        verbose_file = open(verbose_path, "w", encoding="utf-8")
        original_stdout = sys.__stdout__
        original_stderr = sys.__stderr__
        if original_stdout is None or original_stderr is None:
            exit_with_error("표준 출력 스트림을 사용할 수 없습니다.")
        sys.stdout = TeeWriter(verbose_file, original_stdout)
        sys.stderr = TeeWriter(verbose_file, original_stderr)
        print(f"[verbose] 로그 저장: {verbose_path}")

    input_path = args.gamepath
    interactive_gamepath = not bool(input_path)
    while True:
        if not input_path:
            input_path = normalize_user_path_input(input("게임 경로(_Data/루트/.assets)를 입력하세요: "))
        if not input_path:
            if interactive_gamepath:
                print("[오류] 경로 입력이 필요합니다.")
                continue
            exit_with_error("경로 입력이 필요합니다.")

        try:
            game_path, data_path, assets_files = resolve_input_path(input_path)
            break
        except FileNotFoundError as e:
            if interactive_gamepath:
                print(f"[오류] {e}")
                input_path = ""
                continue
            exit_with_error(str(e))

    print(f"[정보] Game Path: {game_path}")
    print(f"[정보] Data Path: {data_path}")
    print(f"[정보] Assets 파일 수: {len(assets_files)}")

    id_filters = parse_id_filters(args.ids)
    names, names_fold = parse_name_filters(args.name)
    contains_tokens = parse_name_contains(args.name_contains)

    default_mode = cast(Mode, args.mode)

    mode_parse = args.parse
    mode_extract = args.extract_all
    mode_replace_json = bool(args.list)
    mode_replace_dir = bool(args.replace_dir)
    interactive_json_path_prompt = False
    interactive_replace_dir_prompt = False
    if mode_replace_json and mode_replace_dir:
        exit_with_error("--list 와 --replace-dir 는 동시에 사용할 수 없습니다.")

    if not mode_parse and not mode_extract and not mode_replace_json and not mode_replace_dir:
        print("작업을 선택하세요:")
        print("  1. Sprite 정보 추출 (JSON)")
        print("  2. JSON 기반 Sprite 교체")
        print("  3. Sprite 추출 (PNG + JSON)")
        print("  4. 파일명 기반 Sprite 교체 (JSON 없이)")
        choice = ask_choice("선택 (1-4): ", {"1", "2", "3", "4"})
        if choice == "1":
            mode_parse = True
        elif choice == "2":
            mode_replace_json = True
            interactive_json_path_prompt = True
            while True:
                args.list = normalize_user_path_input(input("JSON 파일 경로를 입력하세요: "))
                if not args.list:
                    print("[오류] JSON 파일 경로가 필요합니다.")
                    continue
                json_check = Path(args.list).expanduser().resolve()
                if not json_check.exists() or not json_check.is_file():
                    print(f"[오류] JSON 파일을 찾을 수 없습니다: {json_check}")
                    continue
                args.list = str(json_check)
                break
        elif choice == "3":
            mode_extract = True
        else:
            mode_replace_dir = True
            interactive_replace_dir_prompt = True
            while True:
                args.replace_dir = normalize_user_path_input(input("교체 PNG 폴더 경로를 입력하세요: "))
                if not args.replace_dir:
                    print("[오류] 교체 폴더 경로가 필요합니다.")
                    continue
                dir_check = Path(args.replace_dir).expanduser().resolve()
                if not dir_check.exists() or not dir_check.is_dir():
                    print(f"[오류] 교체 폴더를 찾을 수 없습니다: {dir_check}")
                    continue
                args.replace_dir = str(dir_check)
                break

    script_dir = get_script_dir()
    game_tag = sanitize_filename(game_path.name if game_path.name else "unity_game")
    default_json_out = script_dir / f"{game_tag}_sprites.json"
    json_out = Path(args.json_out).expanduser().resolve() if args.json_out else default_json_out

    if mode_parse:
        count = parse_sprites_to_json(
            assets_files,
            json_out=json_out,
            id_filters=id_filters,
            names=names,
            names_fold=names_fold,
            contains_tokens=contains_tokens,
            default_mode=default_mode,
        )
        print(f"[완료] 추출된 Sprite 메타 수: {count}")

    if mode_extract:
        output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else (script_dir / f"{game_tag}_sprites")
        count = extract_sprites(
            assets_files,
            output_dir=output_dir,
            json_out=json_out,
            id_filters=id_filters,
            names=names,
            names_fold=names_fold,
            contains_tokens=contains_tokens,
            default_mode=default_mode,
        )
        print(f"[완료] 추출된 Sprite 수: {count}")

    if mode_replace_json:
        if not args.list:
            if not interactive_json_path_prompt:
                exit_with_error("--list JSON_FILE 이 필요합니다.")
            while True:
                args.list = normalize_user_path_input(input("JSON 파일 경로를 입력하세요: "))
                if not args.list:
                    print("[오류] JSON 파일 경로가 필요합니다.")
                    continue
                json_try = Path(args.list).expanduser().resolve()
                if not json_try.exists() or not json_try.is_file():
                    print(f"[오류] JSON 파일을 찾을 수 없습니다: {json_try}")
                    continue
                args.list = str(json_try)
                break
        json_path = Path(normalize_user_path_input(args.list)).expanduser().resolve()
        while (not json_path.exists() or not json_path.is_file()) and interactive_json_path_prompt:
            print(f"[오류] JSON 파일을 찾을 수 없습니다: {json_path}")
            args.list = normalize_user_path_input(input("JSON 파일 경로를 다시 입력하세요: "))
            if not args.list:
                print("[오류] JSON 파일 경로가 필요합니다.")
                continue
            json_path = Path(args.list).expanduser().resolve()
        if not json_path.exists() or not json_path.is_file():
            exit_with_error(f"JSON 파일을 찾을 수 없습니다: {json_path}")
        replaced, missing, same = replace_from_json(
            assets_files,
            json_path=json_path,
            default_mode=default_mode,
            skip_missing=args.skip_missing,
            changed_only=args.changed_only,
        )
        print(f"[완료] 교체 수: {replaced}, 누락 스킵: {missing}, 동일 이미지 스킵: {same}")

    if mode_replace_dir:
        if not args.replace_dir:
            if not interactive_replace_dir_prompt:
                exit_with_error("--replace-dir DIR 이 필요합니다.")
            while True:
                args.replace_dir = normalize_user_path_input(input("교체 PNG 폴더 경로를 입력하세요: "))
                if not args.replace_dir:
                    print("[오류] 교체 폴더 경로가 필요합니다.")
                    continue
                replace_try = Path(args.replace_dir).expanduser().resolve()
                if not replace_try.exists() or not replace_try.is_dir():
                    print(f"[오류] 교체 폴더를 찾을 수 없습니다: {replace_try}")
                    continue
                args.replace_dir = str(replace_try)
                break
        replace_dir = Path(normalize_user_path_input(args.replace_dir)).expanduser().resolve()
        while (not replace_dir.exists() or not replace_dir.is_dir()) and interactive_replace_dir_prompt:
            print(f"[오류] 교체 폴더를 찾을 수 없습니다: {replace_dir}")
            args.replace_dir = normalize_user_path_input(input("교체 PNG 폴더 경로를 다시 입력하세요: "))
            if not args.replace_dir:
                print("[오류] 교체 폴더 경로가 필요합니다.")
                continue
            replace_dir = Path(args.replace_dir).expanduser().resolve()
        if not replace_dir.exists() or not replace_dir.is_dir():
            exit_with_error(f"교체 폴더를 찾을 수 없습니다: {replace_dir}")
        replaced, missing, same = replace_from_dir(
            assets_files,
            replace_dir=replace_dir,
            recursive=args.replace_recursive,
            default_mode=default_mode,
            skip_missing=args.skip_missing,
            changed_only=args.changed_only,
        )
        print(f"[완료] 교체 수: {replaced}, 누락 스킵: {missing}, 동일 이미지 스킵: {same}")

    if verbose_file is not None:
        verbose_file.flush()


def _restore_tee_streams() -> None:
    if isinstance(sys.stdout, TeeWriter):
        sys.stdout.file.close()
        sys.stdout = sys.__stdout__
    if isinstance(sys.stderr, TeeWriter):
        sys.stderr.file.close()
        sys.stderr = sys.__stderr__


def run_main_ko() -> None:
    try:
        main_cli(lang="ko")
    except Exception as e:
        print(f"\n예상치 못한 오류가 발생했습니다: {e}")
        tb_module.print_exc()
        sys.exit(1)
    finally:
        _restore_tee_streams()


def run_main_en() -> None:
    try:
        main_cli(lang="en")
    except Exception as e:
        print(f"\nAn unexpected error occurred: {e}")
        tb_module.print_exc()
        sys.exit(1)
    finally:
        _restore_tee_streams()


def main() -> None:
    run_main_ko()


if __name__ == "__main__":
    run_main_ko()
