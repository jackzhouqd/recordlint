"""档案挂载点与目录下钻。

界面必须能选到 `records/20/batch-0001` 这种深层档案，
也必须能在不重启服务的前提下增加新来源（网络盘、U 盘、另一盘符）；
与此同时，浏览器传来的任何路径都不得越出挂载点——这是本模块的安全边界。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qaudit import jobs


@pytest.fixture
def srcs(tmp_path: Path) -> Path:
    """两个来源，其中一个是真实的两层结构。"""
    deep = tmp_path / "srcA" / "records" / "20" / "batch-0001"
    deep.mkdir(parents=True)
    (deep / "p1.jpg").write_bytes(b"x")
    (deep / "p2.jpg").write_bytes(b"x")
    (tmp_path / "srcB" / "档案2").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def mounts(srcs: Path) -> list:
    return [jobs.Mount(name="archives-a", path=srcs / "srcA"),
            jobs.Mount(name="扫描投递", path=srcs / "srcB")]


# ---------------------------------------------------------------- 配置加载
def test_load_mounts_reads_every_configured_source(tmp_path: Path):
    cfg = tmp_path / "archives.yaml"
    cfg.write_text(
        "mounts:\n"
        f"  - name: archives-a\n    path: {tmp_path.as_posix()}/srcA\n    note: 2026 年\n"
        f"  - name: 扫描投递\n    path: {tmp_path.as_posix()}/srcB\n",
        encoding="utf-8")

    got = jobs.load_mounts(cfg)

    assert [m.name for m in got] == ["archives-a", "扫描投递"]
    assert got[0].path == (tmp_path / "srcA").resolve()
    assert got[0].note == "2026 年"


def test_load_mounts_falls_back_to_single_root_when_config_missing(tmp_path: Path):
    """没有配置文件时退回 --archive-root 的旧行为，老部署不受影响。"""
    got = jobs.load_mounts(tmp_path / "缺失.yaml", fallback_root=tmp_path)

    assert len(got) == 1 and got[0].path == tmp_path.resolve()


def test_load_mounts_rejects_name_with_separator(tmp_path: Path):
    """挂载点名会成为 rel 的第一段，含分隔符就能伪造层级。"""
    cfg = tmp_path / "archives.yaml"
    cfg.write_text(f"mounts:\n  - name: a/../b\n    path: {tmp_path.as_posix()}\n",
                   encoding="utf-8")

    with pytest.raises(ValueError, match="挂载点名"):
        jobs.load_mounts(cfg)


def test_load_mounts_rejects_duplicate_name(tmp_path: Path):
    cfg = tmp_path / "archives.yaml"
    cfg.write_text(
        f"mounts:\n  - name: 同名\n    path: {tmp_path.as_posix()}\n"
        f"  - name: 同名\n    path: {tmp_path.as_posix()}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="重名"):
        jobs.load_mounts(cfg)


def test_mounts_survive_save_and_reload(tmp_path: Path):
    """管理界面改完要能热生效，落盘格式必须自己读得回来。"""
    cfg = tmp_path / "archives.yaml"
    jobs.save_mounts(cfg, [jobs.Mount(name="新来源", path=tmp_path, note="共享盘")])

    got = jobs.load_mounts(cfg)

    assert len(got) == 1 and got[0].name == "新来源" and got[0].note == "共享盘"


# ---------------------------------------------------------------- 浏览下钻
def test_root_listing_shows_mount_points(mounts):
    listing = jobs.list_archives(mounts)

    assert [i["name"] for i in listing["items"]] == ["archives-a", "扫描投递"]
    assert all(i["kind"] == "挂载点" for i in listing["items"])
    assert listing["parent"] is None


def test_can_drill_down_to_deep_archive(mounts):
    """真实层级是 挂载点/records/20/batch-0001，必须逐层选得到。"""
    rel = "archives-a/records/20"

    listing = jobs.list_archives(mounts, rel)

    assert [i["rel"] for i in listing["items"]] == [f"{rel}/batch-0001"]
    assert listing["items"][0]["pages"] == 2


def test_listing_gives_breadcrumbs_and_parent(mounts):
    listing = jobs.list_archives(mounts, "archives-a/records/20")

    assert [c["name"] for c in listing["crumbs"]] == ["archives-a", "records", "20"]
    assert listing["crumbs"][1]["rel"] == "archives-a/records"
    assert listing["parent"] == "archives-a/records"


def test_listing_can_skip_page_counting(mounts):
    """网络盘上 rglob 统计页数很慢，条目多时要能整层关掉。"""
    listing = jobs.list_archives(mounts, "archives-a/records/20", with_pages=False)

    assert listing["items"][0]["pages"] is None


# ---------------------------------------------------------------- 安全边界
def test_resolve_target_finds_path_under_mount(mounts, srcs):
    got = jobs.resolve_target(mounts, "archives-a/records/20/batch-0001")

    assert got == (srcs / "srcA" / "records" / "20" / "batch-0001").resolve()


def test_resolve_target_rejects_traversal_out_of_mount(mounts):
    with pytest.raises(ValueError):
        jobs.resolve_target(mounts, "archives-a/../../etc")


def test_resolve_target_rejects_unknown_mount(mounts):
    with pytest.raises(ValueError):
        jobs.resolve_target(mounts, "不存在的挂载点/x")


def test_resolve_target_rejects_absolute_path_injection(mounts):
    """Path 的 / 运算符遇到绝对路径会丢弃左侧，必须挡在归属校验上。"""
    for evil in ("archives-a/C:/Windows", "archives-a//etc/passwd", "archives-a/\\\\nas\\share"):
        with pytest.raises((ValueError, FileNotFoundError)):
            jobs.resolve_target(mounts, evil)


def test_resolve_target_still_accepts_plain_root(srcs):
    """旧调用（单根 + 纯相对路径）必须继续可用。"""
    root = srcs / "srcA"

    assert jobs.resolve_target(root, "records") == (root / "records").resolve()
    with pytest.raises(ValueError):
        jobs.resolve_target(root, "../../etc")


def test_listing_of_unknown_mount_is_empty_not_crash(mounts):
    assert jobs.list_archives(mounts, "查无此挂载点/x")["items"] == []


# ---------------------------------------------------------------- 服务端目录选择
def test_dir_browser_lists_drives_at_root():
    """浏览器拿不到本机绝对路径，只能在服务端逐层点选——起点是盘符（或 /）。"""
    listing = jobs.list_dirs("")

    assert listing["items"], "至少要能列出一个盘符 / 根"
    assert listing["parent"] is None
    assert all(Path(i["path"]).exists() for i in listing["items"])


def test_dir_browser_lists_only_directories(tmp_path: Path):
    """选的是目录，列出文件只会干扰——而且这不该变成一个文件浏览器。"""
    (tmp_path / "子目录").mkdir()
    (tmp_path / "档案.pdf").write_bytes(b"x")

    names = [i["name"] for i in jobs.list_dirs(str(tmp_path))["items"]]

    assert names == ["子目录"]


def test_dir_browser_skips_hidden_directories(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "正常").mkdir()

    assert [i["name"] for i in jobs.list_dirs(str(tmp_path))["items"]] == ["正常"]


def test_dir_browser_gives_parent_and_crumbs(tmp_path: Path):
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)

    listing = jobs.list_dirs(str(deep))

    assert listing["parent"] == str(deep.parent)
    assert [c["name"] for c in listing["crumbs"]][-2:] == ["a", "b"]
    assert listing["path"] == str(deep.resolve())


def test_dir_browser_survives_unreachable_path(tmp_path: Path):
    """网络盘掉线、无权限的目录不能让页面 500。"""
    listing = jobs.list_dirs(str(tmp_path / "查无此目录"))

    assert listing["items"] == [] and listing["error"]


# ---------------------------------------------------------------- 自由路径逃生通道
def test_custom_path_needs_explicit_switch(tmp_path: Path):
    """默认关闭：不开开关就不能用绝对路径绕过挂载点。"""
    with pytest.raises(ValueError, match="未启用"):
        jobs.resolve_custom(str(tmp_path), allowed=False)


def test_custom_path_accepts_absolute_path_when_allowed(tmp_path: Path):
    assert jobs.resolve_custom(str(tmp_path), allowed=True) == tmp_path.resolve()


def test_custom_path_rejects_missing_path(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        jobs.resolve_custom(str(tmp_path / "不存在"), allowed=True)
