"""Normalize nodriver / CDP `Runtime.evaluate` results into plain Python values (pure).

Extracted from browser_captcha_personal. Handles deep-serialized values, typed value
wrappers, and object-entry lists. Mutually recursive; no I/O. Locked by
tests/characterization/test_captcha_evaluate_result.py.
"""
from typing import Any, Dict, Optional


def decode_nodriver_object_entries(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, list):
        return None

    result: Dict[str, Any] = {}
    for entry in value:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            return None
        key, entry_value = entry
        if not isinstance(key, str):
            return None
        result[key] = normalize_nodriver_evaluate_result(entry_value)
    return result


def normalize_nodriver_evaluate_result(value: Any) -> Any:
    if value is None:
        return None

    deep_serialized_value = getattr(value, "deep_serialized_value", None)
    if deep_serialized_value is not None:
        return normalize_nodriver_evaluate_result(deep_serialized_value)

    type_name = getattr(value, "type_", None)
    if type_name is not None and hasattr(value, "value"):
        raw_value = getattr(value, "value", None)
        if type_name == "object":
            object_entries = decode_nodriver_object_entries(raw_value)
            if object_entries is not None:
                return object_entries
        if raw_value is not None:
            return normalize_nodriver_evaluate_result(raw_value)
        unserializable_value = getattr(value, "unserializable_value", None)
        if unserializable_value is not None:
            return str(unserializable_value)
        return value

    if isinstance(value, dict):
        typed_value_keys = {"type", "value", "objectId", "weakLocalObjectReference"}
        if "type" in value and set(value.keys()).issubset(typed_value_keys):
            raw_value = value.get("value")
            if value.get("type") == "object":
                object_entries = decode_nodriver_object_entries(raw_value)
                if object_entries is not None:
                    return object_entries
            return normalize_nodriver_evaluate_result(raw_value)
        return {
            key: normalize_nodriver_evaluate_result(item)
            for key, item in value.items()
        }

    if isinstance(value, list):
        object_entries = decode_nodriver_object_entries(value)
        if object_entries is not None:
            return object_entries
        return [normalize_nodriver_evaluate_result(item) for item in value]

    return value
