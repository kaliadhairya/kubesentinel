import os
import glob
import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger("kubesentinel.runbooks")


class RunbookService:
    """Manages operational triage runbooks with automatic mtime hot-reloading.
    
    Allows on-call engineers to update markdown runbooks on disk without
    restarting the service.
    """

    def __init__(self, runbooks_dir: Optional[str] = None):
        self.runbooks_dir = runbooks_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "data",
            "runbooks",
        )
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._mtimes: Dict[str, float] = {}

    def _sync(self):
        """Scans the runbooks directory and reloads files whose mtime changed."""
        if not os.path.exists(self.runbooks_dir):
            os.makedirs(self.runbooks_dir, exist_ok=True)

        md_files = glob.glob(os.path.join(self.runbooks_dir, "*.md"))
        active_files = set(md_files)

        # Remove deleted files from cache
        for cached_file in list(self._cache.keys()):
            if cached_file not in active_files:
                del self._cache[cached_file]
                del self._mtimes[cached_file]
                logger.info(f"Removed deleted runbook from cache: {cached_file}")

        # Check for new or modified files
        for file_path in md_files:
            try:
                current_mtime = os.path.getmtime(file_path)
                if file_path not in self._mtimes or self._mtimes[file_path] < current_mtime:
                    self._load_file(file_path, current_mtime)
            except OSError as e:
                logger.error(f"Error checking mtime for {file_path}: {e}")

    def _load_file(self, file_path: str, mtime: float):
        """Loads and parses a single markdown runbook."""
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        basename = os.path.basename(file_path)
        slug = os.path.splitext(basename)[0]

        # Extract title from first # header or fallback to slug
        title = slug.replace("_", " ").title()
        for line in content.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break

        self._cache[file_path] = {
            "slug": slug,
            "title": title,
            "path": file_path,
            "content": content,
            "mtime": mtime,
        }
        self._mtimes[file_path] = mtime
        logger.info(f"Hot-reloaded runbook: '{title}' from {basename}")

    def get_all_runbooks(self) -> List[Dict[str, Any]]:
        """Returns all loaded runbooks."""
        self._sync()
        return list(self._cache.values())

    def search_runbook(self, query: str) -> Optional[Dict[str, Any]]:
        """Finds the best matching runbook for a given symptom, error code, or query."""
        self._sync()
        clean_query = query.lower()

        best_match = None
        best_score = 0

        for runbook in self._cache.values():
            score = 0
            # Match title
            if any(term in runbook["title"].lower() for term in clean_query.split()):
                score += 3
            # Match keywords in slug
            if any(term in runbook["slug"].lower() for term in clean_query.split()):
                score += 4
            # Match specific exit codes or patterns (e.g. 137, OOM, CrashLoop, 502)
            if "oom" in clean_query and "oom" in runbook["slug"]:
                score += 10
            if "137" in clean_query and "137" in runbook["content"]:
                score += 10
            if "crashloop" in clean_query and "crash" in runbook["slug"]:
                score += 10
            if ("5xx" in clean_query or "latency" in clean_query) and "latency" in runbook["slug"]:
                score += 10

            # Content match
            if clean_query in runbook["content"].lower():
                score += 2

            if score > best_score:
                best_score = score
                best_match = runbook

        return best_match if best_score > 0 else None


runbook_service = RunbookService()
