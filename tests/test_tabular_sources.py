import httpx
import pytest

from bizagent.security.guard import UnsafeSQLError
from bizagent.sources.base import SourceError
from bizagent.sources.gsheets import GoogleSheetsSource
from bizagent.sources.tabular import FilesSource
from bizagent.tabular import slugify, unique_names


def test_slugify():
    assert slugify("Doanh thu (VNĐ)") == "doanh_thu_vnd"
    assert slugify("2026") == "col_2026"
    assert unique_names(["Tên", "tên", ""]) == ["ten", "ten_2", "col_3"]


def test_files_csv_and_excel(tmp_path):
    (tmp_path / "Kế hoạch.csv").write_text(
        "Tháng,Cửa hàng,Mục tiêu (VNĐ),SĐT\n2026-11,A,100,0901\n2026-11,B,50.5,\n", encoding="utf-8"
    )
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    wb.active.title = "Chi phí"
    wb.active.append(["Loại", "Số tiền"])
    wb.active.append(["Điện", 1200])
    wb.save(tmp_path / "tai_chinh.xlsx")

    src = FilesSource("f", {"path": str(tmp_path), "mask_columns": ["sdt"]})
    assert src.list_tables() == ["ke_hoach", "tai_chinh__chi_phi"]
    cols = {c["name"]: c for c in src.describe_table("ke_hoach").columns}
    assert cols["muc_tieu_vnd"]["header"] == "Mục tiêu (VNĐ)" and cols["muc_tieu_vnd"]["type"] == "DOUBLE"
    assert cols["sdt"]["masked"]
    r = src.query("SELECT SUM(muc_tieu_vnd) AS t FROM ke_hoach")
    assert r.rows == [(150.5,)]
    assert src.query("SELECT sdt FROM ke_hoach").rows == [("***",), (None,)]
    assert src.query("SELECT so_tien FROM tai_chinh__chi_phi").rows == [(1200,)]


def test_files_query_is_sandboxed(tmp_path):
    (tmp_path / "a.csv").write_text("x\n1\n", encoding="utf-8")
    src = FilesSource("f", {"path": str(tmp_path)})
    for bad in ("SELECT * FROM read_csv('/etc/passwd')", "SELECT * FROM 'a.csv'", "SELECT * FROM b",
                "COPY a TO 'x.csv'"):
        with pytest.raises((UnsafeSQLError, SourceError)):
            src.query(bad)


def test_files_missing_dir(tmp_path):
    with pytest.raises(SourceError):
        FilesSource("f", {"path": str(tmp_path / "nope")})


def test_google_sheets_api_key(monkeypatch):
    monkeypatch.setenv("GS_KEY", "k1")
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        if req.url.path.endswith("/values/Mục tiêu!A1:C"):
            return httpx.Response(200, json={"values": [["Tháng", "Doanh thu"], ["2026-11", 100], ["2026-12"]]})
        if req.url.path.endswith("/sid"):
            return httpx.Response(200, json={"sheets": [{"properties": {"title": "Mục tiêu"}}]})
        return httpx.Response(404)

    src = GoogleSheetsSource(
        "gs",
        {"spreadsheet_id": "sid", "auth": {"type": "api_key", "key_env": "GS_KEY"},
         "tables": {"muc_tieu": {"range": "Mục tiêu!A1:C"}}},
        transport=httpx.MockTransport(handler),
    )
    assert src.list_tables() == ["muc_tieu"]
    r = src.query("SELECT thang, doanh_thu FROM muc_tieu ORDER BY thang")
    assert r.rows == [("2026-11", 100), ("2026-12", None)]
    assert seen[0].url.params["key"] == "k1"


def test_google_sheets_permission_error(monkeypatch):
    monkeypatch.setenv("GS_KEY", "k1")
    src = GoogleSheetsSource(
        "gs", {"spreadsheet_id": "sid", "auth": {"type": "api_key", "key_env": "GS_KEY"}},
        transport=httpx.MockTransport(lambda req: httpx.Response(403)),
    )
    with pytest.raises(SourceError, match="share"):
        src.list_tables()
