"""服务器版版本号与包导入自检。"""
import push_server


def test_version_is_0_6_0():
    assert push_server.__version__ == "0.6.0"
