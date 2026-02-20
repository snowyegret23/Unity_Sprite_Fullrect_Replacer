from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import traceback as tb_module
from pathlib import Path
from typing import Any, Literal, NoReturn, cast

import UnityPy
from PIL import Image, ImageChops


Language = Literal["ko", "en"]
JsonDict = dict[str, Any]
Mode = Literal["fullrect", "tightclip"]


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
    # 기본은 _Data 최상위의 표준 Unity assets 집합을 사용합니다.
    # (예: Original/backup 폴더의 복사본까지 재귀로 잡히는 것을 방지)
    top_level = [p for p in data_path.glob("*.assets") if p.is_file()]
    top_level.sort()
    if top_level:
        return top_level

    # 예외적으로 최상위에 없을 때만 재귀 탐색합니다.
    assets_files = [p for p in data_path.rglob("*.assets") if p.is_file()]
    assets_files.sort()
    return assets_files


def resolve_input_path(input_path: str) -> tuple[Path, Path, list[Path]]:
    p = Path(input_path).expanduser().resolve()

    # 단일 assets 파일 입력 허용
    if p.is_file():
        if p.suffix.lower() != ".assets":
            raise FileNotFoundError(f"지원하지 않는 파일 형식입니다: {p}")
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
        raise FileNotFoundError(f"에셋 파일(.assets)을 찾을 수 없습니다: {data_path}")
    return game_path, data_path, assets_files


def sanitize_filename(name: str) -> str:
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name.strip())
    sanitized = re.sub(r"\s+", " ", sanitized).strip()
    return sanitized or "unnamed"


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
    return default


def normalize_name_filter_token(raw: str) -> str:
    token = raw.strip()
    lower = token.lower()
    if lower.endswith(".sprite.png"):
        return token[: -len(".sprite.png")]
    if lower.endswith(".png"):
        return token[: -len(".png")]
    return token


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
        env = UnityPy.load(str(assets_file))
        file_name = assets_file.name
        found_in_file = 0

        for obj in env.objects:
            if obj.type.name != "Sprite":
                continue
            sprite = obj.read()
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

    for assets_file in assets_files:
        env = UnityPy.load(str(assets_file))
        file_name = assets_file.name
        file_count = 0

        for obj in env.objects:
            if obj.type.name != "Sprite":
                continue
            sprite = obj.read()
            sprite_name = str(getattr(sprite, "m_Name", "") or "")
            path_id = int(obj.path_id)
            if not sprite_matches_filters(file_name, path_id, sprite_name, id_filters, names, names_fold, contains_tokens):
                continue

            image = sprite.image.convert("RGBA")
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

        if file_count:
            print(f"[정보] {assets_file.name}: Sprite {file_count}개 추출")

    if json_out:
        write_json(json_out, records)
        print(f"[완료] 추출 JSON 저장: {json_out}")

    print(f"[완료] Sprite PNG 추출 수: {exported}")
    return exported


def save_serialized_file_with_fallback(serialized_file: Any) -> bytes:
    errors: list[Exception] = []
    for packer in ("original", "lz4", None):
        try:
            return cast(bytes, serialized_file.save(packer=packer))
        except Exception as e:  # pragma: no cover - 환경별 저장 포맷 대응
            errors.append(e)
    joined = "; ".join(str(e) for e in errors)
    raise RuntimeError(f"에셋 저장 실패: {joined}")


def image_equal(a: Image.Image, b: Image.Image) -> bool:
    if a.size != b.size:
        return False
    return ImageChops.difference(a, b).getbbox() is None


def convert_settings_raw(raw: int, mode: Mode) -> int:
    if mode == "fullrect":
        return (raw | (1 << 1)) & ~(1 << 6)
    return (raw & ~(1 << 1)) | (1 << 6)


