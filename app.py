"""
app.py - Flask UI (本地 + 雲端兩用)
本地: 雙擊 start.bat, 開 http://127.0.0.1:5150
雲端: 部署上 Render, 有密碼登入保護

密碼由環境變數 APP_PASSWORD 讀。
- 本地: 唔設 APP_PASSWORD 就唔需要登入 (方便自己用)
- 雲端: 喺 Render 設 APP_PASSWORD, 就會要求輸入密碼
"""
import json
import os
import secrets
from functools import wraps
from pathlib import Path
from flask import Flask, render_template, request, jsonify, session, redirect

BASE_DIR = Path(__file__).parent
SETTINGS = json.loads((BASE_DIR / "settings.json").read_text(encoding="utf-8"))

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(16))

# 密碼: 由環境變數讀。冇設 = 唔需要登入 (本地自用)
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

import analyzer


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        # 冇設密碼 = 唔需要登入
        if not APP_PASSWORD:
            return f(*args, **kwargs)
        if session.get("authed"):
            return f(*args, **kwargs)
        # API 請求回 401, 頁面請求轉登入頁
        if request.path.startswith("/api/"):
            return jsonify({"error": "未登入"}), 401
        return redirect("/login")
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    if not APP_PASSWORD:
        return redirect("/")
    if request.method == "POST":
        pw = request.form.get("password", "")
        if pw == APP_PASSWORD:
            session["authed"] = True
            return redirect("/")
        return render_template("login.html", error="密碼錯誤")
    return render_template("login.html", error="")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# 快取: df + 當前來源描述
_cache = {"df": None, "source": None, "source_label": "settings.json 預設"}


def get_df(force=False, source=None):
    if source is not None:
        _cache["df"] = analyzer.enrich(analyzer.load_sheet_df(source))
        _cache["source"] = source
        return _cache["df"]
    if _cache["df"] is None or force:
        _cache["df"] = analyzer.enrich(analyzer.load_sheet_df(_cache["source"]))
    return _cache["df"]


@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/api/load_source", methods=["POST"])
@login_required
def api_load_source():
    """切換資料來源: url / csv / default。載入後回傳欄位預覽俾用戶確認。"""
    body = request.get_json(force=True)
    kind = body.get("kind")  # "url" / "csv" / "default"
    try:
        if kind == "url":
            url = (body.get("url") or "").strip()
            if not url:
                return jsonify({"error": "請貼 Google Sheet 連結"}), 400
            source = {"type": "url", "url": url}
            label = "Google 連結"
        elif kind == "csv":
            content = body.get("content") or ""
            if not content.strip():
                return jsonify({"error": "CSV 內容係空"}), 400
            source = {"type": "csv", "content": content}
            label = "上載 CSV"
        else:
            source = None
            label = "settings.json 預設"
        df = get_df(source=source)
        _cache["source_label"] = label
        # 欄位預覽: 頭 3 行, 各欄樣本
        preview = []
        for _, row in df.head(3).iterrows():
            preview.append({
                "送貨日期": row["delivery_date"], "訂單編號": row["order_no"],
                "SKU名稱": row["sku_name"], "類別(type)": row.get("type", ""),
                "出售量": row["sold_qty"], "退款量": row["refund_qty"],
            })
        return jsonify({"ok": True, "label": label, "row_count": len(df), "preview": preview})
    except Exception as e:
        return jsonify({"error": f"載入失敗: {e}"}), 500


@app.route("/api/options")
@login_required
def api_options():
    force = request.args.get("refresh") == "1"
    try:
        df = get_df(force=force)
    except Exception as e:
        return jsonify({"error": f"讀 Google Sheet 失敗: {e}"}), 500
    reasons = analyzer.get_reason_options(df)
    cancel_type_options = analyzer.get_cancel_type_options(df)
    platforms = ["全部", "Express", "HKTVmall"]
    teams = ["全部", "Express車隊", "HKTVmall車隊"]
    # 日期範圍 (用統一格式 YYYY-MM-DD, 方便 UI date input)
    dates = sorted([d for d in df["delivery_date_norm"].tolist() if d]) if not df.empty else []
    # 貨品類別清單 (Column N type), 空白/null 統一做 (未分類), 同 analyze_by_type 一致
    if not df.empty:
        types = sorted(set(
            str(v).strip() if str(v).strip() and str(v).strip().lower() != "null" else "(未分類)"
            for v in df["type"].tolist()
        ))
    else:
        types = []
    return jsonify({
        "reasons": reasons,
        "platforms": platforms,
        "teams": teams,
        "categories": ["全部"] + types,
        "cancel_type_options": cancel_type_options,
        "date_min": dates[0] if dates else "",
        "date_max": dates[-1] if dates else "",
        "row_count": len(df),
        "source_label": _cache["source_label"],
    })


