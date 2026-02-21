[> for Korean version of README.md](README.md)

# Unity Sprite FullRect Replacer

A Unity Sprite extract/replace tool.  
This document is written with **PyInstaller-built EXE usage** as the default.

## Key Features

1. Export Sprite metadata (JSON)
2. Replace Sprites (JSON-based / filename-based)
3. Extract all Sprites (PNG + JSON)
4. Batch import Sprites (changed-only by default, missing files skipped by default)
5. Filtered extraction
   - file name + PathID (`--ids`, `--id`)
   - name-based (`--names`, `--name-contains`)
6. Replacement modes
   - `fullrect`: force FullRect
   - `tightclip`: clip tightly to image alpha bounding box
   - `tightmesh`: clip with a polygon mesh generated from image alpha outline

## Executables

If you downloaded a release package, run these directly:

- `unity_sprite_fullrect_replacer.exe`
- `unity_sprite_fullrect_replacer_en.exe`
- `UABEA_sprite_json_edit.exe`
- `UABEA_sprite_json_edit_en.exe`

If you want to build from source:

```bash
py -3.12 -m pip install -U pyinstaller UnityPy Pillow opencv-python-headless mapbox-earcut numpy
py -3.12 -m PyInstaller --onefile --name unity_sprite_fullrect_replacer unity_sprite_fullrect_replacer.py
py -3.12 -m PyInstaller --onefile --name unity_sprite_fullrect_replacer_en unity_sprite_fullrect_replacer_en.py
py -3.12 -m PyInstaller --onefile --name UABEA_sprite_json_edit UABEA_sprite_json_edit.py
py -3.12 -m PyInstaller --onefile --name UABEA_sprite_json_edit_en UABEA_sprite_json_edit_en.py
```

Built EXE paths:

```text
dist\unity_sprite_fullrect_replacer.exe
dist\unity_sprite_fullrect_replacer_en.exe
dist\UABEA_sprite_json_edit.exe
dist\UABEA_sprite_json_edit_en.exe
```

## CLI Reference (unity_sprite_fullrect_replacer_en.exe)

Basic format:

```bat
unity_sprite_fullrect_replacer_en.exe [options]
```

Option groups:

### Common

- `--gamepath PATH`
  - Game root / `_Data` / single `.assets` file path
  - Default: none (prompts during execution if omitted)
- `--mode fullrect|tightclip|tightmesh`
  - Default replacement mode
  - Default: `fullrect`
- `--verbose`
  - Also save console logs to `verbose.txt`
  - Default: `False`

### Extraction

- `--parse`
  - Export Sprite metadata JSON
  - Default: `False`
- `--extract-all`
  - Extract all Sprite PNGs (or only filtered targets) + optional JSON output
  - Default: `False`
- `--ids IDS` / `--id IDS`
  - file+PathID filter, supports comma-separated and repeated usage
  - Example: `--ids "sharedassets0.assets:186,sharedassets0.assets:200"`
  - Default: none
- `--name NAME` / `--names NAME`
  - Sprite name filter (supports repeated usage / comma-separated)
  - Default: none
- `--name-contains TEXT`
  - Partial name match filter (supports repeated usage / comma-separated)
  - Default: none
- `--output-dir PATH`
  - PNG output directory for `--extract-all`
  - Default: `<script_folder>\<game_name>_sprites`
- `--json-out PATH`
  - JSON output path for `--parse`/`--extract-all`
  - Default: `<script_folder>\<game_name>_sprites.json`

### Insertion

- `--list JSON_FILE`
  - Replace Sprites from JSON mapping
  - Default: none
- `--replace-dir DIR`
  - PNG directory for filename-based replacement without JSON
  - Default: none
- `--replace-recursive`
  - Recursively scan PNG files under `--replace-dir`
  - Default: `False`
- `--skip-missing` / `--no-skip-missing`
  - Toggle skip behavior when `Replace_to` file is missing
  - Default: `--skip-missing` (enabled)
- `--changed-only` / `--no-changed-only`
  - Toggle changed-only write behavior
  - Default: `--changed-only` (enabled)
- Note: `--list` and `--replace-dir` are mutually exclusive.

Examples:

1. Export Sprite metadata JSON
```bat
unity_sprite_fullrect_replacer_en.exe --gamepath "D:\Games\Game" --parse
```

2. Extract all Sprite PNGs + generate JSON
```bat
unity_sprite_fullrect_replacer_en.exe --gamepath "D:\Games\Game" --extract-all --output-dir ".\sprites_out" --json-out ".\sprites.json"
```

3. Extract with file+PathID filter
```bat
unity_sprite_fullrect_replacer_en.exe --gamepath "D:\Games\Game_Data\sharedassets0.assets" --extract-all --ids "sharedassets0.assets:186"
```

4. Extract with name filter (`--name` and `--names` are equivalent)
```bat
unity_sprite_fullrect_replacer_en.exe --gamepath "D:\Games\Game" --extract-all --name "字 A"
unity_sprite_fullrect_replacer_en.exe --gamepath "D:\Games\Game" --extract-all --names "字 A"
```

5. JSON-based replacement (fullrect)
```bat
unity_sprite_fullrect_replacer_en.exe --gamepath "D:\Games\Game" --list ".\sprites.json" --mode fullrect
```

