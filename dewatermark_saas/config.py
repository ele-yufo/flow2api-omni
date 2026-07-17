"""服务自身配置（去水印 SaaS Demo）。

从环境变量读取，全部有合理默认。凭证（Basic Auth）绝不写死、不进 git —— 只从环境变量取，
部署时由 systemd 单元注入。
"""
import os
from dataclasses import dataclass

# 工作目录必须位于 dewatermark 服务的 WM_ALLOWED_DIR（/opt/Projects/flow2api/tmp）之下，
# 否则 ProPainter 服务的 _path_ok 会拒（403）。
_DEFAULT_WORK_DIR = "/opt/Projects/flow2api/tmp/dewm_saas"

# Chrome130 UA：与 flow_client 对齐，取 og-video 时更稳。
_DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    work_dir: str
    max_input_bytes: int
    download_timeout: float
    http_ua: str
    http_proxy: str          # 取 og-video 用的代理；空=直连（实测直连可用）
    basic_user: str
    basic_pass: str
    job_ttl_seconds: float          # 终态 job 存活多久后连同工作目录一起回收
    reap_interval_seconds: float    # reaper 扫描间隔

    @property
    def auth_enabled(self) -> bool:
        return bool(self.basic_user and self.basic_pass)


def load_settings() -> Settings:
    return Settings(
        host=os.environ.get("DEWM_HOST", "127.0.0.1"),
        port=int(os.environ.get("DEWM_PORT", "18300")),
        work_dir=os.environ.get("DEWM_WORK_DIR", _DEFAULT_WORK_DIR),
        max_input_bytes=int(os.environ.get("DEWM_MAX_INPUT_BYTES", str(200 * 1024 * 1024))),
        download_timeout=float(os.environ.get("DEWM_DOWNLOAD_TIMEOUT", "120")),
        http_ua=os.environ.get("DEWM_HTTP_UA", _DEFAULT_UA),
        http_proxy=os.environ.get("DEWM_HTTP_PROXY", ""),
        basic_user=os.environ.get("DEWM_BASIC_USER", ""),
        basic_pass=os.environ.get("DEWM_BASIC_PASS", ""),
        job_ttl_seconds=float(os.environ.get("DEWM_JOB_TTL_SECONDS", "3600")),
        reap_interval_seconds=float(os.environ.get("DEWM_REAP_INTERVAL_SECONDS", "600")),
    )


settings = load_settings()