@app.route("/api/analyze", methods=["POST"])
@login_required
def api_analyze():
    body = request.get_json(force=True)
    selected = body.get("reasons", [])
    platform = body.get("platform", "全部")
    team = body.get("team", "全部")
    date_from = body.get("date_from") or None
    date_to = body.get("date_to") or None
    cancel_types = body.get("cancel_types") or []

    if not selected:
        return jsonify({"error": "請至少揀一個原因"}), 400

    df = get_df()
    results = analyzer.analyze(
        df, selected, platform=platform, carline_team=team,
        date_from=date_from, date_to=date_to, cancel_types=cancel_types,
    )
    total_sold = sum(r["sold_qty"] for r in results)
    total_cancelled = sum(r["cancelled_qty"] for r in results)
    overall = round(total_cancelled / total_sold * 100, 2) if total_sold else 0

    # 車隊對比 (方法一: 各車隊獨立計) — 唔受 team 篩選影響, 內部拆
    by_carline = analyzer.analyze_by_carline(
        df, selected, platform=platform,
        date_from=date_from, date_to=date_to, cancel_types=cancel_types,
    )
    # 按貨品類別 (type) 分析
    by_type = analyzer.analyze_by_type(
        df, selected, platform=platform, carline_team=team,
        date_from=date_from, date_to=date_to, cancel_types=cancel_types,
    )
    # 按貨品類別 + 車隊拆解
    by_type_carline = analyzer.analyze_type_by_carline(
        df, selected, platform=platform,
        date_from=date_from, date_to=date_to, cancel_types=cancel_types,
    )
    return jsonify({
        "results": results,
        "by_carline": by_carline,
        "by_type": by_type,
        "by_type_carline": by_type_carline,
        "summary": {
            "sku_count": len(results),
            "total_sold": round(total_sold, 2),
            "total_cancelled": round(total_cancelled, 2),
            "overall_rate": overall,
        },
    })


@app.route("/api/details", methods=["POST"])
@login_required
def api_details():
    body = request.get_json(force=True)
    selected = body.get("reasons", [])
    platform = body.get("platform", "全部")
    team = body.get("team", "全部")
    date_from = body.get("date_from") or None
    date_to = body.get("date_to") or None
    sku_name = body.get("sku_name") or None
    cancel_types = body.get("cancel_types") or []
    if not selected:
        return jsonify({"error": "請至少揀一個原因"}), 400
    df = get_df()
    rows = analyzer.get_order_details(
        df, selected, platform=platform, carline_team=team,
        date_from=date_from, date_to=date_to, sku_name=sku_name,
        cancel_types=cancel_types,
    )
    return jsonify({"details": rows, "count": len(rows)})


@app.route("/api/merchant_alert", methods=["POST"])
@login_required
def api_merchant_alert():
    """
    商戶違規 SKU 篩選: 指定類別入面, 揪出「商戶+SKU」達到
    (出售量 >= 門檻) 且 (OOS率 >= 門檻 或 取消率 >= 門檻) 嘅組合。
    """
    body = request.get_json(force=True)
    categories = body.get("categories") or []
    oos_reasons = body.get("oos_reasons", [])
    try:
        days = int(body.get("days", 7))
        min_sold_qty = float(body.get("min_sold_qty", 100))
        oos_rate_threshold = float(body.get("oos_rate_threshold", 3))
        cancel_rate_threshold = float(body.get("cancel_rate_threshold", 1))
    except (TypeError, ValueError):
        return jsonify({"error": "門檻數值格式錯誤"}), 400
    platform = body.get("platform", "全部")
    team = body.get("team", "全部")
    date_from = body.get("date_from") or None
    date_to = body.get("date_to") or None
    cancel_types = body.get("cancel_types") or []

    df = get_df()
    out = analyzer.analyze_merchant_alert(
        df, categories, oos_reasons, days=days,
        min_sold_qty=min_sold_qty,
        oos_rate_threshold=oos_rate_threshold,
        cancel_rate_threshold=cancel_rate_threshold,
        platform=platform, carline_team=team,
        date_from=date_from, date_to=date_to,
        cancel_types=cancel_types,
    )
    return jsonify(out)


@app.route("/api/diagnose")
@login_required
def api_diagnose():
    """診斷: 輸入 SKU 名稱關鍵字, 睇底下有幾多個唔同 sku_id + 原始名寫法"""
    kw = request.args.get("kw", "").strip()
    if not kw:
        return jsonify({"error": "請加 ?kw=關鍵字"}), 400
    df = get_df()
    hit = df[df["sku_name"].str.contains(kw, na=False, regex=False)]
    combos = {}
    for _, row in hit.iterrows():
        key = (row["sku_id"], repr(row["sku_name"]))
        combos[key] = combos.get(key, 0) + 1
    return jsonify({
        "keyword": kw,
        "normalized_names": sorted(set(hit["sku_name"].tolist())),
        "id_name_combos": [
            {"sku_id": k[0], "sku_name_repr": k[1], "rows": v}
            for k, v in combos.items()
        ],
    })


if __name__ == "__main__":
    # 雲端 (Render) 會用 gunicorn, 唔行呢段。呢段淨係本地用。
    port = int(os.environ.get("PORT", SETTINGS["server"]["port"]))

    import socket
    def get_lan_ip():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    lan_ip = get_lan_ip()
    print("\n" + "=" * 46)
    print("  貨品取消率分析工具")
    print("=" * 46)
    print(f"  你自己開:      http://127.0.0.1:{port}")
    print(f"  同事內網開:    http://{lan_ip}:{port}")
    print("=" * 46)
    if APP_PASSWORD:
        print("  已設密碼保護")
    print("  按 Ctrl+C 停止\n")

    app.run(host="0.0.0.0", port=port, debug=False)
