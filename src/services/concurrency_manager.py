"""Concurrency manager for token-based rate limiting"""
import asyncio
from typing import Dict, Optional
from ..core.logger import debug_logger


class ConcurrencyManager:
    """Manages concurrent request limits for each token"""

    def __init__(self):
        """Initialize concurrency manager"""
        self._image_concurrency: Dict[int, int] = {}  # token_id -> remaining image concurrency
        self._video_concurrency: Dict[int, int] = {}  # token_id -> remaining video concurrency
        self._lock = asyncio.Lock()  # Protect concurrent access

    async def initialize(self, tokens: list):
        """
        Initialize concurrency counters from token list

        Args:
            tokens: List of Token objects with image_concurrency and video_concurrency fields
        """
        async with self._lock:
            for token in tokens:
                if token.image_concurrency and token.image_concurrency > 0:
                    self._image_concurrency[token.id] = token.image_concurrency
                if token.video_concurrency and token.video_concurrency > 0:
                    self._video_concurrency[token.id] = token.video_concurrency

            debug_logger.log_info(f"Concurrency manager initialized with {len(tokens)} tokens")

    async def can_use_image(self, token_id: int) -> bool:
        """
        Check if token can be used for image generation

        Args:
            token_id: Token ID

        Returns:
            True if token has available image concurrency, False if concurrency is 0
        """
        async with self._lock:
            # If not in dict, it means no limit (-1)
            if token_id not in self._image_concurrency:
                return True

            remaining = self._image_concurrency[token_id]
            if remaining <= 0:
                debug_logger.log_info(f"Token {token_id} image concurrency exhausted (remaining: {remaining})")
                return False

            return True

    async def can_use_video(self, token_id: int) -> bool:
        """
        Check if token can be used for video generation

        Args:
            token_id: Token ID

        Returns:
            True if token has available video concurrency, False if concurrency is 0
        """
        async with self._lock:
            # If not in dict, it means no limit (-1)
            if token_id not in self._video_concurrency:
                return True

            remaining = self._video_concurrency[token_id]
            if remaining <= 0:
                debug_logger.log_info(f"Token {token_id} video concurrency exhausted (remaining: {remaining})")
                return False

            return True

    async def acquire_image(self, token_id: int) -> bool:
        """
        Acquire image concurrency slot

        Args:
            token_id: Token ID

        Returns:
            True if acquired, False if not available
        """
        async with self._lock:
            if token_id not in self._image_concurrency:
                # No limit
                return True

            if self._image_concurrency[token_id] <= 0:
                return False

            self._image_concurrency[token_id] -= 1
            debug_logger.log_info(f"Token {token_id} acquired image slot (remaining: {self._image_concurrency[token_id]})")
            return True

    async def acquire_video(self, token_id: int) -> bool:
        """
        Acquire video concurrency slot

        Args:
            token_id: Token ID

        Returns:
            True if acquired, False if not available
        """
        async with self._lock:
            if token_id not in self._video_concurrency:
                # No limit
                return True

            if self._video_concurrency[token_id] <= 0:
                return False

            self._video_concurrency[token_id] -= 1
            debug_logger.log_info(f"Token {token_id} acquired video slot (remaining: {self._video_concurrency[token_id]})")
            return True

    async def release_image(self, token_id: int):
        """
        Release image concurrency slot

        Args:
            token_id: Token ID
        """
        async with self._lock:
            if token_id in self._image_concurrency:
                self._image_concurrency[token_id] += 1
                debug_logger.log_info(f"Token {token_id} released image slot (remaining: {self._image_concurrency[token_id]})")

    async def release_video(self, token_id: int):
        """
        Release video concurrency slot

        Args:
            token_id: Token ID
        """
        async with self._lock:
            if token_id in self._video_concurrency:
                self._video_concurrency[token_id] += 1
                debug_logger.log_info(f"Token {token_id} released video slot (remaining: {self._video_concurrency[token_id]})")

    async def get_image_remaining(self, token_id: int) -> Optional[int]:
        """
        Get remaining image concurrency for token

        Args:
            token_id: Token ID

        Returns:
            Remaining count or None if no limit
        """
        async with self._lock:
            return self._image_concurrency.get(token_id)

    async def get_video_remaining(self, token_id: int) -> Optional[int]:
        """
        Get remaining video concurrency for token

        Args:
            token_id: Token ID

        Returns:
            Remaining count or None if no limit
        """
        async with self._lock:
            return self._video_concurrency.get(token_id)

    async def reset_token(self, token_id: int, image_concurrency: int = -1, video_concurrency: int = -1):
        """
        Reset concurrency counters for a token

        Args:
            token_id: Token ID
            image_concurrency: New image concurrency limit (-1 for no limit)
            video_concurrency: New video concurrency limit (-1 for no limit)
        """
        async with self._lock:
            if image_concurrency > 0:
                self._image_concurrency[token_id] = image_concurrency
            elif token_id in self._image_concurrency:
                del self._image_concurrency[token_id]

            if video_concurrency > 0:
                self._video_concurrency[token_id] = video_concurrency
            elif token_id in self._video_concurrency:
                del self._video_concurrency[token_id]

            debug_logger.log_info(f"Token {token_id} concurrency reset (image: {image_concurrency}, video: {video_concurrency})")
