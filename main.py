# main.py — AI 異質文本自動化整理系統 v3.0
# LLM：Google Gemini 2.0 Flash（免費，每日 1500 次請求）
# 新增：SSE 即時進度 / 限速並發 / 10MB 保護 / LLM 快取 / 多 Sheet / 三種模板

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

# ─── 日誌 ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("soap_processor")

# ─── 常數 ────────────────────────────────────────────────────────────────────
MAX_FILE_SIZE: int = 10 * 1024 * 1024   # 10 MB 上傳保護
CONFIDENCE_THRESHOLD: float = 0.7        # 低於此分數標記人工審核
TASK_TTL: int = 1800                     # 任務保留 30 分鐘後自動清理
MAX_CONCURRENT_LLM: int = 3              # 同時最多 3 個 Gemini 呼叫（15 RPM 緩衝）

# ─── Pydantic Schemas（三種模板）────────────────────────────────────────────

class SOAPSchema(BaseModel):
    subjective: str = Field(description="主觀自訴 (S)。若無則為空字串。")
    objective: str = Field(description="客觀數據 (O)。若無則為空字串。")
    assessment: str = Field(description="臨床評估 (A)。若無則為空字串。")
    plan: str = Field(description="處置計畫 (P)。若無則為空字串。")
    standardized_terms: List[str] = Field(description="標準醫學術語列表。")
    confidence_score: float = Field(ge=0.0, le=1.0, description="置信度 0.0~1.0。")

class ISBARSchema(BaseModel):
    identification: str = Field(description="身份識別（患者/報告人資訊）。")
    situation: str = Field(description="當前情況或主要問題。")
    background: str = Field(description="相關病史、背景脈絡。")
    assessment: str = Field(description="評估與臨床判斷。")
    recommendation: str = Field(description="建議處置或行動。")
    confidence_score: float = Field(ge=0.0, le=1.0, description="置信度 0.0~1.0。")

class GeneralSchema(BaseModel):
    summary: str = Field(description="文本核心摘要（2~3 句）。")
    key_points: List[str] = Field(description="3~5 個重點列表。")
    action_items: List[str] = Field(description="需跟進的行動項目，若無則為空列表。")
    sentiment: str = Field(description="整體情感傾向：正面、中性 或 負面。")
    confidence_score: float = Field(ge=0.0, le=1.0, description="置信度 0.0~1.0。")

# ─── System Prompts ───────────────────────────────────────────────────────────

SOAP_PROMPT = """
你是一位定性研究數據分析專家，具備豐富的臨床醫學知識與自然語言處理能力。

【核心任務】
你會收到一段從多人協作 Excel 擷取的「異質欄位快照」。欄位由不同醫師、護理師自由命名，混雜英文縮寫（BP、HR、SpO2、pt、c/o、r/o、Rx、Dx、Hx、QD、BID 等）與中文描述。
任務：將碎片化資訊精確歸類到 SOAP 四維度。

【SOAP 定義】
- S (Subjective)：患者描述的症狀、不適感、主訴。
- O (Objective)：可量測指標，如血壓、心跳、血氧、實驗室數值。
- A (Assessment)：醫師診斷結論、疾病名稱、鑑別診斷。
- P (Plan)：處方藥物、治療計畫、回診安排、衛教。

【Few-Shot 範例一】
輸入：【欄位-王醫師備註】: pt c/o chest tightness x 3 days | 【欄位-看診紀錄】: BP 158/92, HR 88, SpO2 97% | 【欄位-診斷】: Hypertension Stage 2, r/o ACS | 【欄位-處置】: Amlodipine 5mg QD, 2週後回診
輸出：{"subjective":"患者主訴胸悶不適持續 3 天","objective":"血壓 158/92 mmHg，心跳 88 次/分，血氧 97%","assessment":"高血壓第二期，需排除急性冠心症","plan":"Amlodipine 5mg 每日一次，2 週後回診","standardized_terms":["Hypertension Stage 2","ACS","Amlodipine","chest tightness"],"confidence_score":0.95}

【Few-Shot 範例二】
輸入：【欄位-主述】: 頭痛暈眩 | 【欄位-檢查】: fasting glucose 185, HbA1c 8.2% | 【欄位-評估】: 控糖不佳 | 【欄位-藥物】: Metformin 1000mg BID + Jardiance 10mg QD
輸出：{"subjective":"近期持續頭痛伴偶發眩暈","objective":"空腹血糖 185 mg/dL，HbA1c 8.2%","assessment":"第二型糖尿病血糖控制不佳","plan":"Metformin 1000mg BID，加開 Jardiance 10mg QD","standardized_terms":["Type 2 Diabetes","HbA1c","Metformin","Empagliflozin"],"confidence_score":0.92}

【Few-Shot 範例三（資訊稀少）】
輸入：【欄位-col_A】: 還好 | 【欄位-備註】: 無
輸出：{"subjective":"患者表示狀況尚可","objective":"","assessment":"","plan":"","standardized_terms":[],"confidence_score":0.22}

規則：無對應資訊的欄位設為空字串；輸出只能是符合 Schema 的 JSON。
""".strip()

