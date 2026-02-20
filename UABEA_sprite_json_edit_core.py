from __future__ import annotations

import argparse
import json
import sys
import traceback as tb_module
from pathlib import Path
from typing import Any, Literal


Language = Literal["ko", "en"]


def t(lang: Language, key: str, **kwargs: Any) -> str:
    ko: dict[str, str] = {
        "desc": (
            "UABEA Sprite JSON을 FullRect로 변환합니다.\n"
            "- 파일 인자를 주면 '<이름>.fullrect.json' 파일을 생성합니다.\n"
            "- 파일 인자 없이 실행하면 '.fullrect.json' 제외 JSON을 직접 수정합니다."
        ),
        "inputs_help": "변환할 JSON 파일 경로(여러 개 가능)",
        "dir_help": "배치 대상 폴더(기본: 현재 폴더)",
        "recursive_help": "하위 폴더까지 재귀 탐색",
        "no_expand_help": "textureRect를 m_Rect 전체로 확장하지 않음 (기본은 확장)",
        "err_no_dir": "[오류] 폴더를 찾을 수 없습니다: {path}",
        "done_no_targets": "[완료] 처리할 JSON 파일이 없습니다.",
        "err_no_file": "[오류] 파일 없음: {path}",
        "err_file": "[오류] {name}: {error}",
        "skip_non_sprite": "[스킵] {name}: Sprite JSON 아님",
        "skip_already": "[스킵] {name}: 이미 FullRect",
        "gen_already": "[생성] {name}: 이미 FullRect (내용 복사)",
        "gen_modified": "[생성] {name}: {msg}",
        "mod_modified": "[수정] {name}: {msg}",
        "err_unknown_state": "[오류] {name}: 알 수 없는 상태({status})",
        "summary_inputs": (
            "[완료] 총 {total}개 | 생성 {generated} | 실제 수정 {modified} | 이미 FullRect {skipped_full} | "
            "Sprite 아님 {skipped_non_sprite} | 오류 {errors}"
        ),
        "summary_batch": (
            "[완료] 총 {total}개 | 수정 {modified} | 이미 FullRect {skipped_full} | "
            "Sprite 아님 {skipped_non_sprite} | 오류 {errors}"
        ),
        "err_not_sprite_mrd": "유효한 Sprite JSON이 아닙니다. 'm_RD' 키를 찾지 못했습니다.",
        "err_not_sprite_raw": "유효한 Sprite JSON이 아닙니다. 'm_RD.settingsRaw' 키를 찾지 못했습니다.",
        "err_raw_cast": "'settingsRaw' 값을 정수로 변환할 수 없습니다: {value!r}",
        "modified_base": "modified settingsRaw {before}->{after}",
        "modified_polygon": "m_IsPolygon=false",
        "modified_rect": "textureRect=m_Rect",
        "unexpected": "\n예상치 못한 오류가 발생했습니다: {error}",
    }

    en: dict[str, str] = {
        "desc": (
            "Convert UABEA Sprite JSON to FullRect.\n"
            "- With input file(s): create '<name>.fullrect.json'.\n"
            "- Without input: modify JSON files in-place except '.fullrect.json'."
        ),
        "inputs_help": "JSON file paths to convert (multiple allowed)",
        "dir_help": "Target directory for batch mode (default: current directory)",
        "recursive_help": "Search recursively in subdirectories",
        "no_expand_help": "Do not expand textureRect to m_Rect (default: expand)",
        "err_no_dir": "[Error] Directory not found: {path}",
        "done_no_targets": "[Done] No JSON files to process.",
        "err_no_file": "[Error] File not found: {path}",
        "err_file": "[Error] {name}: {error}",
        "skip_non_sprite": "[Skip] {name}: Not a Sprite JSON",
        "skip_already": "[Skip] {name}: Already FullRect",
        "gen_already": "[Created] {name}: Already FullRect (copied content)",
        "gen_modified": "[Created] {name}: {msg}",
        "mod_modified": "[Modified] {name}: {msg}",
        "err_unknown_state": "[Error] {name}: Unknown status ({status})",
        "summary_inputs": (
            "[Done] Total {total} | Created {generated} | Actually modified {modified} | Already FullRect {skipped_full} | "
            "Not Sprite {skipped_non_sprite} | Errors {errors}"
        ),
        "summary_batch": (
            "[Done] Total {total} | Modified {modified} | Already FullRect {skipped_full} | "
            "Not Sprite {skipped_non_sprite} | Errors {errors}"
        ),
        "err_not_sprite_mrd": "Invalid Sprite JSON: missing 'm_RD'.",
        "err_not_sprite_raw": "Invalid Sprite JSON: missing 'm_RD.settingsRaw'.",
        "err_raw_cast": "Could not parse integer from 'settingsRaw': {value!r}",
        "modified_base": "modified settingsRaw {before}->{after}",
        "modified_polygon": "m_IsPolygon=false",
        "modified_rect": "textureRect=m_Rect",
        "unexpected": "\nAn unexpected error occurred: {error}",
    }

    table = ko if lang == "ko" else en
    return table[key].format(**kwargs)


