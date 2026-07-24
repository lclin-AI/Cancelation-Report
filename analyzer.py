"""
analyzer.py - 讀 Google Sheet (公開 CSV 連結), 計每個 SKU 嘅 Cancellation Rate
計法 (已同 Ponte 確認):
  分母 = 該 SKU 所有行嘅 quantity 總和
  分子 = 符合「揀咗嘅原因」嘅行, 佢哋 refund_quantity 嘅總和
  Rate = 分子 / 分母
原因來源:
  cancel_type == REFUND_REPORT_CANCEL -> 原因睇 report_reason (L)
  cancel_type == CS_CANCEL            -> 原因睇 cs_reason (K)

用方法 B: Sheet 設成「知道連結即可檢視」, 用 CSV 匯出連結直接讀,
唔使 service account / 金鑰。
"""
import json
from pathlib import Path
import pandas as pd

BASE_DIR = Path(__file__).parent
SETTINGS = json.loads((BASE_DIR / "settings.json").read_text(encoding="utf-8"))

COL = SETTINGS["columns"]
LOGIC = SETTINGS["logic"]


# Column 字母 -> 0-based index
def col_idx(letter):
    idx = 0
    for ch in letter.upper():
        idx = idx * 26 + (ord(ch) - ord('A') + 1)
    return idx - 1


def csv_export_url():
    """
    由 spreadsheet_id + gid 砌出公開 CSV 匯出連結。
    格式: https://docs.google.com/spreadsheets/d/<ID>/export?format=csv&gid=<GID>
    """
    sid = SETTINGS["google_sheets"]["spreadsheet_id"]
    gid = SETTINGS["google_sheets"].get("gid", "0")
    return f"https://docs.google.com/spreadsheets/d/{sid}/export?format=csv&gid={gid}"


def extract_gid(url):
    """由 Google Sheet 連結抽 gid, 抽唔到就 0"""
    import re
    m = re.search(r"[#&]gid=(\d+)", url)
    return m.group(1) if m else "0"


def extract_sheet_id(url):
    """由 Google Sheet 連結抽 spreadsheet id"""
    import re
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    return m.group(1) if m else None


def _map_columns(raw):
    """raw DataFrame (無標題, 純位置) -> 對應欄位 DataFrame。連結/CSV/預設共用。"""
    header_row = SETTINGS["server"]["header_row"]
    raw = raw.iloc[header_row:].reset_index(drop=True)
    max_col = max(col_idx(v) for v in COL.values()) + 1
    for c in range(raw.shape[1], max_col):
        raw[c] = ""
    df = pd.DataFrame()
    for key, letter in COL.items():
        df[key] = raw[col_idx(letter)]
    return df


def load_sheet_df(source=None):
    """
    讀資料, 回傳對應欄位 DataFrame。
    source:
      None                          -> 用 settings.json 預設 Sheet
      {"type":"url","url":...}       -> 由 Google Sheet 連結讀
      {"type":"csv","content":...}   -> 由上載 CSV 內容 (字串) 讀
    """
    if source and source.get("type") == "csv":
        from io import StringIO
        raw = pd.read_csv(StringIO(source["content"]), header=None, dtype=str, keep_default_na=False)
    elif source and source.get("type") == "url":
        sid = extract_sheet_id(source["url"])
        gid = extract_gid(source["url"])
        if not sid:
            raise ValueError("連結格式唔啱, 搵唔到 spreadsheet id")
        csv_url = f"https://docs.google.com/spreadsheets/d/{sid}/export?format=csv&gid={gid}"
        raw = pd.read_csv(csv_url, header=None, dtype=str, keep_default_na=False)
    else:
        raw = pd.read_csv(csv_export_url(), header=None, dtype=str, keep_default_na=False)
    return _map_columns(raw)


def to_num(series):
    return pd.to_numeric(series, errors="coerce").fillna(0)


