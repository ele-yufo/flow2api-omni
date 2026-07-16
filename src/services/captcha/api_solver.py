"""Third-party captcha API solver (yescaptcha/capmonster/ezcaptcha/capsolver).

Extracted from FlowClient (0 self; config-driven + live HTTP). createTask -> poll
getTaskResult -> gRecaptchaResponse token. Characterized via mocked AsyncSession.
"""
import asyncio
from typing import Optional

from curl_cffi.requests import AsyncSession

from ...core.config import config
from ...shared.telemetry import debug_logger


async def get_api_captcha_token(method: str, project_id: str, action: str = "IMAGE_GENERATION") -> Optional[str]:
    """通用API打码服务

    Args:
        method: 打码服务类型
        project_id: 项目ID
        action: reCAPTCHA action类型 (IMAGE_GENERATION 或 VIDEO_GENERATION)
    """
    # 获取配置
    if method == "yescaptcha":
        client_key = config.yescaptcha_api_key
        base_url = config.yescaptcha_base_url
        task_type = "RecaptchaV3TaskProxylessM1"
    elif method == "capmonster":
        client_key = config.capmonster_api_key
        base_url = config.capmonster_base_url
        task_type = "RecaptchaV3TaskProxyless"
    elif method == "ezcaptcha":
        client_key = config.ezcaptcha_api_key
        base_url = config.ezcaptcha_base_url
        task_type = "ReCaptchaV3TaskProxylessS9"
    elif method == "capsolver":
        client_key = config.capsolver_api_key
        base_url = config.capsolver_base_url
        task_type = "ReCaptchaV3EnterpriseTaskProxyLess"
    else:
        debug_logger.log_error(f"[reCAPTCHA] Unknown API method: {method}")
        return None

    if not client_key:
        debug_logger.log_info(f"[reCAPTCHA] {method} API key not configured, skipping")
        return None

    website_key = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
    website_url = f"https://labs.google/fx/tools/flow/project/{project_id}"
    page_action = action

    try:
        # Do not use curl_cffi impersonation for captcha API JSON endpoints: some ASGI
        # servers (for example FastAPI/Uvicorn) may receive an empty body and return 422.
        async with AsyncSession() as session:
            create_url = f"{base_url}/createTask"
            create_data = {
                "clientKey": client_key,
                "task": {
                    "websiteURL": website_url,
                    "websiteKey": website_key,
                    "type": task_type,
                    "pageAction": page_action
                }
            }

            result = await session.post(create_url, json=create_data)
            result_json = result.json()
            task_id = result_json.get('taskId')

            debug_logger.log_info(f"[reCAPTCHA {method}] created task_id: {task_id}")

            if not task_id:
                error_desc = result_json.get('errorDescription', 'Unknown error')
                debug_logger.log_error(f"[reCAPTCHA {method}] Failed to create task: {error_desc}")
                return None

            get_url = f"{base_url}/getTaskResult"
            for i in range(40):
                get_data = {
                    "clientKey": client_key,
                    "taskId": task_id
                }
                result = await session.post(get_url, json=get_data)
                result_json = result.json()

                debug_logger.log_info(
                    f"[reCAPTCHA {method}] polling #{i+1}: {debug_logger.format_data_for_log(result_json)}"
                )

                status = result_json.get('status')
                if status == 'ready':
                    solution = result_json.get('solution', {})
                    response = solution.get('gRecaptchaResponse')
                    if response:
                        debug_logger.log_info(f"[reCAPTCHA {method}] Token获取成功")
                        return response

                # 快速失败：识别 failed/error 状态，不要傻等 120 秒
                if status == 'failed' or result_json.get('errorId') or result_json.get('errorCode'):
                    err_desc = result_json.get('errorDescription') or result_json.get('errorCode') or 'unknown'
                    debug_logger.log_error(f"[reCAPTCHA {method}] Task failed early: {err_desc}")
                    return None
                # HTTP 状态异常时也快速退出
                if result.status_code >= 400:
                    debug_logger.log_error(
                        f"[reCAPTCHA {method}] poll HTTP {result.status_code}, abort"
                    )
                    return None

                await asyncio.sleep(3)

            debug_logger.log_error(f"[reCAPTCHA {method}] Timeout waiting for token")
            return None

    except Exception as e:
        debug_logger.log_error(f"[reCAPTCHA {method}] error: {str(e)}")
        return None
