import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from .imaging import decode, fingerprint


class GalleryStore:
    def __init__(self, root: Path):
        self.root = root
        self.images = root / "images"
        self.images.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gallery TEXT NOT NULL,
                filename TEXT NOT NULL UNIQUE,
                fingerprint TEXT NOT NULL,
                UNIQUE(gallery, fingerprint)
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS galleries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE
            )""")
            # Preserve galleries from installations predating explicit creation.
            db.execute("""INSERT OR IGNORE INTO galleries(name)
                SELECT gallery FROM images GROUP BY gallery ORDER BY MIN(id)""")
            # Canonical names and aliases share one database-enforced namespace.
            db.execute("""CREATE TABLE IF NOT EXISTS gallery_names (
                name TEXT PRIMARY KEY,
                gallery TEXT NOT NULL
            )""")
            db.execute("""INSERT OR IGNORE INTO gallery_names(name, gallery)
                SELECT name, name FROM galleries""")
        self.recover_deletions()

    def recover_deletions(self):
        # Restore uncommitted deletions or finish cleanup after an interrupted commit.
        with self.lock, self.connect() as db:
            for staged in self.images.glob("*.deleting"):
                original = staged.with_suffix(".img")
                exists = db.execute(
                    "SELECT 1 FROM images WHERE filename=?", (original.name,)
                ).fetchone()
                if exists and not original.exists():
                    staged.rename(original)
                else:
                    staged.unlink(missing_ok=True)
            # 清理替换原图时在事务提交前意外退出留下的新文件。
            for replacement in self.images.glob("*.replacement.img"):
                if not db.execute("SELECT 1 FROM images WHERE filename=?", (replacement.name,)).fetchone():
                    replacement.unlink(missing_ok=True)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.root / "gallery.sqlite3", timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, gallery: str) -> bool:
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT gallery FROM gallery_names WHERE name=?", (gallery,)).fetchone()
            if existing:
                if existing["gallery"] != gallery:
                    raise ValueError(f"名称“{gallery}”已被图库“{existing['gallery']}”的别名占用。")
                return False
            db.execute("INSERT INTO galleries(name) VALUES (?)", (gallery,))
            db.execute("INSERT INTO gallery_names(name, gallery) VALUES (?, ?)", (gallery, gallery))
            return True

    def require_gallery(self, gallery: str) -> str:
        with self.connect() as db:
            row = db.execute("SELECT gallery FROM gallery_names WHERE name=?", (gallery,)).fetchone()
            if row is None:
                raise ValueError(f"不存在图库“{gallery}”，请先发送“创建{gallery}”。")
            return row["gallery"]

    def gallery_identity(self, gallery: str) -> tuple[int, str]:
        with self.connect() as db:
            row = db.execute("""SELECT galleries.id, galleries.name
                FROM gallery_names JOIN galleries ON galleries.name=gallery_names.gallery
                WHERE gallery_names.name=?""", (gallery,)).fetchone()
            if row is None:
                raise ValueError(f"不存在图库“{gallery}”，请先发送“创建{gallery}”。")
            return row["id"], row["name"]

    def name_by_id(self, gallery_id: int) -> str:
        with self.connect() as db:
            row = db.execute("SELECT name FROM galleries WHERE id=?", (gallery_id,)).fetchone()
            if row is None:
                raise ValueError("原图库已不存在，请重新发送上传命令。")
            return row["name"]

    def rename(self, gallery: str, new_name: str) -> str:
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT gallery FROM gallery_names WHERE name=?", (gallery,)).fetchone()
            if row is None:
                raise ValueError(f"不存在图库“{gallery}”。")
            old_name = row["gallery"]
            if old_name == new_name:
                raise ValueError("图库新名与当前名称相同。")
            if db.execute("SELECT 1 FROM gallery_names WHERE name=?", (new_name,)).fetchone():
                raise ValueError(f"名称“{new_name}”已被图库名或别名占用。")
            db.execute("UPDATE galleries SET name=? WHERE name=?", (new_name, old_name))
            db.execute("UPDATE images SET gallery=? WHERE gallery=?", (new_name, old_name))
            db.execute("UPDATE gallery_names SET gallery=? WHERE gallery=?", (new_name, old_name))
            db.execute("UPDATE gallery_names SET name=? WHERE name=?", (new_name, old_name))
            return old_name

    def aliases(self, gallery: str) -> list[str]:
        gallery = self.require_gallery(gallery)
        with self.connect() as db:
            return [row["name"] for row in db.execute(
                "SELECT name FROM gallery_names WHERE gallery=? AND name!=gallery ORDER BY rowid",
                (gallery,),
            )]

    def add_alias(self, gallery: str, alias: str):
        gallery = self.require_gallery(gallery)
        with self.lock, self.connect() as db:
            try:
                db.execute("INSERT INTO gallery_names(name, gallery) VALUES (?, ?)", (alias, gallery))
            except sqlite3.IntegrityError:
                raise ValueError(f"名称“{alias}”已被图库名或别名占用。") from None

    def remove_alias(self, gallery: str, alias: str):
        gallery = self.require_gallery(gallery)
        with self.lock, self.connect() as db:
            cursor = db.execute(
                "DELETE FROM gallery_names WHERE gallery=? AND name=? AND name!=gallery",
                (gallery, alias),
            )
            if not cursor.rowcount:
                raise ValueError(f"图库“{gallery}”不存在别名“{alias}”。")

    def add(self, gallery: str, data: bytes) -> tuple[int, bool]:
        gallery = self.require_gallery(gallery)
        frames, _, _ = decode(data)
        digest = fingerprint(frames)
        with self.lock, self.connect() as db:
            existing = db.execute(
                "SELECT id FROM images WHERE gallery=? AND fingerprint=?", (gallery, digest)
            ).fetchone()
            if existing:
                return existing["id"], False
            filename = uuid.uuid4().hex + ".img"
            path = self.images / filename
            try:
                path.write_bytes(data)
                cursor = db.execute(
                    "INSERT INTO images(gallery, filename, fingerprint) VALUES (?, ?, ?)",
                    (gallery, filename, digest),
                )
                db.commit()
                return cursor.lastrowid, True
            except BaseException:
                path.unlink(missing_ok=True)
                raise

    def entries(self, gallery: str):
        gallery = self.require_gallery(gallery)
        with self.connect() as db:
            return db.execute("SELECT * FROM images WHERE gallery=? ORDER BY id", (gallery,)).fetchall()

    def by_ids(self, ids: tuple[int, ...]):
        with self.connect() as db:
            rows = db.execute(
                f"SELECT * FROM images WHERE id IN ({','.join('?' for _ in ids)})", ids
            ).fetchall()
        lookup = {row["id"]: row for row in rows}
        missing = [str(i) for i in ids if i not in lookup]
        if missing:
            raise ValueError("图片 ID 不存在：" + "、".join(missing))
        return [lookup[i] for i in ids]

    def covers(self):
        with self.connect() as db:
            return db.execute(
                """SELECT images.id, galleries.name AS gallery, images.filename,
                (SELECT COUNT(*) FROM images WHERE gallery = galleries.name) AS count
                FROM galleries LEFT JOIN images ON images.id = (
                    SELECT MIN(id) FROM images WHERE gallery = galleries.name
                ) ORDER BY galleries.id"""
            ).fetchall()

    def delete(self, ids: tuple[int, ...]) -> tuple[list[int], list[str]]:
        deleted, errors = [], []
        with self.lock:
            for image_id in dict.fromkeys(ids):
                with self.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    row = db.execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone()
                    if row is None:
                        errors.append(f"ID {image_id}：图片不存在。")
                        continue
                    path = self.path(row)
                    staged = path.with_suffix(".deleting")
                    moved = False
                    try:
                        if path.exists():
                            path.rename(staged)
                            moved = True
                        db.execute("DELETE FROM images WHERE id=?", (image_id,))
                        db.commit()
                    except Exception:
                        db.rollback()
                        if moved:
                            staged.rename(path)
                        errors.append(f"ID {image_id}：删除失败。")
                        continue
                    deleted.append(image_id)
                    if moved:
                        try:
                            staged.unlink()
                        except OSError:
                            errors.append(f"ID {image_id}：记录已删除，但文件清理失败。")
        return deleted, errors

    def path(self, row) -> Path:
        return self.images / row["filename"]

    def catalog(self, query: str = "") -> list[dict]:
        with self.lock, self.connect() as db:
            rows = db.execute("""SELECT g.id, g.name,
                (SELECT COUNT(*) FROM images WHERE gallery=g.name) AS count
                FROM galleries g WHERE EXISTS (
                    SELECT 1 FROM gallery_names n WHERE n.gallery=g.name
                    AND instr(lower(n.name), lower(?)) > 0
                ) ORDER BY g.id""", (query,)).fetchall()
            return [dict(row, aliases=self.aliases(row["name"])) for row in rows]

    def image_page(self, gallery_id: int | None, query: str, page: int) -> dict:
        with self.lock, self.connect() as db:
            clauses, values = [], []
            if gallery_id is not None:
                clauses.append("gallery=?")
                values.append(self.name_by_id(gallery_id))
            if query:
                clauses.append("CAST(id AS TEXT)=?")
                values.append(query)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            total = db.execute("SELECT COUNT(*) FROM images" + where, values).fetchone()[0]
            rows = db.execute("SELECT id, gallery FROM images" + where +
                              " ORDER BY id DESC LIMIT 20 OFFSET ?", (*values, (page - 1) * 20)).fetchall()
            return {"items": [dict(row) for row in rows], "total": total, "page": page}

    def add_by_gallery_id(self, gallery_id: int, data: bytes) -> tuple[int, bool]:
        with self.lock:
            return self.add(self.name_by_id(gallery_id), data)

    def move_image(self, image_id: int, gallery_id: int):
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            gallery = self.name_by_id(gallery_id)
            row = self.by_ids((image_id,))[0]
            try:
                db.execute("UPDATE images SET gallery=? WHERE id=?", (gallery, image_id))
            except sqlite3.IntegrityError:
                raise ValueError("目标图库已有相同图片，未移动。") from None
            return {"id": row["id"], "gallery": gallery}

    def replace_image(self, image_id: int, data: bytes):
        frames, _, _ = decode(data)
        digest = fingerprint(frames)
        del frames
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.by_ids((image_id,))[0]
            duplicate = db.execute("SELECT id FROM images WHERE gallery=? AND fingerprint=? AND id!=?",
                                   (row["gallery"], digest, image_id)).fetchone()
            if duplicate:
                raise ValueError(f"图库已有相同图片（ID {duplicate['id']}），未替换。")
            old = self.path(row)
            staged = old.with_suffix(".deleting")
            new = self.images / (uuid.uuid4().hex + ".replacement.img")
            moved = False
            try:
                new.write_bytes(data)
                if old.exists():
                    old.rename(staged)
                    moved = True
                db.execute("UPDATE images SET filename=?, fingerprint=? WHERE id=?",
                           (new.name, digest, image_id))
                db.commit()
            except BaseException:
                db.rollback()
                new.unlink(missing_ok=True)
                if moved:
                    staged.rename(old)
                raise
            # 提交后清理失败交给启动恢复，不能将成功的替换报告为失败。
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                pass

    def delete_gallery(self, gallery_id: int):
        staged_files = []
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            name = self.name_by_id(gallery_id)
            try:
                for row in db.execute("SELECT * FROM images WHERE gallery=?", (name,)):
                    path = self.path(row)
                    if path.exists():
                        staged = path.with_suffix(".deleting")
                        path.rename(staged)
                        staged_files.append((path, staged))
                db.execute("DELETE FROM images WHERE gallery=?", (name,))
                db.execute("DELETE FROM gallery_names WHERE gallery=?", (name,))
                db.execute("DELETE FROM galleries WHERE id=?", (gallery_id,))
                db.commit()
            except BaseException:
                db.rollback()
                for path, staged in reversed(staged_files):
                    staged.rename(path)
                raise
            for _, staged in staged_files:
                try:
                    staged.unlink(missing_ok=True)
                except OSError:
                    pass