def norm_date(val):
    """
    將各種日期格式統一成 YYYY-MM-DD 字串, 方便比較。
    支援:
      2026年7月12日   (中文, 你 Sheet 用嘅格式)
      2026/7/12  2026-7-12  12/07/2026 (UI input 用嘅 YYYY-MM-DD)
    解析唔到就回傳原字串。
    """
    import re
    if val is None:
        return ""
    s = str(val).strip()
    if not s:
        return ""
    # 中文: 2026年7月12日
    m = re.match(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", s)
    if m:
        y, mo, d = m.groups()
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    # YYYY-MM-DD 或 YYYY/M/D
    m = re.match(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        y, mo, d = m.groups()
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    return s


def enrich(df):
    """加衍生欄位: 平台(Express/Mall)、車隊、有效原因欄"""
    if df.empty:
        return df
    df = df.copy()
    # 平台: 訂單號 M 開頭 = Express
    df["platform"] = df["order_no"].astype(str).str.startswith(
        LOGIC["express_order_prefix"]
    ).map({True: "Express", False: "HKTVmall"})

    # 車隊: Carline "-" 之後首兩字 WM = Express 車隊
    def carline_team(v):
        v = str(v)
        if "-" in v:
            after = v.split("-", 1)[1].strip()
            if after[:2].upper() == LOGIC["express_carline_prefix"]:
                return "Express車隊"
        return "HKTVmall車隊"
    df["carline_team"] = df["carline"].apply(carline_team)

    # 統一日期格式 (中文日期 -> YYYY-MM-DD) 方便篩選同排序
    df["delivery_date_norm"] = df["delivery_date"].apply(norm_date)

    # SKU 名稱正規化: 去前後空格 + 全形空格轉半形 + 連續空格併成一個
    # + 清除零寬字元/控制字元, 令「睇落一樣但有隱藏差異」嘅同款貨可以正確合併
    import re as _re
    def norm_name(v):
        s = str(v)
        # 全形空格、不斷行空格 -> 半形空格
        s = s.replace("\u3000", " ").replace("\xa0", " ")
        # 清除零寬字元 (zero-width space/joiner/BOM 等)
        s = _re.sub(r"[\u200b\u200c\u200d\ufeff]", "", s)
        # 所有空白字元 (tab/換行/多個空格) 併成一個空格
        s = _re.sub(r"\s+", " ", s).strip()
        return s
    df["sku_name"] = df["sku_name"].apply(norm_name)

    # 有效原因: 按 cancel_type 決定睇 K 定 L
    def effective_reason(row):
        ct = str(row["cancel_type"]).strip()
        if ct == LOGIC["report_cancel_value"]:
            return ("客人自報", str(row["report_reason"]).strip())
        elif ct == LOGIC["cs_cancel_value"]:
            return ("客服處理", str(row["cs_reason"]).strip())
        return ("", "")
    reasons = df.apply(effective_reason, axis=1)
    df["reason_source"] = [r[0] for r in reasons]
    df["reason"] = [r[1] for r in reasons]
    return df


def get_reason_options(df):
    """抓返所有出現過嘅原因, 分「客人自報」同「客服處理」兩組"""
    if df.empty:
        return {"客人自報": [], "客服處理": []}
    out = {"客人自報": set(), "客服處理": set()}
    for _, row in df.iterrows():
        src, rsn = row["reason_source"], row["reason"]
        if src and rsn:
            out[src].add(rsn)
    return {k: sorted(v) for k, v in out.items()}


def get_cancel_type_options(df):
    """
    讀 Column J (sku_cancel_reason, settings.json 叫 cancel_type) 入面
    實際出現過嘅分類值, 做「取消分類」篩選嘅 checkbox 選項。
    唔寫死得兩個: 已知嘅 REFUND_REPORT_CANCEL / CS_CANCEL 會顯示做
    「客人自報」/「客服處理」中文名, 第日 Sheet 加多個新分類都會自動出現
    (用返個原始字串做顯示名)。
    回傳: [{"value": 原始字串, "label": 顯示名}, ...]
    """
    if df.empty:
        return []
    label_map = {
        LOGIC["report_cancel_value"]: "客人自報",
        LOGIC["cs_cancel_value"]: "客服處理",
    }
    vals = set()
    for v in df["cancel_type"].tolist():
        s = str(v).strip()
        if s and s.lower() != "null":
            vals.add(s)
    return sorted(
        [{"value": v, "label": label_map.get(v, v)} for v in vals],
        key=lambda x: x["label"],
    )


def _reason_mask(d, selected_reasons, cancel_types=None):
    """
    符合已勾選原因 AND (冇指定取消分類 或 Column J 原始值喺已揀嘅分類入面)。
    cancel_types: None / 空 list = 唔篩; 否則係一個 list, 入面裝住已揀嘅
    Column J 原始值 (例如 ["REFUND_REPORT_CANCEL"])。
    唔影響分母 (sold_qty), 淨係進一步縮窄邊啲取消計落分子。
    """
    mask = d["reason"].isin(selected_reasons)
    if cancel_types:
        mask = mask & d["cancel_type"].astype(str).str.strip().isin(cancel_types)
    return mask


def analyze(df, selected_reasons, platform=None, carline_team=None,
            date_from=None, date_to=None, cancel_types=None):
    """
    selected_reasons: list of 原因字串 (跨 K/L)
    回傳每個 SKU 嘅 sold / cancelled / rate
    """
    if df.empty:
        return []
    d = df.copy()
    d["sold_qty_n"] = to_num(d["sold_qty"])
    d["refund_qty_n"] = to_num(d["refund_qty"])

    # 篩選: 平台 / 車隊 / 日期 (影響分母同分子嘅範圍)
    if platform and platform != "全部":
        d = d[d["platform"] == platform]
    if carline_team and carline_team != "全部":
        d = d[d["carline_team"] == carline_team]
    if date_from:
        d = d[d["delivery_date_norm"] >= norm_date(date_from)]
    if date_to:
        d = d[d["delivery_date_norm"] <= norm_date(date_to)]

    if d.empty:
        return []

    # 按 SKU 名稱合併 (同款貨兩個平台有唔同 SKU ID 但同名)
    # 分母 = 同名所有行嘅 sold 總和; 分子 = 同名符合原因行嘅 refund 總和
    denom = d.groupby("sku_name")["sold_qty_n"].sum()
    mask = _reason_mask(d, selected_reasons, cancel_types)
    numer = d[mask].groupby("sku_name")["refund_qty_n"].sum()
    # 每個 sku_name 底下所有 sku_id (去重, 保持出現次序)
    id_map = {}
    for name, grp in d.groupby("sku_name"):
        ids = list(dict.fromkeys(grp["sku_id"].tolist()))
        id_map[name] = ", ".join(str(i) for i in ids if str(i).strip())

    results = []
    for sku_name, sold in denom.items():
        cancelled = numer.get(sku_name, 0)
        rate = (cancelled / sold * 100) if sold > 0 else 0
        results.append({
            "sku_id": id_map.get(sku_name, ""),
            "sku_name": sku_name,
            "sold_qty": round(float(sold), 2),
            "cancelled_qty": round(float(cancelled), 2),
            "cancellation_rate": round(float(rate), 2),
        })
    # 高 rate 排前
    results.sort(key=lambda x: x["cancellation_rate"], reverse=True)
    return results


def analyze_by_carline(df, selected_reasons, platform=None,
                       date_from=None, date_to=None, cancel_types=None):
    """
    方法一: 每個車隊各自獨立計 (分母 = 該車隊送嘅出售量)。
    回傳每隻 SKU 兩個車隊各自嘅 sold / cancelled / rate。
    注意: 車隊維度喺內部拆, 所以呢度唔篩 carline_team。
    """
    if df.empty:
        return []
    d = df.copy()
    d["sold_qty_n"] = to_num(d["sold_qty"])
    d["refund_qty_n"] = to_num(d["refund_qty"])

    if platform and platform != "全部":
        d = d[d["platform"] == platform]
    if date_from:
        d = d[d["delivery_date_norm"] >= norm_date(date_from)]
    if date_to:
        d = d[d["delivery_date_norm"] <= norm_date(date_to)]
    if d.empty:
        return []

    teams = ["Express車隊", "HKTVmall車隊"]
    mask = _reason_mask(d, selected_reasons, cancel_types)

    # 每個 sku_name 底下所有 sku_id
    id_map = {}
    for name, grp in d.groupby("sku_name"):
        ids = list(dict.fromkeys(grp["sku_id"].tolist()))
        id_map[name] = ", ".join(str(i) for i in ids if str(i).strip())

    # 按 sku_name 合併, 每個車隊各自計分母同分子
    out = {}  # key: sku_name -> dict
    for team in teams:
        sub = d[d["carline_team"] == team]
        if sub.empty:
            continue
        denom = sub.groupby("sku_name")["sold_qty_n"].sum()
        numer = sub[mask.loc[sub.index]].groupby("sku_name")["refund_qty_n"].sum()
        key_prefix = "express" if team == "Express車隊" else "mall"
        for sku_name, sold in denom.items():
            cancelled = numer.get(sku_name, 0)
            rate = (cancelled / sold * 100) if sold > 0 else 0
            if sku_name not in out:
                out[sku_name] = {"sku_id": id_map.get(sku_name, ""), "sku_name": sku_name,
                          "express_sold": 0, "express_cancelled": 0, "express_rate": 0,
                          "mall_sold": 0, "mall_cancelled": 0, "mall_rate": 0}
            out[sku_name][f"{key_prefix}_sold"] = round(float(sold), 2)
            out[sku_name][f"{key_prefix}_cancelled"] = round(float(cancelled), 2)
            out[sku_name][f"{key_prefix}_rate"] = round(float(rate), 2)

    return list(out.values())


def analyze_by_type(df, selected_reasons, platform=None, carline_team=None,
                    date_from=None, date_to=None, cancel_types=None):
    """
    按貨品類別 (Column N type) 計取消率。
    分母 = 該 type 所有行嘅 sold 總和; 分子 = 該 type 符合原因行嘅 refund 總和。
    """
    if df.empty:
        return []
    d = df.copy()
    d["sold_qty_n"] = to_num(d["sold_qty"])
    d["refund_qty_n"] = to_num(d["refund_qty"])

    if platform and platform != "全部":
        d = d[d["platform"] == platform]
    if carline_team and carline_team != "全部":
        d = d[d["carline_team"] == carline_team]
    if date_from:
        d = d[d["delivery_date_norm"] >= norm_date(date_from)]
    if date_to:
        d = d[d["delivery_date_norm"] <= norm_date(date_to)]
    if d.empty:
        return []

    # 空白/null type 統一顯示
    d["type"] = d["type"].apply(lambda v: str(v).strip() if str(v).strip() and str(v).strip().lower() != "null" else "(未分類)")

    denom = d.groupby("type")["sold_qty_n"].sum()
    mask = _reason_mask(d, selected_reasons, cancel_types)
    numer = d[mask].groupby("type")["refund_qty_n"].sum()

    results = []
    for t, sold in denom.items():
        cancelled = numer.get(t, 0)
        rate = (cancelled / sold * 100) if sold > 0 else 0
        results.append({
            "type": t,
            "sold_qty": round(float(sold), 2),
            "cancelled_qty": round(float(cancelled), 2),
            "cancellation_rate": round(float(rate), 2),
        })
    results.sort(key=lambda x: x["cancellation_rate"], reverse=True)
    return results


def analyze_type_by_carline(df, selected_reasons, platform=None,
                            date_from=None, date_to=None, cancel_types=None):
    """
    按貨品類別 (type) + 車隊拆解, 各車隊獨立計取消率。
    """
    if df.empty:
        return []
    d = df.copy()
    d["sold_qty_n"] = to_num(d["sold_qty"])
    d["refund_qty_n"] = to_num(d["refund_qty"])

    if platform and platform != "全部":
        d = d[d["platform"] == platform]
    if date_from:
        d = d[d["delivery_date_norm"] >= norm_date(date_from)]
    if date_to:
        d = d[d["delivery_date_norm"] <= norm_date(date_to)]
    if d.empty:
        return []

    d["type"] = d["type"].apply(lambda v: str(v).strip() if str(v).strip() and str(v).strip().lower() != "null" else "(未分類)")
    teams = ["Express車隊", "HKTVmall車隊"]
    mask = _reason_mask(d, selected_reasons, cancel_types)

    out = {}
    for team in teams:
        sub = d[d["carline_team"] == team]
        if sub.empty:
            continue
        denom = sub.groupby("type")["sold_qty_n"].sum()
        numer = sub[mask.loc[sub.index]].groupby("type")["refund_qty_n"].sum()
        key_prefix = "express" if team == "Express車隊" else "mall"
        for t, sold in denom.items():
            cancelled = numer.get(t, 0)
            rate = (cancelled / sold * 100) if sold > 0 else 0
            if t not in out:
                out[t] = {"type": t,
                          "express_sold": 0, "express_cancelled": 0, "express_rate": 0,
                          "mall_sold": 0, "mall_cancelled": 0, "mall_rate": 0}
            out[t][f"{key_prefix}_sold"] = round(float(sold), 2)
            out[t][f"{key_prefix}_cancelled"] = round(float(cancelled), 2)
            out[t][f"{key_prefix}_rate"] = round(float(rate), 2)
    return list(out.values())


def analyze_merchant_alert(df, categories, oos_reasons, days=7, min_sold_qty=100,
                            oos_rate_threshold=3.0, cancel_rate_threshold=1.0,
                            platform=None, carline_team=None,
                            date_from=None, date_to=None, cancel_types=None):
    """
    指定貨品類別 (type, 可揀多個) 入面, 揪出「商戶 + SKU」組合達到以下條件:
      條件一 (必須): 指定日數內 Total Sold Qty >= min_sold_qty
      條件二 (符合其一即可): OOS rate >= oos_rate_threshold%
                        或 Cancellation Rate (全部原因) >= cancel_rate_threshold%

    categories: list of 類別字串 (type), 空 list/None = 唔篩 (全部類別)。
    日期範圍: 若冇手動指定 date_from/date_to, 用資料 (經類別/平台/車隊篩選後) 入面
              最新一個送貨日期, 往前推 days 日 (連首尾共 days 日)。
    OOS rate 分子: reason 屬於 oos_reasons (用戶自揀) 嘅 refund_qty 總和
    Cancellation Rate 分子: 唔篩 reason, 呢個 商戶+SKU 組合全部退款量總和 (即整體取消率, 唔受原因揀選影響)
    cancel_types: 進一步淨計指定 Column J 分類 (原始值 list, 例如
                  ["REFUND_REPORT_CANCEL"]) 嘅退款, None/空 list = 唔篩。
                  唔影響分母 (sold_qty)。
    兩者分母一樣: 呢個 商戶+SKU 組合喺呢段日子嘅 sold_qty 總和
    """
    empty = {"results": [], "date_from": "", "date_to": ""}
    if df.empty:
        return empty
    d = df.copy()
    d["sold_qty_n"] = to_num(d["sold_qty"])
    d["refund_qty_n"] = to_num(d["refund_qty"])

    if platform and platform != "全部":
        d = d[d["platform"] == platform]
    if carline_team and carline_team != "全部":
        d = d[d["carline_team"] == carline_team]

    # 類別篩選 (空白/null 統一顯示做 (未分類), 同 analyze_by_type 一致)
    d["type"] = d["type"].apply(
        lambda v: str(v).strip() if str(v).strip() and str(v).strip().lower() != "null" else "(未分類)"
    )
    if categories:
        d = d[d["type"].isin(categories)]
    if d.empty:
        return empty

    # 日期範圍: 手動指定就用手動, 否則用最新日期往前推 N 日
    if date_from or date_to:
        if date_from:
            d = d[d["delivery_date_norm"] >= norm_date(date_from)]
        if date_to:
            d = d[d["delivery_date_norm"] <= norm_date(date_to)]
        eff_from, eff_to = (date_from or ""), (date_to or "")
    else:
        valid_dates = sorted([x for x in d["delivery_date_norm"].tolist() if x])
        if not valid_dates:
            return empty
        eff_to = valid_dates[-1]
        from datetime import datetime, timedelta
        min_dt = datetime.strptime(eff_to, "%Y-%m-%d") - timedelta(days=max(int(days), 1) - 1)
        eff_from = min_dt.strftime("%Y-%m-%d")
        d = d[(d["delivery_date_norm"] >= eff_from) & (d["delivery_date_norm"] <= eff_to)]

    if d.empty:
        return {"results": [], "date_from": eff_from, "date_to": eff_to}

    # 取消分類篩選 (Column J 原始值): 淨限制退款計算, 唔影響 sold_qty 分母
    if cancel_types:
        d_refund = d[d["cancel_type"].astype(str).str.strip().isin(cancel_types)]
    else:
        d_refund = d

    oos_set = set(oos_reasons or [])
    oos_mask = d_refund["reason"].isin(oos_set) if oos_set else pd.Series(False, index=d_refund.index)

    group_cols = ["merchant", "sku_name"]
    denom = d.groupby(group_cols)["sold_qty_n"].sum()
    total_refund = d_refund.groupby(group_cols)["refund_qty_n"].sum()
    oos_refund = d_refund[oos_mask].groupby(group_cols)["refund_qty_n"].sum()

    # 每個 商戶+SKU 底下所有 sku_id (去重)
    id_map = {}
    for key, grp in d.groupby(group_cols):
        ids = list(dict.fromkeys(grp["sku_id"].tolist()))
        id_map[key] = ", ".join(str(i) for i in ids if str(i).strip())

    results = []
    for key, sold in denom.items():
        if sold <= 0:
            continue
        merchant, sku_name = key
        t_refund = float(total_refund.get(key, 0))
        o_refund = float(oos_refund.get(key, 0))
        oos_rate = o_refund / sold * 100
        cancel_rate = t_refund / sold * 100

        cond_sold = sold >= min_sold_qty
        cond_oos = oos_rate >= oos_rate_threshold
        cond_cancel = cancel_rate >= cancel_rate_threshold
        if not (cond_sold and (cond_oos or cond_cancel)):
            continue

        matched = []
        if cond_oos:
            matched.append("OOS")
        if cond_cancel:
            matched.append("取消率")

        results.append({
            "merchant": merchant,
            "sku_id": id_map.get(key, ""),
            "sku_name": sku_name,
            "sold_qty": round(float(sold), 2),
            "oos_refund_qty": round(o_refund, 2),
            "oos_rate": round(oos_rate, 2),
            "total_refund_qty": round(t_refund, 2),
            "cancellation_rate": round(cancel_rate, 2),
            "matched": matched,
        })

    results.sort(key=lambda x: max(x["oos_rate"], x["cancellation_rate"]), reverse=True)
    return {"results": results, "date_from": eff_from, "date_to": eff_to}


def get_order_details(df, selected_reasons, platform=None, carline_team=None,
                      date_from=None, date_to=None, sku_name=None, cancel_types=None):
    """
    回傳中咗指定原因嘅 order 明細 (每行一個 order-SKU)。
    核心欄: 訂單編號 / SKU 名稱 / SKU ID / 退款件數 / 原因。
    另附: 送貨日期 / 平台 / 商戶 方便篩選同定位。
    sku_name: 如指定, 只回傳嗰隻 SKU (供「點 SKU 展開」用)。
    """
    if df.empty:
        return []
    d = df.copy()
    d["refund_qty_n"] = to_num(d["refund_qty"])

    if platform and platform != "全部":
        d = d[d["platform"] == platform]
    if carline_team and carline_team != "全部":
        d = d[d["carline_team"] == carline_team]
    if date_from:
        d = d[d["delivery_date_norm"] >= norm_date(date_from)]
    if date_to:
        d = d[d["delivery_date_norm"] <= norm_date(date_to)]
    if sku_name:
        d = d[d["sku_name"] == sku_name]

    # 只要中咗揀嘅原因 (+ 取消分類篩選) 嗰啲行
    d = d[_reason_mask(d, selected_reasons, cancel_types)]
    if d.empty:
        return []

    rows = []
    for _, r in d.iterrows():
        rows.append({
            "order_no": str(r["order_no"]),
            "sku_name": str(r["sku_name"]),
            "sku_id": str(r["sku_id"]),
            "refund_qty": round(float(to_num(pd.Series([r["refund_qty"]])).iloc[0]), 2),
            "reason": str(r["reason"]),
            "reason_source": str(r["reason_source"]),
            "delivery_date": str(r["delivery_date_norm"]),
            "platform": str(r["platform"]),
            "carline_team": str(r["carline_team"]),
            "carline_code": str(r["carline"]),
            "merchant": str(r["merchant"]),
        })
    # 按送貨日期新到舊
    rows.sort(key=lambda x: x["delivery_date"], reverse=True)
    return rows


if __name__ == "__main__":
    df = enrich(load_sheet_df())
    print(f"讀到 {len(df)} 行")
    opts = get_reason_options(df)
    print("客人自報原因:", opts["客人自報"])
    print("客服處理原因:", opts["客服處理"])