ISBAR_PROMPT = """
你是一位醫療品質管理與溝通專家，負責將異質文本整理為標準 ISBAR 交班格式。

【ISBAR 定義】
- I (Identification)：說話者與患者的身份識別（姓名、病房、年齡）。
- S (Situation)：當前最緊急的情況或需立即關注的主要問題。
- B (Background)：相關病史、入院原因、目前用藥、過敏史等背景。
- A (Assessment)：對目前狀況的臨床判斷或擔憂。
- R (Recommendation)：具體建議的行動、請求或處置。

【Few-Shot 範例】
輸入：【欄位-護理師】: 林護理師 | 【欄位-患者】: 張先生 65歲 心內科6床 | 【欄位-狀況】: BP 90/60 冒冷汗 | 【欄位-病史】: 心衰 Furosemide | 【欄位-判斷】: 疑容積不足 | 【欄位-需要】: 請值班醫師評估補液
輸出：{"identification":"林護理師，患者張先生，65歲，心內科6床","situation":"血壓驟降至 90/60 mmHg 並出現冒冷汗症狀","background":"患者有心臟衰竭病史，目前使用 Furosemide","assessment":"疑似容積不足（Volume Depletion）","recommendation":"請值班醫師立即評估是否需補液治療","confidence_score":0.91}

規則：無對應資訊的欄位設為空字串；輸出只能是符合 Schema 的 JSON。
""".strip()

GENERAL_PROMPT = """
你是一位文本分析與資訊萃取專家，負責將格式混亂的異質欄位整理為結構化摘要。

【輸出欄位定義】
- summary：2~3 句話總結文本核心內容。
- key_points：3~5 個最重要的資訊點列表。
- action_items：需跟進的具體行動或待辦事項，若無則為空列表。
- sentiment：整體情感傾向，只能是「正面」、「中性」或「負面」之一。

【Few-Shot 範例】
輸入：【欄位-主題】: Q3 業績回顧 | 【欄位-結果】: 達成率 87% 低於目標 | 【欄位-主因】: 東南亞推廣不力 | 【欄位-決議】: 下月加派兩名業務至泰國，重新制定定價策略
輸出：{"summary":"Q3 業績達成率 87%，未達目標，主因為東南亞市場推廣不佳，決議加強泰國市場投入。","key_points":["Q3 達成率 87%，低於既定目標","東南亞（泰國）推廣效果不佳","決議增派業務至泰國"],"action_items":["增派兩名業務人員至泰國","重新制定東南亞定價策略"],"sentiment":"負面","confidence_score":0.88}

規則：輸出只能是符合 Schema 的 JSON。
""".strip()

# ─── 模板 Registry ────────────────────────────────────────────────────────────
TEMPLATES: Dict[str, Dict[str, Any]] = {
    "soap": {
        "label": "SOAP 醫療病歷",
        "schema": SOAPSchema,
        "prompt": SOAP_PROMPT,
    },
    "isbar": {
        "label": "ISBAR 醫療交班",
        "schema": ISBARSchema,
        "prompt": ISBAR_PROMPT,
    },
    "general": {
        "label": "通用文本摘要",
        "schema": GeneralSchema,
        "prompt": GENERAL_PROMPT,
    },
}

# ─── 任務管理 ─────────────────────────────────────────────────────────────────
# 結構：task_id → {status, queue, result_bytes, output_filename, error, created_at}
tasks: Dict[str, Dict[str, Any]] = {}

# ─── LLM 快取（SHA256 key → (schema_obj, needs_review)）────────────────────
_llm_cache: Dict[str, Tuple[Any, bool]] = {}

