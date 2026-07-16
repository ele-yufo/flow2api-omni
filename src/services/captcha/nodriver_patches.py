"""nodriver runtime monkey-patches (send-task finalize, connection/browser/runtime).

Extracted from browser_captcha_personal. Patches nodriver internals to tolerate runtime
disconnects; fragile by nature. Moved verbatim. browser_captcha re-imports the patch fns.
"""
import asyncio
import types

from ...shared.telemetry import debug_logger
from .errors import _is_runtime_disconnect_error, _is_runtime_normal_close_error

# nodriver 可用性探测 —— _patch_nodriver_runtime 需要 uc/NODRIVER_AVAILABLE。
# 这两个原为 browser_captcha_personal 的模块级全局;抽出补丁函数时必须让本模块自持,
# 不能反向 import browser_captcha_personal(会循环)。语义等价:浏览器真正启动时
# nodriver 必然可用,补丁照常应用。
try:
    import nodriver as uc
    NODRIVER_AVAILABLE = True
except Exception:
    uc = None
    NODRIVER_AVAILABLE = False


_NODRIVER_RUNTIME_PATCHED = False


def _finalize_nodriver_send_task(connection, transaction, tx_id: int, task: asyncio.Task):
    """回收 nodriver websocket.send 的后台异常，避免事件循环打印未检索 task 错误。"""
    try:
        task.result()
    except asyncio.CancelledError:
        connection.mapper.pop(tx_id, None)
        if not transaction.done():
            transaction.cancel()
    except Exception as e:
        connection.mapper.pop(tx_id, None)
        if not transaction.done():
            try:
                transaction.set_exception(e)
            except Exception:
                pass

        if _is_runtime_normal_close_error(e):
            debug_logger.log_info(
                f"[BrowserCaptcha] nodriver websocket 在正常关闭后退出: {type(e).__name__}: {e}"
            )
        elif _is_runtime_disconnect_error(e):
            debug_logger.log_warning(
                f"[BrowserCaptcha] nodriver websocket 发送在断连后退出: {type(e).__name__}: {e}"
            )
        else:
            debug_logger.log_warning(
                f"[BrowserCaptcha] nodriver websocket 发送异常: {type(e).__name__}: {e}"
            )


def _patch_nodriver_connection_instance(connection_instance):
    """在连接实例级别收口 websocket.send 的后台异常。"""
    if not connection_instance or getattr(connection_instance, "_flow2api_send_patched", False):
        return

    try:
        from nodriver.core import connection as nodriver_connection_module
    except Exception as e:
        debug_logger.log_warning(f"[BrowserCaptcha] 加载 nodriver.connection 失败，跳过连接补丁: {e}")
        return

    async def patched_send(self, cdp_obj, _is_update=False):
        if self.closed:
            await self.connect()
        if not _is_update:
            await self._register_handlers()

        transaction = nodriver_connection_module.Transaction(cdp_obj)
        tx_id = next(self.__count__)
        transaction.id = tx_id
        self.mapper[tx_id] = transaction

        send_task = asyncio.create_task(self.websocket.send(transaction.message))
        send_task.add_done_callback(
            lambda task, connection=self, tx=transaction, current_tx_id=tx_id:
            _finalize_nodriver_send_task(connection, tx, current_tx_id, task)
        )
        return await transaction

    connection_instance.send = types.MethodType(patched_send, connection_instance)
    connection_instance._flow2api_send_patched = True


def _patch_nodriver_browser_instance(browser_instance):
    """在浏览器实例级别收口 update_targets，并补齐新 target 的连接补丁。"""
    if not browser_instance:
        return

    _patch_nodriver_connection_instance(getattr(browser_instance, "connection", None))
    for target in list(getattr(browser_instance, "targets", []) or []):
        _patch_nodriver_connection_instance(target)

    if getattr(browser_instance, "_flow2api_update_targets_patched", False):
        return

    original_update_targets = browser_instance.update_targets

    async def patched_update_targets(self, *args, **kwargs):
        try:
            result = await original_update_targets(*args, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as e:
                if _is_runtime_disconnect_error(e):
                    log_message = (
                        f"[BrowserCaptcha] nodriver.update_targets 在浏览器断连后退出: "
                        f"{type(e).__name__}: {e}"
                    )
                    if _is_runtime_normal_close_error(e):
                        debug_logger.log_info(log_message)
                    else:
                        debug_logger.log_warning(log_message)
                    return []
                raise

        _patch_nodriver_connection_instance(getattr(self, "connection", None))
        for target in list(getattr(self, "targets", []) or []):
            _patch_nodriver_connection_instance(target)
        return result

    browser_instance.update_targets = types.MethodType(patched_update_targets, browser_instance)
    browser_instance._flow2api_update_targets_patched = True


def _patch_nodriver_runtime(browser_instance=None):
    """给 nodriver 当前浏览器实例补一层断连降噪与异常透传。"""
    global _NODRIVER_RUNTIME_PATCHED

    if not NODRIVER_AVAILABLE or uc is None:
        return

    if browser_instance is not None:
        _patch_nodriver_browser_instance(browser_instance)

    if not _NODRIVER_RUNTIME_PATCHED:
        _NODRIVER_RUNTIME_PATCHED = True
        debug_logger.log_info("[BrowserCaptcha] 已启用 nodriver 运行态安全补丁")