def to_fullrect_settings_raw(raw: int) -> int:
    # bit1: packingMode (Rectangle=1, Tight=0)
    # bit6: meshType   (FullRect=0, Tight=1)
    return (raw | (1 << 1)) & ~(1 << 6)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _is_close(a: float, b: float, eps: float = 1e-4) -> bool:
    return abs(a - b) <= eps


def _is_fullrect_already(data: dict[str, Any], *, require_rect_expand: bool = True) -> bool:
    rd = data.get("m_RD")
    if not isinstance(rd, dict):
        return False

    raw = rd.get("settingsRaw")
    try:
        raw_int = int(raw)
    except Exception:
        return False

    if raw_int != to_fullrect_settings_raw(raw_int):
        return False
    if data.get("m_IsPolygon") is True:
        return False
    if not require_rect_expand:
        return True

    m_rect = data.get("m_Rect")
    tex_rect = rd.get("textureRect")
    tex_off = rd.get("textureRectOffset")
    if not isinstance(m_rect, dict) or not isinstance(tex_rect, dict):
        return False

    x = _to_float(m_rect.get("x"))
    y = _to_float(m_rect.get("y"))
    w = _to_float(m_rect.get("width"))
    h = _to_float(m_rect.get("height"))

    tx = _to_float(tex_rect.get("x"))
    ty = _to_float(tex_rect.get("y"))
    tw = _to_float(tex_rect.get("width"))
    th = _to_float(tex_rect.get("height"))

    rect_ok = _is_close(x, tx) and _is_close(y, ty) and _is_close(w, tw) and _is_close(h, th)
    if not rect_ok:
        return False

    if isinstance(tex_off, dict):
        ox = _to_float(tex_off.get("x"))
        oy = _to_float(tex_off.get("y"))
        if not (_is_close(x, ox) and _is_close(y, oy)):
            return False

    return True


