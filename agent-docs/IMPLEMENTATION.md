# Implementation notes

## Structure

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
`图库帮助` renders usage as a PNG using the configured Chinese font and current
wait/forward settings; it is sent as a normal image.
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
Deletion deduplicates IDs and processes each separately, reporting partial failure.
Files are renamed to `.deleting` before committing row deletion; failed database
operations restore the original file. Startup restores staged files with surviving
rows and removes staged files whose rows were deleted. Empty galleries and aliases
survive deletion, and SQLite AUTOINCREMENT prevents reuse of deleted image IDs.
No additional role restriction is imposed on deletion or alias management.
Duplicate animation detection compares every frame in order; timing is excluded.
Requests waiting for images are isolated by platform instance, unified origin, and sender.
Original upload message IDs are retained for replies after follow-up image messages.
The plugin uses native OneBot actions because AstrBot's Image serializer drops `sub_type`.
No Git repository existed at implementation time; no commit or push was performed.

## Validation boundary

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
