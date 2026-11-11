"""Remembers past rule prompts so frequently used ones are one click away."""

import json
import threading
import time

from .config import DATA_DIR


def _key(text: str) -> str:
    return " ".join(text.lower().split())


class PromptHistory:
    def __init__(self):
        self.path = DATA_DIR / "prompts.json"
        self.lock = threading.Lock()
        try:
            self.items: list[dict] = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.items = []

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.items, indent=1))

    def record(self, text: str, minutes: float):
        with self.lock:
            k = _key(text)
            item = next((i for i in self.items if _key(i["text"]) == k), None)
            if item is None:
                item = {"text": text.strip(), "count": 0}
                self.items.append(item)
            item["count"] += 1
            item["last_used"] = time.time()
            item["minutes"] = minutes
            self._save()

    def forget(self, text: str):
        with self.lock:
            k = _key(text)
            self.items = [i for i in self.items if _key(i["text"]) != k]
            self._save()

    def top(self, n: int = 8) -> list[dict]:
        """Most used first; ties broken by most recent."""
        with self.lock:
            return sorted(self.items, key=lambda i: (-i["count"], -i.get("last_used", 0)))[:n]
