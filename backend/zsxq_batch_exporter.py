#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
知识星球批量导出器
按搜索条件批量导出话题（Markdown + 图片 + 附件文件）为 ZIP 包

复用 zsxq_markdown_exporter.build_topic_staging() 处理单话题的 Markdown/图片，
本模块仅负责：话题搜索、附件下载、批量编排和最终打包。
"""

import datetime
import os
import shutil
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests

from .db_path_manager import get_db_path_manager
from .image_cache_manager import get_image_cache_manager
from .zsxq_database import ZSXQDatabase
from .zsxq_markdown_exporter import (
    article_html_to_markdown,
    build_topic_staging,
    safe_filename,
    topic_detail_to_markdown,
)
from .zsxq_request_profiles import (
    build_zsxq_file_stream_headers,
    build_zsxq_mobile_headers,
    is_mobile_only_error,
)
from .zsxq_retry import (
    ensure_global_max_retries,
    retry_wait_seconds,
    should_retry_api_code,
)
from .logger_config import log_error, log_info, log_warning


class ZSXQBatchExporter:
    """批量导出器：搜索话题 → 按个调用 build_topic_staging() → 下载附件 → 打包 ZIP"""

    def __init__(self, group_id: str, cookie: str, export_dir: str,
                 log_callback: Optional[Callable[[str], None]] = None):
        self.group_id = str(group_id)
        self.cookie = cookie
        self.export_dir = export_dir
        self.log_callback = log_callback
        self.session = requests.Session()
        self.base_url = "https://api.zsxq.com"

        path_manager = get_db_path_manager()
        self.db_path = path_manager.get_topics_db_path(self.group_id)
        self.db = ZSXQDatabase(self.db_path)

        os.makedirs(self.export_dir, exist_ok=True)
        self.stop_flag = False

    def log(self, message: str):
        if self.log_callback:
            self.log_callback(message)
        else:
            log_info(message)

    def _fetch_article_content(self, article_url: str, title: str) -> Optional[str]:
        """抓取关联文章页面 HTML 并转 Markdown"""
        from urllib.parse import urlparse
        parsed = urlparse(article_url)
        if parsed.scheme not in {"http", "https"}:
            return None
        if self.is_stopped():
            return None
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
            # ZSXQ 文章页需要 Cookie 才能访问完整内容
            if "zsxq.com" in parsed.netloc:
                headers["Cookie"] = self.cookie
            resp = self.session.get(article_url, headers=headers, timeout=30, allow_redirects=True)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            if "json" in content_type:
                return None
            resp.encoding = resp.encoding or "utf-8"
            return article_html_to_markdown(resp.text, fallback_title=title)
        except Exception:
            return None

    def set_stop_flag(self):
        self.stop_flag = True
        self.log("收到停止信号，将在当前话题完成后停止")

    def is_stopped(self):
        return self.stop_flag

    # ------------------------------------------------------------------
    # 话题搜索
    # ------------------------------------------------------------------

    def search_topics(
        self,
        keyword: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        tag_ids: Optional[List[int]] = None,
    ) -> List[Dict[str, Any]]:
        """搜索匹配条件的话题，返回 [{topic_id, title, create_time}, ...]"""
        joins = []
        conditions = ["t.group_id = ?"]
        params: list = [int(self.group_id)]

        if tag_ids and len(tag_ids) > 0:
            placeholders = ", ".join("?" * len(tag_ids))
            joins.append("JOIN topic_tags tt ON t.topic_id = tt.topic_id")
            conditions.append(f"tt.tag_id IN ({placeholders})")
            params.extend(tag_ids)

        if keyword and keyword.strip():
            conditions.append("(t.title LIKE ? OR q.text LIKE ? OR tk.text LIKE ?)")
            kw = f"%{keyword.strip()}%"
            params.extend([kw, kw, kw])

        if start_date:
            conditions.append("date(t.create_time) >= date(?)")
            params.append(start_date)

        if end_date:
            conditions.append("date(t.create_time) <= date(?)")
            params.append(end_date)

        join_clause = " ".join(joins)
        where_clause = " AND ".join(conditions)
        query = f"""
            SELECT DISTINCT t.topic_id, t.title, t.create_time
            FROM topics t
            {join_clause}
            LEFT JOIN questions q ON t.topic_id = q.topic_id
            LEFT JOIN talks tk ON t.topic_id = tk.topic_id
            WHERE {where_clause}
            ORDER BY t.create_time DESC
        """
        self.db.cursor.execute(query, tuple(params))
        rows = self.db.cursor.fetchall()

        topics = []
        for row in rows:
            topics.append({
                "topic_id": str(row[0]) if row[0] is not None else None,
                "title": row[1] or f"topic_{row[0]}",
                "create_time": row[2],
            })
        return topics

    # ------------------------------------------------------------------
    # 批量导出主入口
    # ------------------------------------------------------------------

    def export_batch(
        self,
        keyword: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        tag_ids: Optional[List[int]] = None,
        download_files: bool = True,
        download_images: bool = True,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> Dict[str, Any]:
        """批量导出话题为 ZIP 文件"""
        self.log("正在搜索匹配的话题...")
        topics = self.search_topics(keyword, start_date, end_date, tag_ids)
        total = len(topics)

        if total == 0:
            self.log("没有找到匹配的话题")
            return {"zip_path": None, "total": 0, "exported": 0, "failed": 0, "errors": []}

        # 获取标签名映射 (tag_id -> tag_name)
        tag_names: Dict[int, str] = {}
        if tag_ids:
            placeholders = ", ".join("?" * len(tag_ids))
            self.db.cursor.execute(
                f"SELECT tag_id, tag_name FROM tags WHERE tag_id IN ({placeholders})",
                tuple(tag_ids),
            )
            for row in self.db.cursor.fetchall():
                tag_names[row[0]] = safe_filename(row[1], max_length=40)

        self.log(f"找到 {total} 个匹配话题，开始批量导出...")

        batch_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        staging_dir = os.path.join(self.export_dir, f"batch_staging_{batch_id}")
        os.makedirs(staging_dir, exist_ok=True)

        exported = 0
        failed = 0
        errors = []
        processed: set = set()  # (topic_id, tag_id) pairs to avoid internal dedup across tags

        for i, topic in enumerate(topics):
            if self.is_stopped():
                self.log("批量导出已停止")
                break

            topic_id = topic["topic_id"]
            title = topic["title"] or f"topic_{topic_id}"

            # 确定该话题需要放入哪些标签文件夹
            target_tags = {}
            if tag_ids:
                placeholders = ", ".join("?" * len(tag_ids))
                self.db.cursor.execute(
                    f"SELECT tt.tag_id FROM topic_tags tt WHERE tt.topic_id = ? AND tt.tag_id IN ({placeholders})",
                    (int(topic_id), *tag_ids),
                )
                for row in self.db.cursor.fetchall():
                    tid = row[0]
                    if tid in tag_names:
                        target_tags[tid] = tag_names[tid]

            if not target_tags:
                target_tags[0] = ""  # 无标签 → 放根目录

            for tid, tname in target_tags.items():
                if self.is_stopped():
                    break
                pair_key = (topic_id, tid)
                if pair_key in processed:
                    continue
                processed.add(pair_key)

                self.log(f"[{i + 1}/{total}] {title[:50]}" + (f" → {tname}" if tname else ""))

                try:
                    if tid == 0:
                        parent = staging_dir
                    else:
                        parent = os.path.join(staging_dir, tname)
                        os.makedirs(parent, exist_ok=True)

                    folder_name = safe_filename(title, max_length=60)
                    topic_dir = os.path.join(parent, folder_name)
                    if os.path.exists(topic_dir):
                        folder_name = f"{folder_name}_{tid}"
                        topic_dir = os.path.join(parent, folder_name)

                    self._export_single_topic(topic_id, topic_dir, download_files, download_images)
                    exported += 1
                except Exception as e:
                    failed += 1
                    errors.append(f"{title[:30]}: {e}")
                    self.log(f"  {e}")

            if progress_callback:
                progress_callback(i + 1, total, title)

        if exported == 0:
            self.log("没有成功导出任何话题")
            shutil.rmtree(staging_dir, ignore_errors=True)
            return {"zip_path": None, "total": total, "exported": 0, "failed": failed, "errors": errors}

        self.log("正在打包 ZIP...")
        zip_name = f"batch_export_{self.group_id}_{batch_id}.zip"
        zip_path = os.path.join(self.export_dir, zip_name)
        self._zip_directory(staging_dir, zip_path)
        shutil.rmtree(staging_dir, ignore_errors=True)

        self.log(f"导出完成: {exported}/{total}, 失败 {failed}")
        return {"zip_path": zip_path, "total": total, "exported": exported, "failed": failed, "errors": errors}

    def _export_single_topic(
        self,
        topic_id: str,
        topic_dir: str,
        download_files: bool = True,
        download_images: bool = True,
        ref_depth: int = 0,
    ):
        """导出单个话题到指定目录"""
        detail = self.db.get_topic_detail(int(topic_id))
        if not detail:
            raise FileNotFoundError(f"话题 {topic_id} 不存在于数据库中")

        image_downloader = None
        if download_images:
            cache_manager = get_image_cache_manager(self.group_id)
            def _download(url: str) -> Optional[Path]:
                if self.is_stopped():
                    return None
                try:
                    success, path, _err = cache_manager.download_and_cache(url, timeout=20)
                    if success and path:
                        return Path(path)
                except Exception:
                    pass
                return None
            image_downloader = _download

        file_map: Dict[str, str] = {}
        if download_files:
            file_map = self._download_topic_files(detail, topic_dir)

        def file_resolver(file_id, _name) -> Optional[str]:
            return file_map.get(str(file_id))

        render_kwargs = {
            "file_resolver": file_resolver,
            "include_comments": False,
            "article_fetcher": self._fetch_article_content,
        }

        title = detail.get("title") or f"topic_{topic_id}"
        md_name = safe_filename(title, max_length=80) + ".md"
        if not md_name or md_name == ".md":
            md_name = f"topic_{topic_id}.md"

        build_topic_staging(
            topic_dir,
            detail,
            render=topic_detail_to_markdown,
            render_kwargs=render_kwargs,
            image_downloader=image_downloader,
            md_filename=md_name,
        )

        # 导出正文中引用的 ZSXQ 内部链接（子话题）
        self._export_referenced_topics(topic_dir, detail, download_files, download_images, ref_depth)

    def _export_referenced_topics(
        self, parent_dir: str, detail: Dict[str, Any],
        download_files: bool, download_images: bool, ref_depth: int,
    ):
        """导出话题正文中引用的 ZSXQ 内部链接为子文件夹"""
        if ref_depth >= 2:  # 展开两层引用（话题 → 引用 → 引用的引用）
            return
        imported: set = set()
        for url, link_title in self._extract_zsxq_links(detail):
            if self.is_stopped():
                return
            topic_id = self._resolve_topic_id(url)
            if not topic_id or topic_id in imported:
                continue
            imported.add(topic_id)

            folder_name = safe_filename(link_title, max_length=60)
            if not folder_name:
                folder_name = f"topic_{topic_id}"
            sub_dir = os.path.join(parent_dir, folder_name)
            if os.path.exists(sub_dir):
                continue

            try:
                os.makedirs(sub_dir, exist_ok=True)
                self._export_single_topic(
                    str(topic_id), sub_dir, download_files, download_images, ref_depth + 1,
                )
                self.log(f"   📎 已导出引用: {link_title[:40]}")
            except Exception as e:
                self.log(f"   ⚠️ 引用导出失败: {link_title[:40]} - {e}")

    def _extract_zsxq_links(self, detail: Dict[str, Any]) -> List[tuple]:
        """从话题正文中提取 ZSXQ 内部链接，返回 [(url, title), ...]"""
        import re
        results: List[tuple] = []
        seen: set = set()

        raw_texts = []
        talk = detail.get("talk") or {}
        raw_texts.append(talk.get("text") or "")
        if detail.get("type") == "q&a":
            raw_texts.append((detail.get("question") or {}).get("text") or "")
            raw_texts.append((detail.get("answer") or {}).get("text") or "")

        for text in raw_texts:
            if not text or "<e" not in text:
                continue
            for m in re.finditer(r"""<e\s+([^/>]+?)/?\s*>""", text, re.IGNORECASE | re.DOTALL):
                attrs_str = m.group(1) or ""
                attrs = dict(re.findall(r"""(\w+)\s*=\s*"([^"]*)""""", attrs_str))
                etype = (attrs.get("type") or "").lower()
                if etype not in ("web_url", "web"):
                    continue
                from urllib.parse import unquote
                href = unquote(attrs.get("href", ""))
                link_title = unquote(attrs.get("title", "")) or href
                if not href or "zsxq.com" not in href or href in seen:
                    continue
                seen.add(href)
                results.append((href, link_title))
        return results

    def _resolve_topic_id(self, url: str) -> Optional[int]:
        """解析 ZSXQ 链接（短链接或话题链接）为 topic_id"""
        import re
        # 直接从 URL 提取
        m = re.search(r'/topic/(\d+)', url) or re.search(r'topic_id=(\d+)', url)
        if m:
            return int(m.group(1))
        # 短链接如 t.zsxq.com/xxx，跟随重定向获取真实 URL
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            resp = self.session.head(url, headers=headers, allow_redirects=True, timeout=10)
            m = re.search(r'/topic/(\d+)', resp.url) or re.search(r'topic_id=(\d+)', resp.url)
            if m:
                return int(m.group(1))
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # 附件下载
    # ------------------------------------------------------------------

    def _download_topic_files(self, detail: Dict[str, Any], topic_dir: str) -> Dict[str, str]:
        """下载话题附件到 topic_dir/files/，返回 file_id -> 相对路径 映射"""
        files_dir = os.path.join(topic_dir, "files")
        os.makedirs(files_dir, exist_ok=True)

        talk = detail.get("talk") or {}
        files = talk.get("files") or []
        file_map: Dict[str, str] = {}

        for f in files:
            if self.is_stopped():
                break
            file_id = f.get("file_id")
            file_name = f.get("name") or f"file_{file_id}"
            if file_id is None:
                continue

            safe_name = safe_filename(file_name, max_length=120)
            if not safe_name:
                safe_name = f"file_{file_id}"

            target_path = os.path.join(files_dir, safe_name)

            if not os.path.exists(target_path):
                if self._download_single_file(int(file_id), target_path):
                    file_map[str(file_id)] = f"./files/{safe_name}"
                    self.log(f"   📎 已下载: {file_name}")
                else:
                    self.log(f"   ⚠️ 下载失败: {file_name}")
            else:
                file_map[str(file_id)] = f"./files/{safe_name}"

        return file_map

    def _download_single_file(self, file_id: int, output_path: str) -> bool:
        """下载单个文件到指定路径"""
        if self.is_stopped():
            return False

        download_url = self._get_download_url(file_id)
        if not download_url:
            return False

        try:
            stream_headers = build_zsxq_file_stream_headers(self.cookie, self.group_id, include_cookie=False)
            response = self.session.get(download_url, headers=stream_headers, timeout=300, stream=True)

            if self.is_stopped():
                response.close()
                return False

            if response.status_code in [401, 403]:
                response.close()
                stream_headers = build_zsxq_file_stream_headers(self.cookie, self.group_id, include_cookie=True)
                response = self.session.get(download_url, headers=stream_headers, timeout=300, stream=True)

            if response.status_code == 200:
                with open(output_path, "wb") as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if self.is_stopped():
                            return False
                        if chunk:
                            f.write(chunk)
                return True
            else:
                self.log(f"   ❌ 文件下载 HTTP {response.status_code}: file_id={file_id}")
                return False
        except Exception as e:
            self.log(f"   ❌ 文件下载异常: file_id={file_id}, error={e}")
            return False

    def _get_download_url(self, file_id: int) -> Optional[str]:
        """获取文件下载链接"""
        url = f"{self.base_url}/v2/files/{file_id}/download_url"
        max_retries = ensure_global_max_retries(5)

        for attempt in range(max_retries):
            if self.is_stopped():
                return None
            if attempt > 0:
                import time as _time
                import random as _random
                _time.sleep(_random.uniform(15, 30))

            headers = build_zsxq_mobile_headers(self.cookie, self.group_id)
            try:
                response = self.session.get(url, headers=headers, timeout=30)
                if response.status_code == 200:
                    data = response.json()
                    if data.get("succeeded"):
                        dl_url = data.get("resp_data", {}).get("download_url")
                        if dl_url:
                            return dl_url
                    else:
                        error_code = data.get("code")
                        error_msg = data.get("message", data.get("error", ""))
                        if is_mobile_only_error(error_code, error_msg):
                            return None
                        if should_retry_api_code(error_code, attempt, max_retries):
                            import time as _time
                            _time.sleep(retry_wait_seconds(attempt))
                            continue
                        return None
                elif response.status_code in [429, 500, 502, 503, 504]:
                    if attempt < max_retries - 1:
                        continue
                return None
            except Exception:
                if attempt < max_retries - 1:
                    continue
                return None

        return None

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def _zip_directory(self, source_dir: str, zip_path: str) -> str:
        """将目录打包为 ZIP"""
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, files in os.walk(source_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, source_dir)
                    zf.write(file_path, arcname)
        return zip_path

    def close(self):
        """关闭资源"""
        try:
            self.db.close()
        except Exception:
            pass
