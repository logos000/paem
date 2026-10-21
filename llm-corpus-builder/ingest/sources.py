"""Source adapters for the crawled official materials.

Each adapter yields :class:`SourceItem` records.  Pages are parsed here (the
site template decides where the body, title, date and breadcrumb live);
attachments are only located here and extracted later in ``build.py``.

Supported layouts
-----------------
* ``zju-sites/archive/<site>/_raw/<host>/...``  Sudy CMS pages (ugrs, grs, zju,
  news, libweb) and Drupal pages (zjui); ``_index.jsonl`` lists url/title/rel.
  Attachments live at ``<site>/<host>/<url path>``.
* ``ZJUIntl/sites/ZJUIUG``  SharePoint export: ``_page-content-raw/*.canvas.html``
  (clean RTE HTML), ``_pages-index.json`` / ``_page-content-text/_render-index.json``
  (titles, urls, modified) and ``Shared Documents/`` (files).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any, Iterator
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from structure.extract import RawBlock, heading_level, html_blocks
from evidence.store import canonical_url, normalize_text

ATTACHMENT_EXTS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".xlsm", ".ppt", ".pptx", ".txt", ".zip", ".rar", ".7z", ".png", ".jpg", ".jpeg", ".gif", ".mp4", ".wps", ".et"}
DOC_EXTS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".xlsm", ".txt", ".wps"}
SUDY_SITES = {"ugrs", "grs", "zju", "news", "libweb"}
# A crawl follows links off its own host; the page belongs to the host's site
# (template, audience, tier), not to the archive directory it was saved in.
HOST_SITES = {
    "zjui.intl.zju.edu.cn": "zjui",
    "grs.zju.edu.cn": "grs",
    "ugrs.zju.edu.cn": "ugrs",
    "zju.edu.cn": "zju",
    "news.zju.edu.cn": "news",
    "libweb.zju.edu.cn": "libweb",
    "iczu.zju.edu.cn": "iczu",
    "zibs.zju.edu.cn": "zibs",
}
# Every site whose pages/attachments live under zju-sites/archive/<site>/.
# Single authority for rel-path mapping (pipeline, bench chunkers, snapshot).
ARCHIVE_SITES = frozenset(SUDY_SITES) | frozenset(HOST_SITES.values()) | frozenset({"gfxwj"})


def site_for_url(url: str, archive_site: str) -> str:
    host = urlparse(url).netloc.lower().split(":")[0].removeprefix("www.")
    if not host:
        return archive_site
    if host in HOST_SITES:
        return HOST_SITES[host]
    return archive_site if host.endswith("zju.edu.cn") else "external"

BOILERPLATE_RE = re.compile(
    r"^(?:分享到[:：]?.*|打印本页|关闭窗口|【?(?:打印|关闭)】?|上一篇[:：].*|下一篇[:：].*|浏览次数[:：]?\s*\d*|点击[:：]?\s*\d*|"
    r"版权所有.*|Copyright.*|地址[:：].{0,60}邮编.*|.{0,20}ICP备.*|访问量.*|当前位置[:：].*|发布时间[:：].{0,30}|作者[:：].{0,30}|来源[:：].{0,30})$",
    re.I,
)


@dataclass
class AttachmentLink:
    url: str
    label: str
    local_path: Path | None
    embedded: bool = False  # Sudy pdf viewer (`pdfsrc`) rather than a hyperlink


@dataclass
class SourceItem:
    site: str
    kind: str  # page | attachment
    source_url: str
    local_path: Path
    rel_path: str
    title: str = ""
    title_source: str = ""
    published: str = ""
    modified: str = ""
    # P1-5: how ``published`` was obtained — "date_node" / "url_pattern" /
    # "modified_fallback" (SharePoint mtime; NOT a real publication date).
    date_provenance: str = ""
    breadcrumb: list[str] = field(default_factory=list)
    blocks: list[RawBlock] = field(default_factory=list)
    page_kind: str = "article"  # article | listing | shell | home
    attachments: list[AttachmentLink] = field(default_factory=list)
    parent_url: str = ""
    link_label: str = ""
    raw_text_path: Path | None = None
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- helpers


def _clean_title(value: str) -> str:
    value = normalize_text(value)
    value = re.sub(r"\s*[|｜]\s*浙江大学ZJUI.*$", "", value)
    value = re.sub(r"\s*[-_|]\s*(?:浙江大学(?:本科生院|研究生院|新闻网|图书馆)?|Zhejiang University.*)$", "", value)
    return value.strip()


def _ymd(year: int, month: int, day: int) -> str:
    # Some templates print year-day-month; swap when only that reading is valid.
    if month > 12 and day <= 12:
        month, day = day, month
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return ""
    return f"{year:04d}-{month:02d}-{day:02d}"


def _date(value: str) -> str:
    value = normalize_text(value)
    match = re.search(r"((?:19|20)\d{2})\s*[年/.-]\s*(\d{1,2})\s*[月/.-]\s*(\d{1,2})", value)
    if match:
        return _ymd(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    match = re.search(r"(\d{1,2})/(\d{1,2})/((?:19|20)\d{2})", value)
    if match:
        return _ymd(int(match.group(3)), int(match.group(1)), int(match.group(2)))
    return ""


def _drop_boilerplate(blocks: list[RawBlock]) -> list[RawBlock]:
    return [block for block in blocks if not (block.kind != "table_row" and BOILERPLATE_RE.match(block.text))]


def _attachment_ext(url: str) -> str:
    return Path(unquote(urlparse(url).path)).suffix.lower()


def _local_for_url(site_dir: Path, url: str) -> Path | None:
    parsed = urlparse(url)
    if not parsed.netloc:
        return None
    candidate = site_dir / parsed.netloc / unquote(parsed.path).lstrip("/")
    if candidate.is_file():
        return candidate
    alt = site_dir / parsed.netloc.removeprefix("www.") / unquote(parsed.path).lstrip("/")
    return alt if alt.is_file() else None


def _links(root: Tag, base_url: str, site_dir: Path) -> list[AttachmentLink]:
    found: dict[str, AttachmentLink] = {}
    for node in root.find_all(attrs={"pdfsrc": True}):
        url = urljoin(base_url, node["pdfsrc"])
        label = ""
        match = re.search(r"'title'\s*:\s*'([^']*)'", node.get("sudyfile-attr", ""))
        if match:
            label = match.group(1)
        found[canonical_url(url)] = AttachmentLink(url, normalize_text(label), _local_for_url(site_dir, url), embedded=True)
    for anchor in root.find_all("a", href=True):
        url = urljoin(base_url, anchor["href"])
        if _attachment_ext(url) not in ATTACHMENT_EXTS:
            continue
        key = canonical_url(url)
        label = normalize_text(anchor.get_text(" ")) or normalize_text(anchor.get("title", ""))
        sudy = re.search(r"'title'\s*:\s*'([^']*)'", anchor.get("sudyfile-attr", ""))
        if sudy:
            label = normalize_text(sudy.group(1))
        if key in found:
            found[key].label = found[key].label or label
            continue
        found[key] = AttachmentLink(url, label, _local_for_url(site_dir, url))
    return list(found.values())


# ---------------------------------------------------------------- Sudy / Drupal pages

SUDY_BODY = ("div.wp_articlecontent", ".article-content", ".article_content", ".Article_Content", "div.read", "#vsb_content", ".v_news_content", ".content-txt", ".article")
# Generic "h1"/"h3.title" are sidebar headings ("校内链接") on Sudy column pages.
SUDY_TITLE = (".article-title h3", ".article-title h1", ".article-title", "h1.arti_title", ".arti_title", "h2.article_title", ".article_title")
# ``.Article_Title`` is also the class of sidebar list entries on Sudy sites;
# only trust it when the page has exactly one.
SUDY_SINGLE_TITLE = (".Article_Title",)
DRUPAL_BODY = ("div.newsInfo div.con", "div.newsInfo", "div.main")


def _first(soup: BeautifulSoup, selectors: tuple[str, ...]) -> tuple[Tag | None, str]:
    for selector in selectors:
        node = soup.select_one(selector)
        if node is not None and normalize_text(node.get_text(" ")):
            return node, selector
    return None, ""


def _sudy_breadcrumb(soup: BeautifulSoup) -> list[str]:
    for node in soup.select(".position, .possplit, .breadcrumb, .wp_position, .crumbs, .location"):
        container = node if node.name != "span" else node.parent
        if container is None:
            continue
        crumbs = [normalize_text(a.get_text(" ")) for a in container.find_all("a")]
        crumbs = [c for c in crumbs if c and c not in ("首页", "Home")]
        if crumbs:
            return crumbs
    return []


def _listing_like(root: Tag, blocks: list[RawBlock]) -> bool:
    links = len(root.find_all("a", href=True))
    text = sum(len(block.text) for block in blocks)
    return links >= 12 and text / max(1, links) < 40


def parse_cms_page(site: str, site_dir: Path, url: str, html: str, index_title: str = "") -> SourceItem:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    item = SourceItem(site=site, kind="page", source_url=url, local_path=Path(), rel_path="")
    if site == "zjui":
        # The banner repeats the section name ("教育教学") in its own div.con.
        banner = soup.select_one("div.main div.banner") or soup.select_one("div.banner")
        if banner is not None:
            section = normalize_text(banner.get_text(" "))
            item.breadcrumb = [section] if section else []
            banner.decompose()
        for tag in soup.select("header, footer, nav, div.header, div.footer"):
            tag.decompose()
        root, selector = _first(soup, DRUPAL_BODY)
        title_node = soup.select_one("div.newsInfo .tits")
        date_node = soup.select_one("div.newsInfo .tit span")
        attach_root = soup.select_one("div.file_upload")
    else:
        root, selector = _first(soup, SUDY_BODY)
        title_node, _ = _first(soup, SUDY_TITLE) if root is not None else (None, "")
        if title_node is None and root is not None:
            single = soup.select(SUDY_SINGLE_TITLE[0])
            title_node = single[0] if len(single) == 1 else None
        date_node = None
        for node in soup.select(".article-time, .arti_update, .Article_PublishDate, .article-title p, .arti_metas, .news_meta, .article-info"):
            if _date(node.get_text(" ")):
                date_node = node
                break
        attach_root = None
        item.breadcrumb = _sudy_breadcrumb(soup)
    if title_node is not None:
        item.title, item.title_source = _clean_title(title_node.get_text(" ")), "page"
    if not item.title:
        item.title, item.title_source = _clean_title(soup.title.get_text(" ") if soup.title else ""), "html_title"
    if not item.title or item.title in ("ZJUI", "浙江大学"):
        item.title, item.title_source = _clean_title(index_title), "index_title"
    if (not item.title or item.title in ("ZJUI", "浙江大学")) and item.breadcrumb:
        item.title, item.title_source = item.breadcrumb[-1], "breadcrumb"
    if date_node is not None:
        item.published = _date(date_node.get_text(" "))
        item.date_provenance = "date_node"
    if not item.published:
        match = re.search(r"/((?:19|20)\d{2})/(\d{2})(\d{2})/c\d+a\d+/page\.htm", url)
        if match:
            item.published = _ymd(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            item.date_provenance = "url_pattern"
    if root is None:
        item.warnings.append("no_body_selector")
        root = soup.body or soup
        for tag in root.find_all(["header", "footer", "nav"]):
            tag.decompose()
        item.page_kind = "listing"
    else:
        item.warnings.append(f"body:{selector}")
    item.attachments = _links(root, url, site_dir)
    if attach_root is not None:
        known = {canonical_url(a.url) for a in item.attachments}
        item.attachments += [a for a in _links(attach_root, url, site_dir) if canonical_url(a.url) not in known]
    item.blocks = _drop_boilerplate(html_blocks(root))
    if item.title and item.blocks and item.blocks[0].kind in ("heading", "paragraph") and normalize_text(item.blocks[0].text) == item.title:
        item.blocks = item.blocks[1:]
    if item.page_kind != "listing":
        if _listing_like(root, item.blocks):
            item.page_kind = "listing"
        elif sum(len(b.text) for b in item.blocks) < 40 and item.attachments:
            item.page_kind = "shell"
        elif sum(len(b.text) for b in item.blocks) < 40 and root.find("img") is not None:
            item.page_kind = "shell"
            item.warnings.append("image_only_body")
    if re.search(r"/(?:main|index)\.htm[l]?$|^https?://[^/]+/?$", url):
        item.page_kind = "home"
    return item


def iter_cms(archive: Path, site: str, *, pages: bool = True) -> Iterator[SourceItem]:
    site_dir = archive / site
    index = site_dir / "_index.jsonl"
    if not index.is_file():
        return
    with open(index, encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            rel = row.get("rel", "").replace("\\", "/")
            path = site_dir / "_raw" / rel
            if not path.is_file():
                continue
            raw = path.read_bytes()
            html = None
            for encoding in ("utf-8", "gb18030"):
                try:
                    html = raw.decode(encoding)
                    break
                except UnicodeDecodeError:
                    continue
            html = html if html is not None else raw.decode("utf-8", "replace")
            item = parse_cms_page(site_for_url(row["url"], site), site_dir, row["url"], html, row.get("title", ""))
            item.local_path = path
            item.rel_path = f"{site}/_raw/{rel}"
            # The crawler stores page text next to the HTML path with a .txt suffix.
            text_path = site_dir / "_text" / rel
            candidates = (text_path.with_suffix(".txt"), Path(f"{text_path}.txt"), text_path)
            item.raw_text_path = next((p for p in candidates if p.is_file()), None)
            yield item


def iter_cms_orphans(archive: Path, site: str, linked: set[Path]) -> Iterator[SourceItem]:
    """Attachments on disk that no parsed page links to (still citable by URL)."""
    site_dir = archive / site
    for host_dir in site_dir.iterdir():
        if not host_dir.is_dir() or host_dir.name.startswith("_"):
            continue
        for path in host_dir.rglob("*"):
            if path.is_file() and path.suffix.lower() in DOC_EXTS and path.resolve() not in linked:
                rel = path.relative_to(host_dir).as_posix()
                yield SourceItem(site=site_for_url(f"https://{host_dir.name}/", site), kind="attachment", source_url=f"https://{host_dir.name}/{rel}", local_path=path,
                                 rel_path=f"{site}/{host_dir.name}/{rel}", title=path.stem, title_source="filename")


# ---------------------------------------------------------------- SharePoint

SP_BASE = "https://zjuintl.sharepoint.com/sites/ZJUIUG"


SP_CHROME = {"zjui_undergraded", "zjui undergraded"}


def rendered_text_blocks(text: str, title: str) -> list[RawBlock]:
    lines = [normalize_text(line) for line in text.splitlines()]
    # Header written by the renderer: title, ====, url.
    if len(lines) > 2 and set(lines[1]) == {"="}:
        lines = lines[3:]
    blocks: list[RawBlock] = []
    for line in lines:
        if not line or line == title or line.casefold() in SP_CHROME or not re.search(r"\w", line):
            continue
        level = heading_level(line)
        blocks.append(RawBlock("heading" if level else "paragraph", line, level=level))
    return blocks


def iter_sharepoint(root: Path) -> Iterator[SourceItem]:
    site_root = root / "sites" / "ZJUIUG"
    raw_dir = site_root / "_page-content-raw"
    pages_index = {row["file"]: row for row in json.loads((raw_dir / "_pages-index.json").read_text(encoding="utf-8"))} if (raw_dir / "_pages-index.json").is_file() else {}
    render_path = site_root / "_page-content-text" / "_render-index.json"
    render_index = {row["name"]: row for row in json.loads(render_path.read_text(encoding="utf-8"))} if render_path.is_file() else {}
    for path in sorted(raw_dir.glob("*.canvas.html")):
        name = path.name[: -len(".canvas.html")]
        meta = pages_index.get(name, {})
        render = render_index.get(name, {})
        url = render.get("url") or f"{SP_BASE}/SitePages/{name}.aspx"
        soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "lxml")
        item = SourceItem(site="sharepoint", kind="page", source_url=url, local_path=path, rel_path=f"sharepoint/_page-content-raw/{path.name}")
        item.title = normalize_text(meta.get("title") or render.get("title") or name.replace("-", " "))
        item.title_source = "pages_index" if meta.get("title") else "render_index" if render.get("title") else "filename"
        item.modified = str(meta.get("modified", ""))
        item.published = item.modified[:10]
        item.date_provenance = "modified_fallback"
        blocks: list[RawBlock] = []
        tables = 0
        for rte in soup.select("div[data-sp-rte]"):
            prefix = f"r{tables}"
            tables += 1
            blocks.extend(html_blocks(rte, table_prefix=prefix))
        text_path = site_root / "_page-content-text" / f"{name}.txt"
        if not blocks and text_path.is_file():
            # Some canvases were saved empty; the browser-rendered text survives
            # (tables flattened, so these pages are flagged).
            blocks = rendered_text_blocks(text_path.read_text(encoding="utf-8", errors="replace"), item.title)
            item.warnings.append("rendered_text_fallback")
        if not blocks:
            item.warnings.append("no_rte_content")
        item.blocks = _drop_boilerplate(blocks)
        links = []
        for rte in soup.select("div[data-sp-rte]"):
            links += _links(rte, url, site_root)
        for link in links:
            parsed = urlparse(link.url)
            if "/sites/ZJUIUG/" in parsed.path:
                local = site_root / unquote(parsed.path.split("/sites/ZJUIUG/", 1)[1])
                link.local_path = local if local.is_file() else None
        item.attachments = links
        if sum(len(b.text) for b in item.blocks) < 40:
            item.page_kind = "shell"
        if name.casefold() == "home":
            item.page_kind = "home"
        item.raw_text_path = text_path if text_path.is_file() else None
        yield item


def iter_sharepoint_files(root: Path) -> Iterator[SourceItem]:
    site_root = root / "sites" / "ZJUIUG"
    modified: dict[str, str] = {}
    index = root / "_file-index.csv"
    if index.is_file():
        import csv
        with open(index, encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                modified[row["path"].split("/sites/ZJUIUG/", 1)[-1]] = row.get("modified", "")
    docs = site_root / "Shared Documents"
    for path in sorted(docs.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in DOC_EXTS or "Forms" in path.relative_to(docs).parts[:1]:
            continue
        rel = path.relative_to(site_root).as_posix()
        item = SourceItem(site="sharepoint", kind="attachment", source_url=f"{SP_BASE}/{rel.replace(' ', '%20')}", local_path=path,
                          rel_path=f"sharepoint/{rel}", title=path.stem, title_source="filename")
        item.modified = modified.get(rel, "")
        item.published = item.modified[:10]
        item.date_provenance = "modified_fallback"
        yield item


# ---------------------------------------------------------------- 规范性文件库 (gfxwj)


def gfxwj_load(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Site metadata + all doc info records from the gfxwj crawl tree."""
    site_dir = root / "zju-sites" / "archive" / "gfxwj"
    meta = json.loads((site_dir / "_meta.json").read_text(encoding="utf-8"))
    records = []
    for path in sorted((site_dir / "docs").glob("*/info.json")):
        records.append(json.loads(path.read_text(encoding="utf-8")))
    return meta, records


