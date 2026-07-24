貨品取消率分析工具 (Cancellation Rate Analyzer)
================================================

功能
----
讀 Google Sheet <Record>, 計每個 SKU 嘅 Cancellation Rate。
UI 可以揀用邊啲取消原因分析, 亦可篩選平台 / 車隊 / 日期範圍。

計法 (已確認)
-------------
每個 SKU:
  分母 = 該 SKU 所有行嘅 Column M (出售量) 總和
  分子 = 符合揀咗原因嘅行, 佢哋 Column N (退款量) 嘅總和
  取消率 = 分子 / 分母 x 100%

原因來源:
  cancel_type = REFUND_REPORT_CANCEL -> 原因睇 Column L (客人自報)
  cancel_type = CS_CANCEL            -> 原因睇 Column K (客服處理)

安裝步驟
--------
1. 將成個資料夾放去:
   C:\Users\pchan\Documents\AI\cancellation_analyzer

2. 將 RRMP 個 service account JSON 複製過嚟,
   改名做:  rrmp-writer-key.json
   放喺同 app.py 一齊嘅資料夾。

3. !!! 重要 !!!
   去你張 Google Sheet 撳「共用 / Share」,
   將 service account 個 email (JSON 入面 client_email 嗰個,
   例如 xxx@xxx.iam.gserviceaccount.com)
   加做「檢視者 (Viewer)」就得。
   唔加嘅話 Python 讀唔到張 Sheet。

4. 雙擊 start.bat
   - 第一次會自動 pip install
   - 之後自動開瀏覽器去 http://127.0.0.1:5150

使用
----
1. 開咗個網頁, 佢會自動由 Sheet 抓晒所有原因做 checkbox
2. 揀你想分析嘅原因 (預設全揀)
3. 揀平台 / 車隊 / 日期範圍 (可選)
4. 撳「分析」
5. 睇每個 SKU 嘅取消率表 + Top 15 圖, 可匯出 CSV

Sheet 更新咗新資料
------------------
撳「重新讀取 Google Sheet」就會攞最新資料。

檔案結構
--------
app.py           Flask 伺服器
analyzer.py      讀 Sheet + 計算核心
settings.json    設定 (column 對應 / spreadsheet id / port)
templates/       網頁介面
requirements.txt 套件清單
start.bat        一鍵啟動

如果 column 對唔上
------------------
改 settings.json 入面 "columns" 嗰段嘅字母就得,
唔使改 code。