def apply_sprite_mode_and_rect(
    sprite_obj: Any,
    *,
    mode: Mode,
    tight_rect: tuple[int, int, int, int] | None = None,
) -> tuple[int, int]:
    tree = sprite_obj.read_typetree()
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
        texture_rect_offset["x"] = float(x)
        texture_rect_offset["y"] = float(y)
        rd["textureRectOffset"] = texture_rect_offset

    tree["m_RD"] = rd
    sprite_obj.save_typetree(tree)
    return before, after


def update_atlas_settings(sprite: Any, mode: Mode) -> None:
    atlas_ptr = getattr(sprite, "m_SpriteAtlas", None)
    if not atlas_ptr:
        return
    if getattr(atlas_ptr, "m_FileID", 0) != 0 or getattr(atlas_ptr, "m_PathID", 0) == 0:
        return

    try:
        atlas = atlas_ptr.read()
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
            replace_path = (json_base_dir / replace_path).resolve()

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
    default_mode: Mode,
    skip_missing: bool,
    changed_only: bool,
) -> tuple[int, int, int]:
    env = UnityPy.load(str(assets_file))
    file_lower = assets_file.name.lower()
    texture_cache: dict[int, Any] = {}

    replaced = 0
    skipped_missing = 0
    skipped_same = 0
    modified = False

    for obj in env.objects:
        if obj.type.name != "Sprite":
            continue
        sprite = obj.read()
        path_id = int(obj.path_id)
        sprite_name = str(getattr(sprite, "m_Name", "") or "")
        assets_name = obj.assets_file.name

        entry = by_path_id.get((file_lower, assets_name, path_id))
        if entry is None:
            entry = by_name.get((file_lower, assets_name, sprite_name))
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

        texture = texture_cache.get(texture_path_id)
        if texture is None:
            for tex_obj in env.objects:
                if tex_obj.type.name == "Texture2D" and int(tex_obj.path_id) == texture_path_id:
                    texture = tex_obj.read()
                    texture_cache[texture_path_id] = texture
                    break
        if texture is None:
            print(f"[스킵] Texture2D를 찾을 수 없습니다: {assets_file.name}:{path_id}:{sprite_name}")
            continue

        try:
            texture_image = texture.image.convert("RGBA")
        except FileNotFoundError as e:
            print(f"[스킵] 리소스 파일 누락으로 Texture2D를 읽을 수 없습니다: {assets_file.name}:{path_id}:{sprite_name} ({e})")
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

        tree_now = obj.read_typetree()
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
            # fullrect는 m_Rect 기준으로 metadata를 맞추고, 실제 픽셀 기록은 입력 이미지 크기에 따라 안전하게 처리합니다.
            # - 입력이 m_Rect와 동일하면 m_Rect 전체를 덮어씁니다.
            # - 입력이 기존 textureRect와 동일하면 기존 영역만 덮어씁니다(왜곡 방지).
            if replacement.size == (full_w, full_h):
                write_x, write_y_top, write_w, write_h = full_x, full_y_top, full_w, full_h
                target_img = replacement
            else:
                write_x, write_y_top, write_w, write_h = base_x, base_y_top, base_w, base_h
                target_img = replacement if replacement.size == (base_w, base_h) else replacement.resize((base_w, base_h), Image.Resampling.LANCZOS)

            if changed_only:
                current_crop = texture_image.crop((write_x, write_y_top, write_x + write_w, write_y_top + write_h))
                expected_raw = convert_settings_raw(raw_now, "fullrect")
                rect_ok = (rect_now_x, rect_now_y, rect_now_w, rect_now_h) == (full_x, full_y, full_w, full_h)
                if image_equal(current_crop, target_img) and raw_now == expected_raw and rect_ok:
                    skipped_same += 1
                    continue

            # alpha 마스크 없이 덮어써야 sprite 결과가 원본 PNG와 일치합니다.
            texture_image.paste(target_img, (write_x, write_y_top))
            texture.set_image(texture_image)
            texture.save()
            before, after = apply_sprite_mode_and_rect(obj, mode="fullrect", tight_rect=(full_x, full_y, full_w, full_h))
            update_atlas_settings(sprite, mode="fullrect")
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
            texture.set_image(texture_image)
            texture.save()

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
    description = "Unity Sprite 교체/추출 도구 (fullrect + tightclip)"
    parser = argparse.ArgumentParser(
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  %(prog)s --gamepath "D:\\Games\\SomeGame" --parse
  %(prog)s --gamepath "D:\\Games\\SomeGame_Data\\sharedassets0.assets" --extract-all --ids "sharedassets0.assets:186"
  %(prog)s --gamepath "D:\\Games\\SomeGame" --list sprites.json --mode fullrect
  %(prog)s --gamepath "D:\\Games\\SomeGame" --list sprites.json --mode tightclip
        """,
    )
    parser.add_argument("--gamepath", type=str, help="게임 루트 / _Data / 단일 .assets 파일 경로")
    parser.add_argument("--parse", action="store_true", help="Sprite 메타 정보를 JSON으로 추출")
    parser.add_argument("--extract-all", action="store_true", help="Sprite PNG 전체(또는 필터 대상) 추출")
    parser.add_argument("--list", type=str, metavar="JSON_FILE", help="JSON 기반 Sprite 교체")
    parser.add_argument("--ids", action="append", help="파일명:PathID 필터. 콤마로 여러 개 지정 가능")
    parser.add_argument("--name", "--names", dest="name", action="append", help="Sprite 이름 필터. 콤마로 여러 개 지정 가능")
    parser.add_argument("--name-contains", action="append", help="Sprite 이름 부분일치 필터. 콤마로 여러 개 지정 가능")
    parser.add_argument("--mode", choices=["fullrect", "tightclip"], default="fullrect", help="교체 모드 기본값")
    parser.add_argument("--output-dir", type=str, help="추출 PNG 출력 폴더")
    parser.add_argument("--json-out", type=str, help="JSON 출력 파일 경로")
    parser.add_argument("--skip-missing", dest="skip_missing", action="store_true", default=True, help="없는 Replace_to 파일은 스킵")
    parser.add_argument("--no-skip-missing", dest="skip_missing", action="store_false", help="없는 Replace_to 파일 발견 시 오류")
    parser.add_argument("--changed-only", dest="changed_only", action="store_true", default=True, help="변경분만 반영")
    parser.add_argument("--no-changed-only", dest="changed_only", action="store_false", help="동일 이미지도 강제 재기록")
    parser.add_argument("--verbose", action="store_true", help="로그를 verbose.txt로 저장")
    args = parser.parse_args()

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
    if not input_path:
        input_path = input("게임 경로(_Data/루트/.assets)를 입력하세요: ").strip()
    if not input_path:
        exit_with_error("경로 입력이 필요합니다.")

    try:
        game_path, data_path, assets_files = resolve_input_path(input_path)
    except FileNotFoundError as e:
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
    mode_replace = bool(args.list)
    if not mode_parse and not mode_extract and not mode_replace:
        print("작업을 선택하세요:")
        print("  1. Sprite 정보 추출 (JSON)")
        print("  2. JSON 기반 Sprite 교체")
        print("  3. Sprite 추출 (PNG + JSON)")
        choice = ask_choice("선택 (1-3): ", {"1", "2", "3"})
        if choice == "1":
            mode_parse = True
        elif choice == "2":
            mode_replace = True
            args.list = input("JSON 파일 경로를 입력하세요: ").strip()
            if not args.list:
                exit_with_error("JSON 파일 경로가 필요합니다.")
        else:
            mode_extract = True

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

    if mode_replace:
        if not args.list:
            exit_with_error("--list JSON_FILE 이 필요합니다.")
        json_path = Path(args.list).expanduser().resolve()
        if not json_path.exists():
            exit_with_error(f"JSON 파일을 찾을 수 없습니다: {json_path}")
        replaced, missing, same = replace_from_json(
            assets_files,
            json_path=json_path,
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