def _gfxwj_blocks(record: dict[str, Any]) -> list[RawBlock]:
    # 单个异构段落：跨文档同构的属性行会被构建期的模板块清理整批删掉，
    # 合并后的整段文本因含文号/单位等文档专属值而保留。
    row = (
        f"发文单位：{record.get('orgName', '')}；文号：{record.get('fileNum', '')}；"
        f"时效性：{record.get('timeliness', '')}；制度层级：{record.get('wjcj', '')}；"
        f"分类：{record.get('firstLevel', '')}／{record.get('secondLevel', '')}／{record.get('threeLevel', '')}；"
        f"印发日期：{record.get('printDate', '')}；执行日期：{record.get('executeDate', '')}。"
        "文件正文为其 PDF 附件（学校红头文件）。"
    )
    return [RawBlock("paragraph", normalize_text(row))]


def iter_gfxwj(crawl: Path) -> Iterator[SourceItem]:
    """规范性文件 metadata pages: one per doc (info.json), filtered by _included.json."""
    site_dir = crawl / "zju-sites" / "archive" / "gfxwj"
    if not site_dir.is_dir():
        return
    meta, records = gfxwj_load(crawl)
    included = set(json.loads((site_dir / "_included.json").read_text(encoding="utf-8"))) if (site_dir / "_included.json").is_file() else None
    enc_base = str(meta.get("webvpn_base", ""))
    for record in records:
        if included is not None and record["id"] not in included:
            continue
        info_path = site_dir / "docs" / record["id"] / "info.json"
        item = SourceItem(
            site="gfxwj", kind="page",
            source_url=f"{enc_base}/xwfw/gfxwj/tpl/template-1/info.html?id={record['id']}&list=gfxwj",
            local_path=info_path, rel_path=f"gfxwj/docs/{record['id']}/info.json",
            title=normalize_text(record.get("title", "")), title_source="api_title",
        )
        item.published = str(record.get("printDate", ""))[:10]
        item.date_provenance = "print_date"
        first_level = str(record.get("firstLevel", ""))
        item.breadcrumb = ["规范性文件", first_level] if first_level else ["规范性文件"]
        item.blocks = _gfxwj_blocks(record)
        yield item


