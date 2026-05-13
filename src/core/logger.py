"""Debug logger module for detailed API request/response logging"""
import json
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional
from .config import config

class DebugLogger:
    """Debug logger for API requests and responses"""

    def __init__(self):
        self.log_file = Path("logs.txt")
        self._setup_logger()

    def _setup_logger(self):
        """Setup file logger"""
        # Create logger
        self.logger = logging.getLogger("debug_logger")
        self.logger.setLevel(logging.DEBUG)

        # Remove existing handlers
        self.logger.handlers.clear()

        # Create rotating file handler. Debug logs can include full upstream
        # request/response samples, so keep bounded files by default.
        file_handler = RotatingFileHandler(
            self.log_file,
            maxBytes=config.debug_log_max_bytes,
            backupCount=config.debug_log_backup_count,
            encoding='utf-8'
        )
        file_handler.setLevel(logging.DEBUG)

        # Create formatter
        formatter = logging.Formatter(
            '%(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(formatter)

        # Add handler
        self.logger.addHandler(file_handler)

        # Prevent propagation to root logger
        self.logger.propagate = False

    def _mask_token(self, token: str) -> str:
        """Mask token for logging (show first 6 and last 6 characters)"""
        if not config.debug_mask_token or len(token) <= 12:
            return token
        return f"{token[:6]}...{token[-6:]}"

    def _format_timestamp(self) -> str:
        """Format current timestamp"""
        return datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

    def _write_separator(self, char: str = "=", length: int = 100):
        """Write separator line"""
        self.logger.info(char * length)

    def _mask_cookie_header(self, cookie_value: str) -> str:
        """Mask auth cookies inside a Cookie/Set-Cookie header."""
        if not isinstance(cookie_value, str):
            return cookie_value
        result = cookie_value
        for marker in (
            "__Secure-next-auth.session-token=",
            "__Host-next-auth.csrf-token=",
            "__Secure-next-auth.callback-url=",
        ):
            if marker in result:
                prefix, rest = result.split(marker, 1)
                token, sep, suffix = rest.partition(";")
                result = f"{prefix}{marker}{self._mask_token(token)}{sep}{suffix}"
        return result

    def _sanitize_for_log(self, data: Any, max_length: int = 200, key_name: str = "") -> Any:
        """Mask secrets and truncate large payload fields before writing logs."""
        key_lower = (key_name or "").lower()
        sensitive_keys = {
            "authorization",
            "access_token",
            "refresh_token",
            "id_token",
            "st",
            "at",
            "cookie",
            "set-cookie",
        }
        token_keys = {
            "token",
            "recaptchatoken",
            "recaptcha_token",
            "recaptcharesponse",
            "grecaptcharesponse",
            "sessiontoken",
            "session_token",
        }
        large_keys = {
            "encodedimage",
            "encodedvideo",
            "base64",
            "imagedata",
            "imagebytes",
            "rawimagebytes",
            "data",
        }

        if isinstance(data, dict):
            result = {}
            for key, value in data.items():
                result[key] = self._sanitize_for_log(value, max_length=max_length, key_name=str(key))
            return result

        if isinstance(data, list):
            return [self._sanitize_for_log(item, max_length=max_length, key_name=key_name) for item in data]

        if isinstance(data, str):
            compact_key = key_lower.replace("-", "").replace("_", "")
            if key_lower in sensitive_keys or compact_key in token_keys:
                if key_lower in ("cookie", "set-cookie"):
                    return self._mask_cookie_header(data)
                if data.startswith("Bearer "):
                    return f"Bearer {self._mask_token(data[7:])}"
                return self._mask_token(data)

            if compact_key in large_keys and len(data) > max_length:
                return f"{data[:100]}... (truncated, total {len(data)} chars)"

            if len(data) > 10000:
                return f"{data[:100]}... (truncated, total {len(data)} chars)"

        return data

    def _truncate_large_fields(self, data: Any, max_length: int = 200) -> Any:
        """Backward-compatible wrapper for log sanitization."""
        return self._sanitize_for_log(data, max_length=max_length)

    def format_data_for_log(self, data: Any) -> str:
        """Return a sanitized string representation for ad-hoc log messages."""
        try:
            sanitized = self._sanitize_for_log(data)
            if isinstance(sanitized, (dict, list)):
                return json.dumps(sanitized, ensure_ascii=False)
            return str(sanitized)
        except Exception:
            return "<unserializable>"

    def _sanitize_text_for_log(self, text: str) -> str:
        """Best-effort masking for already formatted text messages."""
        if not isinstance(text, str):
            return str(text)
        sanitized = text
        for marker in ("Bearer ", "access_token': '", '"access_token": "', "token': '", '"token": "'):
            start = 0
            while marker in sanitized[start:]:
                idx = sanitized.find(marker, start)
                value_start = idx + len(marker)
                end_candidates = [
                    pos for pos in (
                        sanitized.find("'", value_start),
                        sanitized.find('"', value_start),
                        sanitized.find(",", value_start),
                        sanitized.find("}", value_start),
                        sanitized.find(" ", value_start),
                    )
                    if pos != -1
                ]
                value_end = min(end_candidates) if end_candidates else min(len(sanitized), value_start + 120)
                value = sanitized[value_start:value_end]
                if len(value) > 12:
                    sanitized = sanitized[:value_start] + self._mask_token(value) + sanitized[value_end:]
                    start = value_start + 12
                else:
                    start = value_end
        return sanitized

    def log_request(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        body: Optional[Any] = None,
        files: Optional[Dict] = None,
        proxy: Optional[str] = None
    ):
        """Log API request details to log.txt"""

        if not config.debug_enabled or not config.debug_log_requests:
            return

        try:
            self._write_separator()
            self.logger.info(f"🔵 [REQUEST] {self._format_timestamp()}")
            self._write_separator("-")

            # Basic info
            self.logger.info(f"Method: {method}")
            self.logger.info(f"URL: {url}")

            # Headers
            self.logger.info("\n📋 Headers:")
            masked_headers = self._sanitize_for_log(dict(headers))

            for key, value in masked_headers.items():
                self.logger.info(f"  {key}: {value}")

            # Body
            if body is not None:
                self.logger.info("\n📦 Request Body:")
                if isinstance(body, (dict, list)):
                    body_to_log = self._sanitize_for_log(body)
                    body_str = json.dumps(body_to_log, indent=2, ensure_ascii=False)
                    self.logger.info(body_str)
                else:
                    self.logger.info(self._sanitize_text_for_log(str(body)))

            # Files
            if files:
                self.logger.info("\n📎 Files:")
                try:
                    if hasattr(files, 'keys') and callable(getattr(files, 'keys', None)):
                        for key in files.keys():
                            self.logger.info(f"  {key}: <file data>")
                    else:
                        self.logger.info("  <multipart form data>")
                except (AttributeError, TypeError):
                    self.logger.info("  <binary file data>")

            # Proxy
            if proxy:
                self.logger.info(f"\n🌐 Proxy: {proxy}")

            self._write_separator()
            self.logger.info("")  # Empty line

        except Exception as e:
            self.logger.error(f"Error logging request: {e}")

    def log_response(
        self,
        status_code: int,
        headers: Dict[str, str],
        body: Any,
        duration_ms: Optional[float] = None
    ):
        """Log API response details to log.txt"""

        if not config.debug_enabled or not config.debug_log_responses:
            return

        try:
            self._write_separator()
            self.logger.info(f"🟢 [RESPONSE] {self._format_timestamp()}")
            self._write_separator("-")

            # Status
            status_emoji = "✅" if 200 <= status_code < 300 else "❌"
            self.logger.info(f"Status: {status_code} {status_emoji}")

            # Duration
            if duration_ms is not None:
                self.logger.info(f"Duration: {duration_ms:.2f}ms")

            # Headers
            self.logger.info("\n📋 Response Headers:")
            masked_headers = self._sanitize_for_log(dict(headers))
            for key, value in masked_headers.items():
                self.logger.info(f"  {key}: {value}")

            # Body
            self.logger.info("\n📦 Response Body:")
            if isinstance(body, (dict, list)):
                # 对大字段进行截断处理
                body_to_log = self._truncate_large_fields(body)
                body_str = json.dumps(body_to_log, indent=2, ensure_ascii=False)
                self.logger.info(body_str)
            elif isinstance(body, str):
                # Try to parse as JSON
                try:
                    parsed = json.loads(body)
                    # 对大字段进行截断处理
                    parsed = self._truncate_large_fields(parsed)
                    body_str = json.dumps(parsed, indent=2, ensure_ascii=False)
                    self.logger.info(body_str)
                except:
                    # Not JSON, log as text (limit length)
                    if len(body) > 2000:
                        self.logger.info(f"{body[:2000]}... (truncated)")
                    else:
                        self.logger.info(body)
            else:
                self.logger.info(str(body))

            self._write_separator()
            self.logger.info("")  # Empty line

        except Exception as e:
            self.logger.error(f"Error logging response: {e}")

    def log_error(
        self,
        error_message: str,
        status_code: Optional[int] = None,
        response_text: Optional[str] = None
    ):
        """Log API error details to log.txt"""

        if not config.debug_enabled:
            return

        try:
            self._write_separator()
            self.logger.info(f"🔴 [ERROR] {self._format_timestamp()}")
            self._write_separator("-")

            if status_code:
                self.logger.info(f"Status Code: {status_code}")

            # 任何时候都要打错误正文 —— 之前条件嵌在 status_code 分支内，
            # 导致 captcha/浏览器层这种 status_code=None 的错误体被吞掉，
            # 调试 warmup 崩溃时彻底失明。
            if error_message:
                self.logger.info(f"Error Message: {self._sanitize_text_for_log(error_message)}")

            if response_text:
                self.logger.info("\n📦 Error Response:")
                # Try to parse as JSON
                try:
                    parsed = json.loads(response_text)
                    parsed = self._sanitize_for_log(parsed)
                    body_str = json.dumps(parsed, indent=2, ensure_ascii=False)
                    self.logger.info(body_str)
                except:
                    # Not JSON, log as text
                    if len(response_text) > 2000:
                        self.logger.info(f"{self._sanitize_text_for_log(response_text[:2000])}... (truncated)")
                    else:
                        self.logger.info(self._sanitize_text_for_log(response_text))

            self._write_separator()
            self.logger.info("")  # Empty line

        except Exception as e:
            self.logger.error(f"Error logging error: {e}")

    def log_info(self, message: str):
        """Log general info message to log.txt"""
        if not config.debug_enabled:
            return
        try:
            self.logger.info(f"ℹ️  [{self._format_timestamp()}] {self._sanitize_text_for_log(message)}")
        except Exception as e:
            self.logger.error(f"Error logging info: {e}")

    def log_warning(self, message: str):
        """Log warning message to log.txt"""
        if not config.debug_enabled:
            return
        try:
            self.logger.warning(f"⚠️  [{self._format_timestamp()}] {self._sanitize_text_for_log(message)}")
        except Exception as e:
            self.logger.error(f"Error logging warning: {e}")

# Global debug logger instance
debug_logger = DebugLogger()
