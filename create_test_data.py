"""
製作三份測試 Excel，分別對應 SOAP / ISBAR / General 三種模板。
執行：python create_test_data.py
"""
import pandas as pd
from pathlib import Path

OUT_DIR = Path(__file__).parent

# ── 1. SOAP 醫療病歷測試資料 ─────────────────────────────────────────────────
soap_data = {
    "王醫師備註": [
        "pt c/o chest tightness x 3 days",
        "頭痛暈眩，失眠兩週",
        "腹痛 nausea, diarrhea x 1 day",
        "還好",                        # ← 資訊稀少，預期低置信度
        "knee pain after jogging，局部腫脹",
    ],
    "看診紀錄": [
        "BP 158/92, HR 88, SpO2 97%",
        "fasting glucose 185, HbA1c 8.2%",
        "T 38.5°C, HR 102, 腸音亢進",
        "無",
        "X-ray: no fracture, ROM受限",
    ],
    "診斷": [
        "Hypertension Stage 2, r/o ACS",
        "控糖不佳 Type 2 DM",
        "Acute Gastroenteritis",
        "",
        "Knee strain / 膝關節扭傷",
    ],
    "處置": [
        "Amlodipine 5mg QD, 2週後回診",
        "Metformin 1000mg BID + Jardiance 10mg QD",
        "Oral rehydration, 低油飲食，必要時 Smecta",
        "",
        "Ice + rest + Naproxen 500mg BID, 若無改善轉骨科",
    ],
    "過敏史": [
        "NKDA",
        "Penicillin allergy",
        "",
        "",
        "NKDA",
    ],
}

# ── 2. ISBAR 醫療交班測試資料 ────────────────────────────────────────────────
isbar_data = {
    "護理師": [
        "林護理師",
        "陳護理師",
        "王護理師",
    ],
    "患者資訊": [
        "張先生 65歲 心內科6床",
        "李小姐 42歲 內科3床",
        "黃先生 78歲 ICU 2床",
    ],
    "當前狀況": [
        "BP 90/60 冒冷汗，10分鐘前突然惡化",
        "SpO2 drop to 89%, 呼吸急促 RR 28",
        "GCS 從 12 降至 8，瞳孔不等大",
    ],
    "病史背景": [
        "心衰病史，目前 Furosemide 40mg QD",
        "哮喘，近期換 Salbutamol inhaler",
        "腦出血術後 Day3，ICP monitor in situ",
    ],
    "臨床判斷": [
        "疑容積不足，可能利尿劑過量",
        "疑支氣管痙攣或肺水腫",
        "顱內壓升高或再出血",
    ],
    "需要協助": [
        "請值班醫師立即評估補液",
        "請respiratory therapist協助，並準備插管",
        "請神外主治醫師緊急評估，準備CT",
    ],
}

# ── 3. General 通用文本測試資料（業務/行政場景）────────────────────────────
general_data = {
    "主題": [
        "Q3 業績回顧",
        "新產品上線計劃",
        "員工滿意度調查",
        "IT 系統故障報告",
    ],
    "結果": [
        "達成率 87%，低於目標 100%",
        "MVP 完成，待 UAT 測試",
        "整體滿意度 72 分，比上季下降 5 分",
        "2026-05-16 14:00 ERP 系統當機 2 小時",
    ],
    "主因": [
        "東南亞推廣效果不佳，泰國市場滲透率僅 12%",
        "前端整合完成，後端 API 仍有 2 個 bug",
        "薪資福利與晉升透明度滿意度偏低",
        "資料庫磁碟空間不足，自動備份觸發 lock",
    ],
    "決議": [
        "下月加派兩名業務至泰國，重新制定定價策略",
        "修復 bug 後 2026-06-01 正式上線",
        "HR 將在 2026-06-15 前提出薪酬優化方案",
        "擴充磁碟至 2TB，強化監控告警閾值",
    ],
    "負責人": [
        "業務部 張經理",
        "產品部 李副理",
        "HR 王主任",
        "IT 部 陳工程師",
    ],
}

# ── 4. 混合壓力測試（包含空列、重複列、特殊字元）────────────────────────────
stress_data = {
    "醫師備註_A": ["pt c/o headache", "", "pt c/o headache", "!!特殊字元@#$", "normal"],
    "data_B":     ["BP 120/80",        "", "BP 120/80",        "???",            "HR 72"],
    "col_C":      ["HTN Dx",           "", "HTN Dx",           None,             ""],
    "處置_D":     ["Aspirin 100mg QD", "", "Aspirin 100mg QD", "N/A",            "觀察"],
}

# ── 輸出檔案 ─────────────────────────────────────────────────────────────────
soap_path    = OUT_DIR / "test_soap.xlsx"
isbar_path   = OUT_DIR / "test_isbar.xlsx"
general_path = OUT_DIR / "test_general.xlsx"
stress_path  = OUT_DIR / "test_stress.xlsx"

pd.DataFrame(soap_data).to_excel(soap_path,    index=False)
pd.DataFrame(isbar_data).to_excel(isbar_path,  index=False)
pd.DataFrame(general_data).to_excel(general_path, index=False)

# 壓力測試：多個 Sheet
with pd.ExcelWriter(stress_path, engine="openpyxl") as writer:
    pd.DataFrame(stress_data).to_excel(writer, sheet_name="Sheet1_重複", index=False)
    pd.DataFrame(soap_data).to_excel(writer,   sheet_name="Sheet2_SOAP",  index=False)
    pd.DataFrame({"空欄": []}).to_excel(writer, sheet_name="Sheet3_空", index=False)

print("[OK] Test data generated:")
print(f"  - {soap_path.name}    -> SOAP template")
print(f"  - {isbar_path.name}   -> ISBAR template")
print(f"  - {general_path.name} -> General template")
print(f"  - {stress_path.name}  -> Stress test (3 sheets: duplicates / empty rows / special chars)")