def iter_gfxwj_files(crawl: Path) -> Iterator[SourceItem]:
    """规范性文件 PDF 附件（红头文件正文），source_url 用真实 loadFile 地址。

    loadFile 的 URL 路径相同、仅 query 不同，无法走页面链接的扩展名门禁，
    所以按 SharePoint Shared Documents 的方式作为独立附件源注册。
    """
    site_dir = crawl / "zju-sites" / "archive" / "gfxwj"
    if not site_dir.is_dir():
        return
    meta, records = gfxwj_load(crawl)
    included = set(json.loads((site_dir / "_included.json").read_text(encoding="utf-8"))) if (site_dir / "_included.json").is_file() else None
    enc_base = str(meta.get("webvpn_base", ""))
    p_param = "?_p=YXM9MSZwPTEmbT1OJg__&"
    for record in records:
        if included is not None and record["id"] not in included:
            continue
        wjmc = str(record.get("wjmc", "") or "").strip()
        if not wjmc:
            continue
        title = normalize_text(record.get("title", "")) or record["id"]
        safe = re.sub(r'[<>:"/\\|?*\r\n\t]', "_", title).strip(" ._")[:120] or "untitled"
        for name in (f"{safe}.pdf", f"{safe}__{record['id']}.pdf"):
            path = site_dir / "files" / name
            if path.is_file():
                item = SourceItem(
                    site="gfxwj", kind="attachment",
                    source_url=f"{enc_base}/mobile/loadFile.do{p_param}ids={wjmc}&types=1",
                    local_path=path, rel_path=f"gfxwj/files/{name}",
                    title=title, title_source="api_title",
                )
                item.modified = str(record.get("printDate", ""))[:10]
                item.published = item.modified
                item.date_provenance = "print_date"
                yield item
                break
