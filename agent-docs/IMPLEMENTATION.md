# Implementation notes

## Official development references

- [AstrBot plugin development guide](https://docs.astrbot.app/dev/star/plugin-new.html): primary reference for plugin development and framework integration.
- [AstrBot plugin views](https://docs.astrbot.app/dev/star/guides/plugin-pages.html): reference for the management view, bridge, and plugin Web API.

Consult these official documents and the local AstrBot source before changing framework integrations. Verify API availability against the target AstrBot version.

## AstrBot compatibility

Plugin v0.4.0 declares `astrbot_version: ">=4.29.0,<5"` using the official PEP 440 metadata field. The lower bound comes from the actual view contract: [v4.28.2 bridge](https://github.com/AstrBotDevs/AstrBot/blob/v4.28.2/astrbot/dashboard/plugin_page_bridge.js) lacks `AstrBotPluginView`, while [v4.29.0 bridge](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.0/astrbot/dashboard/plugin_page_bridge.js) exposes it. The v4.29.0 view service supports `views/`, and its public Web API provides the request, response, upload read/close, and response-header interfaces used here. The next major version is excluded pending compatibility review.

This is a source-verified API compatibility floor, not a claim of full runtime testing on every supported release. Independent browser integration checks used official 4.29.3 source APIs and SDK; complete AstrBot startup/login remains unverified. The missing-Web-API fallback is defensive only and does not expand the declared support range.

## Structure

- `gallery/web.py`: AstrBot public Web API handlers registered during initialization; all expensive work shares the chat work lock.
- `views/manage/`: dependency-free plugin view using only the injected AstrBotPluginView bridge, with sequential per-file import and first-frame PNG previews.
- `.astrbot-plugin/i18n/zh-CN.json`: view title and description.

List thumbnails remain first-frame PNGs. Clicking a thumbnail opens an original-image
dialog through `images/<image_id>/original`, returning the exact stored bytes as a
MIME-typed data URL over the authenticated bridge. No image re-encoding occurs;
browser-supported animations preserve their original timing and looping. The dialog
supports fit-to-window and natural size, clears image sources on close, and ignores
late responses from closed viewers. Tests assert byte identity and observe changing
GIF pixels in the sandboxed browser view.

The view follows the official plugin-pages contract. No independent HTTP server or authentication layer is used. Missing public Web API support disables only registration, retaining chat functionality. Closed handlers reject requests after termination. Gallery mutations resolve stable IDs under the storage lock. Gallery deletion stages all files before one transaction; replacement uses a unique `.replacement.img` file and stages the old file. Startup removes unreferenced replacement files and restores/cleans staged deletions according to database state. UI pagination is capped at 20 images, and uploads reuse max_download_mb and existing decode/deduplication constraints.

- `main.py`: AstrBot lifecycle, event dispatch, pending requests, bounded processing.
- `gallery/commands.py`: command parsing and validation.
- `gallery/storage.py`: SQLite metadata, persistent original files, gallery-scoped deduplication.
- `gallery/imaging.py`: frame decoding, DCT pHash, transformations, contact sheets.
- `gallery/fonts.py`: ordered directory fonts, cached cmap coverage, grapheme fallback,
  bidi run ordering, shared measurement/rendering, and bitmap color-font scaling.
- `gallery/onebot.py`: CQ/array parsing, first-level forward extraction, bounded HTTP download, native OneBot sending.
- `tests/`: domain tests and handler tests using explicit AstrBot/transport mocks.

## Decisions

New commands use AstrBot's `is_at_or_wake_command` gate and normalized
`get_message_str()`, matching CommandFilter's wake policy. Do not gate on `is_wake`:
ALL-event listeners set that flag even for otherwise unawakened messages.
The domain parser accepts command bodies only and strips no prefix.
Pending image continuations still accept unawakened messages from the same session/sender.

The user explicitly selected deduplication within a gallery. Other galleries may contain the same image.
IDs are globally increasing positive integers. Gallery names cannot conflict with ID/count syntax.
All galleries are shared within the plugin instance.
New galleries must be explicitly created with `创建图库名`; uploads never create them.
The `galleries` table persists empty galleries. Startup idempotently migrates distinct
gallery names from legacy image rows. Covers use a left join and empty placeholders.
`图库帮助` renders docs/help.md as a PNG using the configured font stack; it is sent as a normal image.
Command rows share measured column widths. Administrator markers in the document
are illustrative: rendered markers require a nonblank admin ID and the corresponding
selected permission. The legacy wait/forward arguments remain accepted for compatibility
but do not add text to the user-supplied help copy.
`gallery_names` stores both canonical names and aliases under one primary key.
Name resolution returns the canonical name before storing or querying images.
Pending uploads capture the stable gallery ID when the command is received and
resolve its current name at execution, preventing rename/recreate misrouting.
Renaming updates galleries, image ownership, and the shared name namespace in one
transaction. Existing aliases and gallery/image IDs are preserved. The old canonical
name is released, not converted into an alias; new names must be globally unused.
Alias commands accept quoted names containing whitespace.
Contact-sheet headers wrap the canonical name and every alias without truncation,
including for empty galleries.
Oversized contact sheets split into ordered pages within the 16-million-pixel
per-image budget. Every page repeats the complete header and uses the same column
width, including the final partial page. Multi-page previews follow multi_image_mode
and the strict forward_threshold comparison while retaining sub_type=0. Single-page
previews and help images remain direct.
Deletion deduplicates IDs and processes each separately, reporting partial failure.
Files are renamed to `.deleting` before committing row deletion; failed database
operations restore the original file. Startup restores staged files with surviving
rows and removes staged files whose rows were deleted. Empty galleries and aliases
survive deletion, and SQLite AUTOINCREMENT prevents reuse of deleted image IDs.
Chat mutation permissions are selected independently through `admin_gallery_permissions`
and `admin_image_permissions`. Restrictions apply only when `admin_ids` contains a
nonblank sender ID and the operation is selected. An empty administrator list allows
everyone. Group roles and AstrBot administrator roles do not implicitly grant access.
Check before collecting images or creating pending requests, again on continuations,
and at execution after acquiring the work lock. Alias queries remain unrestricted.
`图库删除` resolves a stable gallery ID and reuses transactional `delete_gallery`.
The authenticated Dashboard retains its own access boundary, without QQ ID checks.
Duplicate animation detection compares every frame in order; timing is excluded.
Requests waiting for images are isolated by platform instance, unified origin, and sender.
Original upload message IDs are retained for replies after follow-up image messages.
The plugin uses native OneBot actions because AstrBot's Image serializer drops `sub_type`.
No Git repository existed at implementation time; no commit or push was performed.

## Validation boundary

For the optional browser integration check, install `fastapi`, `python-multipart`,
and `playwright` in a test environment with local Chrome, then run:
`python tests/verify_web_view.py --astrbot <official-source-root> --output <temporary-screenshot-directory>`.
This uses the unmodified official `astrbot.api.web` module and bridge SDK with a
minimal Dashboard host in a sandboxed iframe. It exercises real multipart parsing,
creation, mixed import results, rename, replacement, move, deletion, desktop/mobile
layout, and dark context updates. It does not exercise full core initialization,
login/authentication, view discovery, or QQ integration. Test gallery files are
isolated in an automatically cleaned temporary directory.

Font configuration uses `font_dir` and `font_order`; legacy `font_path` remains an
internal fallback when no directory is configured. Explicit directories never add
system fonts. File metadata participates in cache keys; collection files use face 0.
Coverage is not proof of an emoji sequence ligature. Pillow RAQM is required for
RTL rendering, and recommended for all complex scripts and emoji sequences.
python-bidi's Python algorithm provides run levels; Pillow shapes logical runs.
Unicode bidi isolates are currently rejected. Test real fonts and RAQM builds when
assessing complex-script/emoji fidelity; unit tests alone cannot establish it.

Local tests run with Python 3.12, Pillow, and NumPy.
Handler tests mock AstrBot and HTTP session creation; they do not establish real framework or protocol connectivity.
Dependency installation was blocked by sandbox socket permissions.
Docker version was readable, but container operations were denied by filesystem/named-pipe permissions.
Therefore live AstrBot loading, real HTTP downloads, QQ forwarding, and sticker rendering remain integration checks.
Ruff was not installed in the available environment.

## Integration checklist

1. Install `requirements.txt` in the AstrBot runtime and ensure a Chinese font exists.
2. Load the plugin through the configured local AstrBot compose environment.
3. Verify direct/replied/follow-up uploads in private and group conversations.
4. Verify a quoted forward containing an image and a nested forward imports only the first-level image.
5. Verify default direct sending at five images and forced forwarding at six.
6. Check sticker rendering with the actual OneBot implementation.
7. Restart AstrBot and verify persisted galleries/IDs.