def patch_sprite_json(data: dict[str, Any], *, expand_to_m_rect: bool = True, lang: Language = "ko") -> tuple[int, int, bool, bool]:
    if "m_RD" not in data or not isinstance(data["m_RD"], dict):
        raise ValueError(t(lang, "err_not_sprite_mrd"))

    rd = data["m_RD"]
    if "settingsRaw" not in rd:
        raise ValueError(t(lang, "err_not_sprite_raw"))

    try:
        before_raw = int(rd["settingsRaw"])
    except Exception as exc:
        raise ValueError(t(lang, "err_raw_cast", value=rd["settingsRaw"])) from exc

    after_raw = to_fullrect_settings_raw(before_raw)
    rd["settingsRaw"] = after_raw

    changed_polygon = False
    if "m_IsPolygon" in data and data["m_IsPolygon"] is not False:
        data["m_IsPolygon"] = False
        changed_polygon = True

    expanded_rect = False
    if expand_to_m_rect:
        m_rect = data.get("m_Rect")
        rd_rect = rd.get("textureRect")
        rd_rect_off = rd.get("textureRectOffset")
        if isinstance(m_rect, dict) and isinstance(rd_rect, dict):
            x = _to_float(m_rect.get("x"))
            y = _to_float(m_rect.get("y"))
            w = _to_float(m_rect.get("width"))
            h = _to_float(m_rect.get("height"))
            rd_rect["x"] = x
            rd_rect["y"] = y
            rd_rect["width"] = w
            rd_rect["height"] = h
            if isinstance(rd_rect_off, dict):
                rd_rect_off["x"] = x
                rd_rect_off["y"] = y
            expanded_rect = True

    return before_raw, after_raw, changed_polygon, expanded_rect


def build_fullrect_output_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}.fullrect{path.suffix}")


def patch_payload(payload: Any, *, expand_to_m_rect: bool = True, lang: Language = "ko") -> tuple[str, dict[str, Any] | None]:
    if not isinstance(payload, dict):
        return "skip_non_object", None
    if "m_RD" not in payload or not isinstance(payload["m_RD"], dict) or "settingsRaw" not in payload["m_RD"]:
        return "skip_non_sprite", None
    if _is_fullrect_already(payload, require_rect_expand=expand_to_m_rect):
        return "already_fullrect", payload

    before_raw, after_raw, changed_polygon, expanded_rect = patch_sprite_json(
        payload,
        expand_to_m_rect=expand_to_m_rect,
        lang=lang,
    )
    return "modified", {
        "payload": payload,
        "before_raw": before_raw,
        "after_raw": after_raw,
        "changed_polygon": changed_polygon,
        "expanded_rect": expanded_rect,
    }


def format_modified_message(details: dict[str, Any], lang: Language) -> str:
    parts = [t(lang, "modified_base", before=details["before_raw"], after=details["after_raw"])]
    if details.get("changed_polygon"):
        parts.append(t(lang, "modified_polygon"))
    if details.get("expanded_rect"):
        parts.append(t(lang, "modified_rect"))
    return ", ".join(parts)


def patch_file_inplace(path: Path, *, expand_to_m_rect: bool = True, lang: Language = "ko") -> tuple[str, str]:
    with path.open("r", encoding="utf-8-sig") as f:
        payload = json.load(f)

    status, details = patch_payload(payload, expand_to_m_rect=expand_to_m_rect, lang=lang)
    if status != "modified":
        return status, ""

    patched = details["payload"]
    with path.open("w", encoding="utf-8") as f:
        json.dump(patched, f, ensure_ascii=False, indent=2)
    return status, format_modified_message(details, lang)


def convert_file_to_copy(path: Path, *, expand_to_m_rect: bool = True, lang: Language = "ko") -> tuple[str, str, Path | None]:
    with path.open("r", encoding="utf-8-sig") as f:
        payload = json.load(f)

    status, details = patch_payload(payload, expand_to_m_rect=expand_to_m_rect, lang=lang)
    if status in {"skip_non_object", "skip_non_sprite"}:
        return status, "", None

    output_path = build_fullrect_output_path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_payload = details["payload"] if details is not None else payload
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(write_payload, f, ensure_ascii=False, indent=2)

    if status == "modified":
        return status, format_modified_message(details, lang), output_path
    return status, "", output_path


def collect_targets(inputs: list[str], base_dir: Path, recursive: bool, *, exclude_fullrect_suffix: bool) -> list[Path]:
    if inputs:
        return [Path(value).expanduser().resolve() for value in inputs]

    pattern = "**/*.json" if recursive else "*.json"
    targets = sorted(p.resolve() for p in base_dir.glob(pattern) if p.is_file())
    if exclude_fullrect_suffix:
        targets = [p for p in targets if not p.name.lower().endswith(".fullrect.json")]
    return targets