6. JSON-based replacement (tightclip)
```bat
unity_sprite_fullrect_replacer_en.exe --gamepath "D:\Games\Game" --list ".\sprites.json" --mode tightclip
```

7. JSON-based replacement (tightmesh)
```bat
unity_sprite_fullrect_replacer_en.exe --gamepath "D:\Games\Game" --list ".\sprites.json" --mode tightmesh
```

8. Filename-based replacement (without JSON)
```bat
unity_sprite_fullrect_replacer_en.exe --gamepath "D:\Games\Game" --replace-dir ".\sprites" --mode fullrect
```

## JSON Format

Edit JSON generated by `--parse` or `--extract-all`, then pass it to `--list`.
If `Replace_to` is a relative path, it is resolved against the EXE (or script) directory.

```json
{
  "sharedassets0.assets|sharedassets0.assets|字 A|Sprite|186": {
    "Type": "Sprite",
    "File": "sharedassets0.assets",
    "assets_name": "sharedassets0.assets",
    "Path_ID": 186,
    "Name": "字 A",
    "TextureRect_X": 125,
    "TextureRect_Y": 369,
    "TextureRect_Width": 468,
    "TextureRect_Height": 696,
    "Mode": "fullrect",
    "Replace_to": "C:\\path\\to\\字 A.sprite.png"
  }
}
```

## UABEA JSON Patch Script (UABEA_sprite_json_edit.exe / UABEA_sprite_json_edit_en.exe)

`UABEA_sprite_json_edit.exe` (Korean) and `UABEA_sprite_json_edit_en.exe` (English) patch UABEA-dumped Sprite JSON by target mode.

What it does (by mode):
- `fullrect`
  - Set `settingsRaw` to FullRect bits
  - (Default) keep current `textureRect`
  - Use `--expand-rect` to expand `textureRect` to `m_Rect`
  - set `textureRectOffset` to `(0,0)`
  - rebuild quad mesh (4 vertices)
- `tightclip` (requires `--image`)
  - set `textureRect` from PNG alpha bounding box
  - set `textureRectOffset` to `textureRect.xy`
  - rebuild quad mesh (4 vertices)
- `tightmesh` (requires `--image`)
  - set `textureRect` from PNG alpha bounding box
  - set `textureRectOffset` to `textureRect.xy`
  - try rebuilding polygon mesh (environment-dependent), otherwise keep existing mesh

Behavior:
- **With input arguments**: create `<original_name>.<mode>.json` (keep original file)
- **Without input arguments**: in-place patch JSON files in current directory (or `--dir`), excluding `.fullrect/.tightclip/.tightmesh.json`

CLI options (full list):

- `inputs` (positional, zero or more)
  - If specified, creates `<name>.<mode>.json` for each input
  - Default: none
- `--dir PATH`
  - Target folder for batch mode when no positional inputs are given
  - Default: current folder (`.`)
- `--recursive`
  - Recursively scan subfolders in batch mode (no positional inputs)
  - Default: `False`
- `--mode fullrect|tightclip|tightmesh`
  - Target mode
  - Default: `fullrect`
- `--image PATH`
  - PNG image used by `tightclip`/`tightmesh`
  - Optional in `fullrect`
- `--expand-rect`
  - For `fullrect` only: expand `textureRect` to `m_Rect`
  - Default: `False` (no expansion by default)

Examples:

1. Single input -> create `<name>.fullrect.json`
```bat
UABEA_sprite_json_edit_en.exe "字 A-sharedassets0.assets.bak_before_sprite_replace-186.json"
```

2. Single input -> create `<name>.tightclip.json` (requires `--image`)
```bat
UABEA_sprite_json_edit_en.exe --mode tightclip --image "C:\path\to\字 A.sprite.png" "字 A-sharedassets0.assets.bak_before_sprite_replace-186.json"
```

3. Single input -> create `<name>.tightmesh.json` (requires `--image`)
```bat
UABEA_sprite_json_edit_en.exe --mode tightmesh --image "C:\path\to\字 A.sprite.png" "字 A-sharedassets0.assets.bak_before_sprite_replace-186.json"
```

4. Batch in current folder (excluding generated mode suffix files)
```bat
UABEA_sprite_json_edit_en.exe
```

5. Batch in specific folder
```bat
UABEA_sprite_json_edit_en.exe --dir "C:\path\to\json_folder"
```

6. Recursive batch in specific folder
```bat
UABEA_sprite_json_edit_en.exe --dir "C:\path\to\json_folder" --recursive
```

## Notes

- `Managed` folder is not required for Sprite-only modifications.
- Input path supports game root / `_Data` / single `.assets` file.
- In `fullrect` replacement, the tool updates `textureRect/textureRectOffset` to match `m_Rect` together with `settingsRaw`.
- `tightmesh` writes an alpha-outline polygon mesh into `m_VertexData/m_IndexBuffer/m_SubMeshes`.
  - For better `tightmesh` quality, `opencv-python-headless`, `mapbox-earcut`, and `numpy` are recommended.
- When `--gamepath` is a game root or `_Data` folder, the tool recursively scans the entire `_Data` folder by default.
  (It includes Unity serialized-file candidates beyond `.assets`, and auto-skips files that fail `UnityPy.load`.)
- If a Texture2D `.resS` resource is missing, that entry is skipped instead of aborting the entire run.

## License

[MIT License](LICENSE)
