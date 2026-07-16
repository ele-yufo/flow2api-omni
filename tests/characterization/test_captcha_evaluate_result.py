"""Characterization: lock nodriver CDP evaluate-result normalization."""
from types import SimpleNamespace
from tests.conftest import assert_golden


def test_normalize_evaluate_result_golden():
    from src.services.captcha.evaluate_result import normalize_nodriver_evaluate_result as norm

    typed_str = SimpleNamespace(type_="string", value="hello", deep_serialized_value=None)
    typed_obj = SimpleNamespace(type_="object", value=[["a", 1], ["b", 2]], deep_serialized_value=None)
    out = {
        "none": norm(None),
        "plain_str": norm("x"),
        "plain_list": norm([1, 2, 3]),
        "typed_string": norm(typed_str),
        "typed_object_entries": norm(typed_obj),
        "dict_typed_wrapper": norm({"type": "string", "value": "wrapped"}),
        "plain_dict": norm({"k": "v", "n": 5}),
        "object_entry_list": norm([["x", "1"], ["y", "2"]]),
    }
    assert out["typed_string"] == "hello"
    assert out["typed_object_entries"] == {"a": 1, "b": 2}
    assert out["dict_typed_wrapper"] == "wrapped"
    assert out["object_entry_list"] == {"x": "1", "y": "2"}
    assert_golden("captcha_evaluate_result", out)
