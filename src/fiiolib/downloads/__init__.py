"""Download package."""

from fiiolib.downloads.manager import DownloadManager, QueueResult
from fiiolib.downloads.qbittorrent import QBittorrentClient, QBittorrentError

__all__ = ["DownloadManager", "QBittorrentClient", "QBittorrentError", "QueueResult"]
