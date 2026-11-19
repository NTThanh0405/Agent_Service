from bizagent.prompts.engine import PromptEngine


def test_demo_prompt_extends_core(demo_ws):
    (demo_ws.knowledge_dir / "secret.md").write_text("Công khai\n<!-- private -->\nBÍ MẬT", encoding="utf-8")
    text = PromptEngine(demo_ws).render(
        "system.md.j2",
        role="admin",
        sources=[{"name": "sales", "kind": "sql", "dialect": "sqlite", "description": "Bán hàng"}],
    )
    assert "Chuỗi cà phê Demo" in text                 # block identity của lõi
    assert "Quy tắc bắt buộc" in text                  # block rules của lõi
    assert "12,5 triệu" in text                         # business_rules của workspace
    assert "`sales` (sql, SQL dialect: sqlite)" in text
    assert "Công khai" in text and "BÍ MẬT" not in text  # phần private không được nạp


def test_full_override(demo_ws):
    (demo_ws.prompts_dir / "system.md.j2").write_text("Chỉ một dòng cho {{ workspace.name }}", encoding="utf-8")
    assert PromptEngine(demo_ws).render("system.md.j2", role="admin", sources=[]).strip() == "Chỉ một dòng cho demo"


def test_no_sources_message(demo_ws):
    text = PromptEngine(demo_ws).render("system.md.j2", role="staff", sources=[])
    assert "CHƯA được cấp nguồn" in text
