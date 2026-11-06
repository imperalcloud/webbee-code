"""Universal index phase 2, Task 7: the local index takes any folder, every file kind, the focus first; only code and
resolution configs are ever listed for the vault (documents never leave the machine)."""
import io
import os
import time
import unicodedata
import zipfile

from webbee.intel.service import IntelService, _index_walk, _local_listing_of


def _docx(title: str) -> bytes:
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f'<w:document {ns}><w:body><w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
                                        f"<w:r><w:t>{title}</w:t></w:r></w:p></w:body></w:document>")
    return buf.getvalue()


def _folder(tmp_path):
    f = tmp_path / "home"
    (f / "app").mkdir(parents=True)
    (f / ".gnupg").mkdir()
    (f / "app" / "Main.kt").write_text("class Wallet { fun pay() = 1 }\n")
    (f / "notes.md").write_text("# Plan\nship it\n")
    (f / "NDA.docx").write_bytes(_docx("Agreement"))
    (f / "photo.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    (f / ".gnupg" / "a.docx").write_bytes(_docx("secret"))
    (f / "x.icloud").write_bytes(b"bplist00")
    (f / unicodedata.normalize("NFD", "Café.txt")).write_text("menu\n")
    return f


def test_any_folder_by_kind_and_nothing_but_code_to_the_vault(tmp_path):
    folder = _folder(tmp_path)
    svc = IntelService(str(folder), "k1", cache_dir=str(tmp_path / "cache"), focus="app")
    svc.build()
    kinds = {p: f.kind for p, f in svc.index.files.items()}
    assert kinds["NDA.docx"] == "document" and kinds["app/Main.kt"] == "code" and kinds["notes.md"] == "document"
    assert ".gnupg/a.docx" not in kinds and "x.icloud" not in kinds
    assert any(unicodedata.normalize("NFC", p) == "Café.txt" for p in kinds)
    prog = svc.progress()
    assert prog["skipped"]["placeholder"] == 1 and prog["indexed"] == prog["total"] and prog["focus_done"]
    listing, _capped = _local_listing_of(str(folder))
    assert {i["path"] for i in listing} <= {"app/Main.kt"}            # what the vault may get: code only


def test_focus_first_then_recent_then_the_rest(tmp_path):
    folder = _folder(tmp_path)
    old = time.time() - 90 * 86400
    os.utime(folder / "notes.md", (old, old))
    order, _skipped = _index_walk(str(folder), focus="app")
    assert order[0] == "app/Main.kt"
    assert order.index("NDA.docx") < order.index("notes.md")            # changed in the last 30 days first


def test_a_big_folder_is_built_in_a_process_pool_with_the_same_result(tmp_path, monkeypatch):
    from webbee.intel import service
    f = tmp_path / "big"
    f.mkdir()
    for i in range(30):
        (f / f"m{i}.py").write_text(f"def f{i}():\n    return {i}\n")
    monkeypatch.setattr(service, "POOL_MIN_FILES", 10)
    pooled = IntelService(str(f), "k2", cache_dir=str(tmp_path / "c1"))
    pooled.build()
    monkeypatch.setattr(service, "POOL_MIN_FILES", 10_000)
    inline = IntelService(str(f), "k3", cache_dir=str(tmp_path / "c2"))
    inline.build()
    assert pooled.index.files == inline.index.files and len(pooled.index.files) == 30


def test_excluded_folders_and_app_packages_are_never_walked(tmp_path):
    f = tmp_path / "Documents"
    (f / "repo" / "src").mkdir(parents=True)
    (f / "Photos Library.photoslibrary").mkdir()
    (f / "repo" / "src" / "a.py").write_text("x = 1\n")
    (f / "Photos Library.photoslibrary" / "p.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    (f / "memo.md").write_text("# Memo\n")
    order, _ = _index_walk(str(f), exclude=("repo",))
    assert order == ["memo.md"]


def test_a_secret_written_later_is_never_read_and_the_profile_names_no_document(tmp_path, monkeypatch):
    folder = _folder(tmp_path)
    svc = IntelService(str(folder), "k5", cache_dir=str(tmp_path / "c5"))
    svc.build()
    (folder / "credentials.json").write_text('{"key": "x"}')
    import builtins
    opened = []
    real_open = builtins.open
    monkeypatch.setattr(builtins, "open", lambda f, *a, **k: (opened.append(str(f)), real_open(f, *a, **k))[1])
    svc.apply_changes(["credentials.json"])
    monkeypatch.setattr(builtins, "open", real_open)
    assert "credentials.json" not in svc.index.files and not any("credentials.json" in o for o in opened)
    prof = svc.repo_profile()
    assert not any("Agreement" in s for s in prof["top_symbols"]) and prof["other_kinds"]["document"] >= 2



def test_gitignore_applies_without_git(tmp_path, monkeypatch):
    """06.11 report: in a folder without git (or on a machine without the git binary) no .gitignore applied."""
    from webbee.intel import service
    (tmp_path / ".gitignore").write_text("dumps/\n*.png\n")
    (tmp_path / "dumps").mkdir()
    (tmp_path / "dumps" / "big.sql").write_text("x")
    (tmp_path / "shot.png").write_bytes(b"\x89PNG")
    (tmp_path / "app.py").write_text("x = 1\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / ".gitignore").write_text("local.py\n")
    (tmp_path / "sub" / "local.py").write_text("y = 1\n")
    (tmp_path / "sub" / "keep.py").write_text("z = 1\n")
    rels, _stats, _skipped = service._index_listing(str(tmp_path))
    assert "app.py" in rels and "sub/keep.py" in rels
    assert not {"dumps/big.sql", "shot.png", "sub/local.py"} & set(rels)
    # the same with git present but failing (no binary): the rules still apply
    monkeypatch.setattr(service, "_git_listed", lambda root: None)
    assert "sub/local.py" not in service._index_listing(str(tmp_path))[0]
