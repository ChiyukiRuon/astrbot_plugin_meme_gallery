# Agent Guide

## Working agreements

- Read the relevant implementation and tests before editing; follow existing conventions.
- Write user-facing documentation, messages, and comments in Chinese. Write agent-facing documentation and implementation notes in English.
- Keep this entry point at the project root; place additional agent documentation in `agent-docs/`.
- Keep changes focused on the requested task. Ask the user when unresolved requirements or alternative designs would materially change behavior; do not silently change product decisions.
- Do not commit or push unless explicitly requested. When requested, use Conventional Commits (`type(scope): description`, optional scope), with a brief subject and any details after a blank line.
- Do not assume this directory is a Git checkout. Check before using Git; do not initialize a repository without a request.

## Project and file map

This is `astrbot_plugin_meme_gallery`, an AstrBot plugin for `aiocqhttp` (OneBot v11). It provides shared meme galleries, image upload and retrieval, previews, and static/animated image transformations. It is loaded by AstrBot, not run as a standalone application.

| Path | Responsibility |
| --- | --- |
| `main.py` | `MemeGallery` lifecycle, AstrBot event handling, pending requests, command execution, and bounded processing |
| `gallery/commands.py` | Framework-independent command parsing, immutable `Command`, and name/argument validation |
| `gallery/storage.py` | SQLite metadata, original image files, gallery identity, aliases, deduplication, and deletion recovery |
| `gallery/imaging.py` | Pillow/NumPy decoding, per-frame pHash, transformations, fonts, help images, and contact sheets |
| `gallery/onebot.py` | CQ/array message parsing, image extraction/download, and native OneBot sending |
| `tests/test_gallery.py` | Command, imaging, storage, and OneBot unit tests |
| `tests/test_plugin.py` | Handler tests using mocked AstrBot APIs and transport |
| `_conf_schema.json` | AstrBot configuration definitions and defaults |
| `metadata.yaml` | Plugin identity, version, description, and supported platforms |
| `requirements.txt` | Runtime dependencies: Pillow, NumPy, and aiohttp |
| `README.md` | Chinese installation, command, configuration, and behavior documentation |
| `agent-docs/IMPLEMENTATION.md` | Detailed design decisions and integration checklist |

Read `agent-docs/IMPLEMENTATION.md` for design context. Its environment and validation notes describe past observations; recheck them before treating them as current limitations.

## Behavior to preserve

### Commands and pending requests

- New commands use AstrBot's `is_at_or_wake_command` gate and normalized `get_message_str()`. Do not substitute `is_wake`: ALL-event listeners can set it themselves.
- `parse_command()` consumes command bodies only; prefix stripping and wake policy belong to AstrBot.
- Pending image requests are isolated by platform instance, unified message origin, and sender. Follow-up images do not require another wake prefix.
- A new valid command replaces the previous pending request for that key. Expiration silently discards requests, and a pending request must only be claimed once.
- Retain the original command message ID for follow-up upload replies.
- Uploads capture the stable gallery ID so a rename followed by recreation of the old name cannot redirect an existing pending upload.

### Storage and gallery identity

- Galleries are shared within the plugin instance. Uploads require explicit gallery creation and must never create galleries implicitly.
- Canonical names and aliases share one globally unique namespace and the same validation rules. Preserve quoted names in alias and rename commands.
- Image IDs are globally increasing positive integers and must not be reused after deletion.
- Rename updates gallery metadata, image ownership, and the name namespace transactionally. Preserve gallery/image IDs and existing aliases; release the old canonical name without making it an alias.
- Empty galleries and aliases survive deletion of their last image. The lowest remaining image ID supplies the cover; empty galleries use placeholders.
- Deduplication is scoped to one gallery. Static images use pHash; animations compare all frame hashes in order, including frame count but excluding timing. This is visual deduplication, not byte equality.
- Preserve uploaded originals. Runtime data belongs under `StarTools.get_data_dir("astrbot_plugin_meme_gallery")`, normally AstrBot's `data/plugin_data/astrbot_plugin_meme_gallery`, containing `gallery.sqlite3` and `images/`.
- Keep startup migrations idempotent and compatible with existing data. Preserve transaction rollback and `.deleting` staging/recovery when changing deletion logic.
- Batch deletion deduplicates IDs and reports partial failures. Current commands impose no extra role restriction on deletion or alias management; do not change that policy implicitly.

### Images and transport

- Uploads support attached/replied/follow-up images and first-level forward nodes. Do not recursively expand nested forwards or replies inside forward nodes.
- Transformations accept exactly one image and do not expand forwards. Speed and reverse require animation.
- Keep native OneBot actions for image sending: the implementation relies on `sub_type=1` for meme images. Help and gallery previews are ordinary images.
- Forwarding uses a strict `count > forward_threshold` comparison (default threshold: 5). Explicit forward mode applies to multiple images; previews remain direct.
- Preserve current bounds: at most 20 images per view, configurable download size (default 20 MB), 16 million pixels per image, 500 animation frames, 100 million total decoded pixels, and 16 million pixels per contact sheet.
- Static transformations output PNG; animated transformations output GIF with 10 ms timing granularity. Odd-sized mirrors preserve the middle row or column.
- Preview headers include the canonical name and every alias, wrapping rather than truncating. Font configuration must support container paths.

## Implementation conventions

- Keep parsing, storage, imaging, and transport concerns in their existing modules; do not move domain logic into the event handler.
- Follow the existing Python style: four-space indentation, type hints where useful, dataclasses for command/request values, and `ValueError` for expected user-facing validation failures.
- Keep blocking image and storage work off the async event loop using `asyncio.to_thread`. Preserve the plugin work lock, storage locking, download bounds, and HTTP session cleanup.
- When changing commands or configuration, update the parser/handler, `_conf_schema.json` defaults where applicable, generated help in `gallery/imaging.py`, and `README.md` together.
- Add or adjust regression tests for changed behavior, especially wake gating, request isolation, rename identity, migrations, deduplication, and file/database consistency.
- Do not edit generated `__pycache__` files or place runtime gallery data in the source tree.

## Validation and local integration

Use an interpreter with the dependencies from `requirements.txt`; existing validation notes use Python 3.12. A local `.venv` may be available, but check it before relying on it.

Run from the project root for code changes:

```sh
python -m unittest discover -s tests -v
python -m compileall -q main.py gallery
```

On Windows, use `.\.venv\Scripts\python.exe` in place of `python` when using that environment. No project formatter or linter configuration is currently provided; do not invent a required tooling pipeline.

Handler tests mock AstrBot and HTTP session creation. Passing them does not prove real AstrBot loading, HTTP connectivity, QQ forwarding, or sticker rendering. Report what was actually checked and any remaining integration gaps.

For integration work, follow `agent-docs/IMPLEMENTATION.md` and exercise private/group messages, wake rules, attached/replied/follow-up uploads, first-level forward extraction, send thresholds, and persistence across restart. Back up the entire runtime data directory with the plugin stopped before testing against valuable stored data.

Reference for AstrBot API work: https://docs.astrbot.app/dev/star/plugin-new.html. Check the local AstrBot source and current official documentation when API behavior is uncertain.
