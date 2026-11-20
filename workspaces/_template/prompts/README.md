# Ghi đè prompt

Prompt lõi: `bizagent/prompts/system.md.j2`. Hai cách tuỳ chỉnh cho doanh nghiệp này:

**1. Sửa một phần** (khuyên dùng): tạo `prompts/system.md.j2`

```jinja
{% extends "core/system.md.j2" %}
{% block business_rules %}
8. Doanh thu luôn báo theo đơn vị triệu đồng, làm tròn 1 chữ số thập phân.
9. Giọng văn: {{ vars.tone }}.
{% endblock %}
```

Các block có thể ghi đè: `identity`, `context`, `sources`, `rules`, `business_rules`, `knowledge`.

**2. Thay hẳn**: tạo `prompts/system.md.j2` không có `extends`.

Chế độ nhiều agent (`agents.yaml: multi_agent`) có thêm prompt riêng từng agent, ghi đè cùng cách:
`router.md.j2`, `analyst.md.j2`, `advisor.md.j2`, `designer.md.j2` (vd `{% extends "core/advisor.md.j2" %}` +
`{% block business_rules %}`). Analyst và designer kế thừa `system.md.j2` nên luật ở trên áp dụng cho cả hai.
Xem chi tiết: `docs/AGENTS.md`; in prompt đã ghép: `bizagent prompt --agent advisor`.

Biến dùng được: `workspace` (thông tin trong workspace.yaml), `vars` (prompt_vars), `knowledge`, `today`, `now`,
`weekday`, `role`, `sources`. Gõ sai tên biến sẽ báo lỗi ngay (kiểm tra bằng `bizagent check`).
