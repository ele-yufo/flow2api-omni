"""Regression: nodriver_patches must resolve NODRIVER_AVAILABLE / uc.

抽出补丁函数时曾把 _patch_nodriver_runtime 引用的模块级全局 uc/NODRIVER_AVAILABLE
落在原文件 → 浏览器启动时 NameError('NODRIVER_AVAILABLE' not defined),整条
personal 打码链路失败。此测试锁住:该模块自持这两个名字,补丁函数不因 NameError 崩。

(此 bug 由 live 冒烟测试发现——离线 mock 未覆盖真实浏览器启动路径。)
"""
from src.services.captcha import nodriver_patches


def test_module_defines_nodriver_globals():
    # 这两个名字必须在模块命名空间内可解析
    assert hasattr(nodriver_patches, "NODRIVER_AVAILABLE")
    assert hasattr(nodriver_patches, "uc")
    assert isinstance(nodriver_patches.NODRIVER_AVAILABLE, bool)


def test_patch_runtime_no_nameerror_on_none():
    # 原 bug:调用即 NameError。修复后:browser_instance=None 时安全早返回/应用,不抛 NameError。
    try:
        nodriver_patches._patch_nodriver_runtime(None)
    except NameError as e:
        raise AssertionError(f"_patch_nodriver_runtime 仍有未定义名字: {e}")
    # 其它异常(理论上不该有)也视为回归
