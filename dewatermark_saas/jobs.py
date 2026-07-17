"""内存 job store + 状态机（Demo 阶段够用；进程重启丢 job，YAGNI 不上库）。

所有访问都在 asyncio 事件循环单线程内，故用普通 dict + asyncio.Queue，无需额外锁。
"""
import asyncio
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, Literal, Optional

Status = Literal[
    "queued",         # 已入队，等 worker
    "resolving",      # 解析分享链接
    "downloading",    # 下载源视频
    "dewatermarking", # 调 ProPainter 去水印
    "done",           # 完成，可下载
    "error",          # 失败（error_message 有值）
]

# 各阶段的基准进度（前端可在阶段内自行做动画）。
STAGE_PROGRESS: Dict[str, int] = {
    "queued": 0,
    "resolving": 10,
    "downloading": 30,
    "dewatermarking": 60,
    "done": 100,
    "error": 100,
}


@dataclass
class Job:
    id: str
    source_kind: Literal["link", "upload"]
    status: Status = "queued"
    progress: int = 0
    link: Optional[str] = None          # source_kind == "link"
    input_path: Optional[str] = None    # source_kind == "upload" 时已落地的文件
    output_path: Optional[str] = None   # 去水印产物（done 时有值）
    error_message: Optional[str] = None
    timings: Optional[dict] = None
    work_dir: str = ""
    created_monotonic: float = field(default_factory=time.monotonic)

    def set_status(self, status: Status, *, progress: Optional[int] = None) -> None:
        self.status = status
        self.progress = progress if progress is not None else STAGE_PROGRESS.get(status, self.progress)

    def public_view(self) -> dict:
        """返回给前端轮询的 JSON。"""
        return {
            "status": self.status,
            "progress": self.progress,
            "error_message": self.error_message,
            "download_url": f"/api/jobs/{self.id}/download" if self.status == "done" else None,
        }


class JobStore:
    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}
        self.queue: "asyncio.Queue[str]" = asyncio.Queue()

    def create(self, source_kind: Literal["link", "upload"], *, work_base: str,
               link: Optional[str] = None, input_path: Optional[str] = None) -> Job:
        job_id = uuid.uuid4().hex
        job = Job(
            id=job_id,
            source_kind=source_kind,
            work_dir=os.path.join(work_base, job_id),
            link=link,
            input_path=input_path,
        )
        self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    async def enqueue(self, job_id: str) -> None:
        await self.queue.put(job_id)

    def reap(self, older_than_monotonic: float) -> list:
        """回收早于阈值的终态 job（done/error），从内存移除并返回其 work_dir 列表供删盘。

        只回收终态 job：queued/处理中的绝不动，避免删掉正在用的目录。
        """
        removed_dirs = []
        for job_id in list(self._jobs.keys()):
            job = self._jobs[job_id]
            if job.status in ("done", "error") and job.created_monotonic < older_than_monotonic:
                if job.work_dir:
                    removed_dirs.append(job.work_dir)
                del self._jobs[job_id]
        return removed_dirs
