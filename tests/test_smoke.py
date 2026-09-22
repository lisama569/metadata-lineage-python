"""骨架冒烟测试：确认包与应用可导入、健康检查可用。"""


def test_imports_and_health() -> None:
    from metadata_lineage.api import create_app
    from fastapi.testclient import TestClient

    client = TestClient(create_app())
    assert client.get("/health").json() == {"status": "ok"}