# ─── FastAPI 應用程式 ─────────────────────────────────────────────────────────
app = FastAPI(
    title="AI 異質文本自動化整理系統",
    version="3.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# ─── Gemini 客戶端 ────────────────────────────────────────────────────────────
GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "")
if not GEMINI_API_KEY:
    logger.warning("GEMINI_API_KEY 未設定，LLM 呼叫將會失敗。")

gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# 並發限速：免費方案 15 RPM，最多同時送 3 個請求
_llm_semaphore: Optional[asyncio.Semaphore] = None


@app.on_event("startup")
async def _startup() -> None:
    global _llm_semaphore
    _llm_semaphore = asyncio.Semaphore(MAX_CONCURRENT_LLM)
    asyncio.create_task(_cleanup_loop())
    logger.info("應用程式啟動完成。")


# ─── 輔助函式 ─────────────────────────────────────────────────────────────────

def build_context_snapshot(row: pd.Series) -> str:
    """逐欄拼接非空值為 LLM 可讀快照，零 hardcode 欄位名。"""
    parts: List[str] = []
    for col, val in row.items():
        s = str(val).strip()
        if s and s not in ("nan", "None", "NaT", ""):
            parts.append(f"【欄位-{col}】: {s}")
    return " | ".join(parts)


def _cache_key(snapshot: str, template: str) -> str:
    return hashlib.sha256(f"{template}:{snapshot}".encode()).hexdigest()


def _flatten(obj: Any) -> Dict[str, Any]:
    """將任意 Pydantic schema 攤平為 dict，List[str] → 逗號字串。"""
    d = obj.model_dump()
    for k, v in d.items():
        if isinstance(v, list):
            d[k] = ", ".join(str(i) for i in v)
    return d


def _fallback(template_key: str) -> Any:
    """各模板的空白 fallback 實例（LLM 解析失敗時使用）。"""
    if template_key == "soap":
        return SOAPSchema(
            subjective="", objective="", assessment="", plan="",
            standardized_terms=[], confidence_score=0.0,
        )
    if template_key == "isbar":
        return ISBARSchema(
            identification="", situation="", background="",
            assessment="", recommendation="", confidence_score=0.0,
        )
    return GeneralSchema(
        summary="", key_points=[], action_items=[],
        sentiment="", confidence_score=0.0,
    )


def _friendly_error(exc: Exception) -> str:
    """將技術性例外轉換為使用者可讀的中文說明。"""
    mapping = {
        "AuthenticationError":  "API Key 無效或已過期，請重新確認 GEMINI_API_KEY。",
        "PermissionDenied":     "API 存取被拒，請確認 API Key 已啟用 Gemini API。",
        "ResourceExhausted":    "已用完 Gemini 免費配額（每日 1500 次），請明日再試。",
        "RateLimitError":       "請求速率過高，請稍後再試。",
        "DeadlineExceeded":     "Gemini API 回應逾時，請重試。",
        "InvalidArgument":      "請求格式錯誤，可能是 Excel 內容異常或欄位過長。",
        "ServiceUnavailable":   "Gemini 服務暫時無法使用，請稍後再試。",
    }
    name = type(exc).__name__
    msg = str(exc)
    for key, description in mapping.items():
        if key in name or key in msg:
            return description
    return f"處理失敗（{name}）：請確認 Excel 格式正確且服務正常運行。"


def _safe_sheet_name(original: str, prefix: str = "標準化_") -> str:
    """處理 Excel sheet 名稱：移除非法字元並限制在 31 字以內。"""
    clean = re.sub(r'[\\/?*\[\]:]', '_', original)
    max_len = 31 - len(prefix)
    return f"{prefix}{clean[:max_len]}"


# ─── LLM 核心（含快取 + 限速並發 + 指數退避）────────────────────────────────