def main_cli(lang: Language = "ko") -> None:
    parser = argparse.ArgumentParser(description=t(lang, "desc"))
    parser.add_argument("inputs", nargs="*", help=t(lang, "inputs_help"))
    parser.add_argument("--dir", default=".", help=t(lang, "dir_help"))
    parser.add_argument("--recursive", action="store_true", help=t(lang, "recursive_help"))
    parser.add_argument("--no-expand-rect", action="store_true", help=t(lang, "no_expand_help"))
    args = parser.parse_args()

    base_dir = Path(args.dir).expanduser().resolve()
    if not base_dir.exists():
        raise SystemExit(t(lang, "err_no_dir", path=base_dir))

    inputs_mode = len(args.inputs) > 0
    targets = collect_targets(
        args.inputs,
        base_dir,
        args.recursive,
        exclude_fullrect_suffix=(not inputs_mode),
    )
    if not targets:
        print(t(lang, "done_no_targets"))
        return

    modified = 0
    generated = 0
    skipped_full = 0
    skipped_non_sprite = 0
    errors = 0

    for target in targets:
        if not target.exists():
            print(t(lang, "err_no_file", path=target))
            errors += 1
            continue

        if inputs_mode:
            try:
                status, msg, output_path = convert_file_to_copy(
                    target,
                    expand_to_m_rect=not args.no_expand_rect,
                    lang=lang,
                )
            except Exception as e:
                print(t(lang, "err_file", name=target.name, error=e))
                errors += 1
                continue

            if status in {"skip_non_object", "skip_non_sprite"}:
                skipped_non_sprite += 1
                print(t(lang, "skip_non_sprite", name=target.name))
            elif status == "already_fullrect":
                skipped_full += 1
                generated += 1
                print(t(lang, "gen_already", name=output_path.name))
            elif status == "modified":
                modified += 1
                generated += 1
                print(t(lang, "gen_modified", name=output_path.name, msg=msg))
            else:
                errors += 1
                print(t(lang, "err_unknown_state", name=target.name, status=status))
        else:
            try:
                status, msg = patch_file_inplace(
                    target,
                    expand_to_m_rect=not args.no_expand_rect,
                    lang=lang,
                )
            except Exception as e:
                print(t(lang, "err_file", name=target.name, error=e))
                errors += 1
                continue

            if status in {"skip_non_object", "skip_non_sprite"}:
                skipped_non_sprite += 1
                print(t(lang, "skip_non_sprite", name=target.name))
            elif status == "already_fullrect":
                skipped_full += 1
                print(t(lang, "skip_already", name=target.name))
            elif status == "modified":
                modified += 1
                print(t(lang, "mod_modified", name=target.name, msg=msg))
            else:
                errors += 1
                print(t(lang, "err_unknown_state", name=target.name, status=status))

    if inputs_mode:
        print(
            t(
                lang,
                "summary_inputs",
                total=len(targets),
                generated=generated,
                modified=modified,
                skipped_full=skipped_full,
                skipped_non_sprite=skipped_non_sprite,
                errors=errors,
            )
        )
    else:
        print(
            t(
                lang,
                "summary_batch",
                total=len(targets),
                modified=modified,
                skipped_full=skipped_full,
                skipped_non_sprite=skipped_non_sprite,
                errors=errors,
            )
        )


def run_main_ko() -> None:
    try:
        main_cli(lang="ko")
    except Exception as e:
        print(t("ko", "unexpected", error=e))
        tb_module.print_exc()
        sys.exit(1)


def run_main_en() -> None:
    try:
        main_cli(lang="en")
    except Exception as e:
        print(t("en", "unexpected", error=e))
        tb_module.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    run_main_ko()