async def parse_row_with_llm(snapshot: str, template: str) -> Tuple[Any, bool]:
    """
    呼叫 Gemini Structured Output，回傳 (schema_obj, needs_review)。
    快取命中直接回傳，不消耗 API 配額。
    Semaphore 確保同時最多 MAX_CONCURRENT_LLM 個並發請求。
    """
    key = _cache_key(snapshot, template)
    if key in _llm_cache:
        logger.info("LLM 快取命中，跳過 API 呼叫。")
        return _llm_cache[key]

    tpl = TEMPLATES[template]
    config = types.GenerateContentConfig(
        system_instruction=tpl["prompt"],
        temperature=0.1,
        response_mime_type="application/json",
        response_schema=tpl["schema"],
        max_output_tokens=1024,
    )

    async with _llm_semaphore:
        for attempt in range(4):
            try:
                response = await gemini_client.aio.models.generate_content(
                    model="gemini-2.0-flash",
                    contents=f"請分析以下異質欄位快照：\n\n{snapshot}",
                    config=config,
                )

                if not response.text:
                    raise ValueError("Gemini 回傳空白內容")

                raw: dict = json.loads(response.text.strip())
                raw["confidence_score"] = max(
                    0.0, min(1.0, float(raw.get("confidence_score", 0.0)))
                )
                result = tpl["schema"].model_validate(raw)
                needs_review = result.confidence_score < CONFIDENCE_THRESHOLD

                _llm_cache[key] = (result, needs_review)
                return result, needs_review

            except Exception as exc:
                if attempt < 3:
                    exc_name = type(exc).__name__
                    exc_str  = str(exc)
                    is_rate_limit = (
                        "429" in exc_str or "ResourceExhausted" in exc_name
                        or "TooManyRequests" in exc_name or "rate" in exc_str.lower()
                    )
                    wait = 65 if is_rate_limit else (2 ** attempt * 2)
                    logger.warning(
                        f"Gemini 呼叫失敗（第 {attempt + 1} 次），{wait}s 後重試：{exc}"
                    )
                    await asyncio.sleep(wait)
                    continue

                logger.error(f"Gemini 最終失敗：{exc}")
                fb = _fallback(template)
                return fb, True


# ─── 背景處理任務 ─────────────────────────────────────────────────────────────

async def process_file(
    task_id: str, raw_bytes: bytes, filename: str, template: str
) -> None:
    """
    逐 Sheet 讀取 Excel → 逐列 LLM 解析 → 合併輸出 → 存入任務結果。
    透過 asyncio.Queue 即時推送進度事件給 SSE 端點。
    """
    queue: asyncio.Queue = tasks[task_id]["queue"]

    async def push(event: dict) -> None:
        await queue.put(event)

    try:
        xl = pd.ExcelFile(io.BytesIO(raw_bytes))
        sheet_names: List[str] = xl.sheet_names
        total_sheets = len(sheet_names)

        output_buffer = io.BytesIO()

        with pd.ExcelWriter(output_buffer, engine="openpyxl") as writer:
            for sheet_idx, sheet_name in enumerate(sheet_names, start=1):
                df: pd.DataFrame = xl.parse(sheet_name, dtype=str).fillna("")

                # 去首尾空格（相容 pandas 1.x / 2.x）
                strip = lambda x: x.strip() if isinstance(x, str) else x  # noqa
                try:
                    df = df.applymap(strip)
                except AttributeError:
                    df = df.map(strip)

                total_rows = len(df)

                if total_rows == 0:
                    await push({
                        "type": "warning",
                        "message": f"工作表「{sheet_name}」無資料，已跳過。",
                    })
                    continue

                logger.info(
                    f"[{task_id}] 處理 Sheet {sheet_idx}/{total_sheets}："
                    f"「{sheet_name}」，共 {total_rows} 列"
                )

                parsed_rows: List[dict] = []

                for idx, row in df.iterrows():
                    snapshot = build_context_snapshot(row)

                    if not snapshot:
                        row_result = _flatten(_fallback(template))
                        row_result["待人工審核標記"] = "TRUE"
                    else:
                        schema_obj, needs_review = await parse_row_with_llm(
                            snapshot, template
                        )
                        row_result = _flatten(schema_obj)
                        row_result["待人工審核標記"] = "TRUE" if needs_review else "FALSE"

                    parsed_rows.append(row_result)

                    # 每列處理完畢即推送進度
                    await push({
                        "type": "progress",
                        "sheet":        sheet_name,
                        "sheet_index":  sheet_idx,
                        "sheets_total": total_sheets,
                        "current":      int(idx) + 1,
                        "total":        total_rows,
                    })

                    # 每列間隔 4 秒，確保不超過免費方案 15 RPM 上限
                    await asyncio.sleep(4.0)

                result_df = pd.concat(
                    [df.reset_index(drop=True), pd.DataFrame(parsed_rows)],
                    axis=1,
                )
                out_sheet = _safe_sheet_name(sheet_name)
                result_df.to_excel(writer, index=False, sheet_name=out_sheet)

        output_buffer.seek(0)
        original_stem = os.path.splitext(filename)[0]
        output_filename = f"標準化研究數據_{original_stem}.xlsx"

        tasks[task_id].update({
            "status":          "done",
            "result_bytes":    output_buffer.read(),
            "output_filename": output_filename,
        })

        await push({"type": "done", "filename": output_filename})
        logger.info(f"[{task_id}] 處理完畢：{output_filename}")

    except Exception as exc:
        msg = _friendly_error(exc)
        logger.exception(f"[{task_id}] 處理失敗：{exc}")
        tasks[task_id].update({"status": "error", "error": msg})
        await push({"type": "error", "message": msg})


# ─── 任務清理 ─────────────────────────────────────────────────────────────────

async def _cleanup_loop() -> None:
    """每 5 分鐘清理超過 30 分鐘的過期任務，防止記憶體洩漏。"""
    while True:
        await asyncio.sleep(300)
        now = time.time()
        expired = [
            tid for tid, t in list(tasks.items())
            if now - t["created_at"] > TASK_TTL
        ]
        for tid in expired:
            del tasks[tid]
        if expired:
            logger.info(f"清理 {len(expired)} 個過期任務。")


# ─── 路由 ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def serve_frontend() -> HTMLResponse:
    """提供前端頁面，避免跨來源問題。"""
    path = Path(__file__).parent / "index.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="找不到 index.html")
    return HTMLResponse(content=path.read_text(encoding="utf-8"))


@app.get("/api/templates", summary="取得可用模板清單")
async def get_templates() -> Dict[str, str]:
    return {k: v["label"] for k, v in TEMPLATES.items()}


@app.post("/api/upload", summary="上傳 Excel，立即回傳任務 ID")
async def start_upload(
    file: UploadFile = File(...),
    template: str = Form("soap"),
) -> Dict[str, str]:
    """
    驗證後立即回傳 task_id，後台非同步開始處理。
    前端收到 task_id 後連接 /api/stream/{task_id} 取得即時進度。
    """
    if template not in TEMPLATES:
        raise HTTPException(
            status_code=400,
            detail=f"不支援的模板「{template}」，可選：{list(TEMPLATES.keys())}",
        )

    filename: str = file.filename or "unknown.xlsx"
    if not filename.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(
            status_code=400, detail="僅接受 .xlsx 或 .xls 格式的 Excel 檔案。"
        )

    raw_bytes: bytes = await file.read()

    if len(raw_bytes) > MAX_FILE_SIZE:
        mb = len(raw_bytes) / 1024 / 1024
        raise HTTPException(
            status_code=413,
            detail=f"檔案過大（{mb:.1f} MB），上限為 {MAX_FILE_SIZE // 1048576} MB。",
        )

    task_id = str(uuid.uuid4())
    tasks[task_id] = {
        "status":          "processing",
        "queue":           asyncio.Queue(),
        "result_bytes":    None,
        "output_filename": None,
        "error":           None,
        "created_at":      time.time(),
    }

    asyncio.create_task(process_file(task_id, raw_bytes, filename, template))
    logger.info(f"任務建立：{task_id}，檔案：{filename}，模板：{template}")

    return {"task_id": task_id}


@app.get("/api/stream/{task_id}", summary="SSE 即時進度串流")
async def stream_progress(task_id: str) -> StreamingResponse:
    """
    Server-Sent Events 端點。
    每 30 秒發送 ping 保持連線，直到收到 done / error 事件後關閉。
    """
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="任務不存在或已過期（30 分鐘清理）。")

    async def event_gen():
        queue: asyncio.Queue = tasks[task_id]["queue"]
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30)
            except asyncio.TimeoutError:
                yield 'data: {"type":"ping"}\n\n'
                continue

            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

            if event.get("type") in ("done", "error"):
                break

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",   # 防止 Nginx 緩衝 SSE
        },
    )


@app.get("/api/download/{task_id}", summary="下載處理完成的 Excel")
async def download_result(task_id: str) -> StreamingResponse:
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="任務不存在或已過期（30 分鐘清理）。")

    task = tasks[task_id]
    if task["status"] == "error":
        raise HTTPException(status_code=500, detail=task["error"])
    if task["status"] != "done":
        raise HTTPException(
            status_code=425, detail="檔案尚未處理完成，請等待進度完成後再下載。"
        )

    buffer = io.BytesIO(task["result_bytes"])
    encoded = quote(task["output_filename"])

    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition":          f"attachment; filename*=UTF-8''{encoded}",
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
    )
